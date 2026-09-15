"""Retention pruning for `data/<product>/` (issue #12c).

Two things in this project grow without bound on an unattended box:

  1. `data/<product>/raw/<source>/<week>/*.jsonl` — the system of record.
     Preserving these is what lets you rebuild the warehouse from source
     if classification prompts change. But nobody documented that they
     grow forever, and a headless install fills its disk in ~a year.

  2. `data/<product>/run_logs/*.{out,json,running,pid}` — one set per
     pipeline run. `.out` is the captured stdout/stderr, easily 5-20 MB
     for a run with LLM tracing. Piles up under a weekly scheduler.

This script prunes both, opt-in and dry-run by default.

    # See what would be deleted (default dry-run):
    python scripts/prune_data.py --raw-older-than 52w --run-logs-older-than 12w

    # Actually delete:
    python scripts/prune_data.py --raw-older-than 52w --commit

Never touches `products/`, `config/`, `.env`, or the warehouse itself.
Refuse to delete anything without an explicit retention flag.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DURATION_RE = re.compile(r"^(\d+)([dwmy])$")


def _parse_duration(spec: str) -> timedelta:
    """`4w` → 28d, `12m` → 360d (30d months), `1y` → 365d, `30d` → 30d."""
    m = _DURATION_RE.match(spec.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"duration must look like `30d`, `12w`, `6m`, or `1y`, got {spec!r}"
        )
    n = int(m.group(1))
    unit = m.group(2)
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
    return timedelta(days=n * days)


def _iter_products(data_root: Path):
    """Yield product-id directories under `data/`. Skips hidden dirs and
    non-directories."""
    if not data_root.exists():
        return
    for child in sorted(data_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield child


def _prune_raw_jsonl(
    product_dir: Path, older_than: timedelta, now: datetime,
    commit: bool,
) -> tuple[int, int]:
    """Delete raw JSONL files whose enclosing week-directory represents a
    week older than `older_than`. Returns (files_removed, bytes_freed).

    Deletes at the FILE level (not directory) so an empty week-dir left
    behind is fine — subsequent runs recreate it if needed. Doesn't try
    to be clever about dating from file mtime; the week_id in the path
    is the authoritative source of "when this data represents."
    """
    raw = product_dir / "raw"
    if not raw.exists():
        return 0, 0
    cutoff = now - older_than
    files_removed = 0
    bytes_freed = 0
    # Path layout: raw/<source>/<week_id>/*.jsonl. week_id is ISO
    # YYYY-Www; we parse the week's Monday as its representative date.
    for source_dir in raw.iterdir():
        if not source_dir.is_dir():
            continue
        for week_dir in source_dir.iterdir():
            if not week_dir.is_dir():
                continue
            week_date = _week_id_to_date(week_dir.name)
            if week_date is None or week_date >= cutoff:
                continue
            for jsonl in week_dir.glob("*.jsonl"):
                try:
                    size = jsonl.stat().st_size
                except OSError:
                    size = 0
                if commit:
                    try:
                        jsonl.unlink()
                    except OSError:
                        continue
                files_removed += 1
                bytes_freed += size
    return files_removed, bytes_freed


def _week_id_to_date(week_id: str) -> datetime | None:
    """`2025-W42` → the Monday of ISO week 42, 2025 (UTC midnight)."""
    m = re.match(r"^(\d{4})-W(\d{1,2})$", week_id)
    if not m:
        return None
    year, week = int(m.group(1)), int(m.group(2))
    try:
        # %G/%V handle ISO week years correctly (e.g. 2020-W53 exists).
        return datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _prune_run_logs(
    product_dir: Path, older_than: timedelta, now: datetime,
    commit: bool,
) -> tuple[int, int]:
    """Delete run-log files older than the cutoff. `.running` markers are
    NEVER pruned — those are handled by the scheduler's stale-marker
    detector (fix #2). Deletes the full set `<run_id>.{out,json,pid}` as
    a group so we don't leave dangling metadata."""
    run_logs = product_dir / "run_logs"
    if not run_logs.exists():
        return 0, 0
    cutoff = now - older_than
    files_removed = 0
    bytes_freed = 0

    # Group by run_id (stem) so we delete all sidecars together.
    stems: dict[str, list[Path]] = {}
    for f in run_logs.iterdir():
        if not f.is_file():
            continue
        if f.suffix in (".running",):
            continue  # scheduler's job to clean these
        stems.setdefault(f.stem, []).append(f)

    for stem, files in stems.items():
        # Use the newest mtime across the group as the group's age. A
        # `.json` written at run completion is the most reliable signal
        # of when the run "finished"; falls back to `.out` mtime for
        # crashed runs that never wrote a JSON.
        newest = max((f.stat().st_mtime for f in files), default=0)
        if newest == 0:
            continue
        ts = datetime.fromtimestamp(newest, tz=timezone.utc)
        if ts >= cutoff:
            continue
        # Preserve the JSON summary if requested — but by default we
        # prune the whole set. A partial keep is cognitive load: either
        # the run is history worth keeping (keep everything) or it's
        # not (drop everything).
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            if commit:
                try:
                    f.unlink()
                except OSError:
                    continue
            files_removed += 1
            bytes_freed += size
    return files_removed, bytes_freed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Prune old raw JSONL + run logs from data/<product>/.",
    )
    ap.add_argument(
        "--raw-older-than", type=_parse_duration, default=None,
        help="Delete raw JSONL files from weeks older than this "
             "(e.g. `52w`, `1y`). Omit to skip raw pruning.",
    )
    ap.add_argument(
        "--run-logs-older-than", type=_parse_duration, default=None,
        help="Delete run-log files (.out/.json/.pid) older than this "
             "(e.g. `12w`, `6m`). Omit to skip run-log pruning.",
    )
    ap.add_argument(
        "--commit", action="store_true",
        help="Actually delete. Without this, prints what WOULD be deleted "
             "and exits 0.",
    )
    ap.add_argument(
        "--data-root", type=Path, default=None,
        help="Override data root (default: read from config/app.yaml).",
    )
    args = ap.parse_args()

    if args.raw_older_than is None and args.run_logs_older_than is None:
        ap.error(
            "specify at least one of --raw-older-than / --run-logs-older-than "
            "(no default: this script never deletes without an explicit request)"
        )

    if args.data_root is not None:
        data_root = args.data_root
    else:
        from pipeline.config import app_config, resolve_path
        data_root = resolve_path(app_config()["paths"]["data_root"])

    if not data_root.exists():
        print(f"[prune] data root does not exist: {data_root}", file=sys.stderr)
        return 0

    now = datetime.now(timezone.utc)
    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"[prune] {mode} data_root={data_root}")

    total_files = 0
    total_bytes = 0
    for product_dir in _iter_products(data_root):
        files_raw = bytes_raw = 0
        files_runs = bytes_runs = 0
        if args.raw_older_than is not None:
            files_raw, bytes_raw = _prune_raw_jsonl(
                product_dir, args.raw_older_than, now, args.commit,
            )
        if args.run_logs_older_than is not None:
            files_runs, bytes_runs = _prune_run_logs(
                product_dir, args.run_logs_older_than, now, args.commit,
            )
        if files_raw or files_runs:
            print(
                f"[prune]   {product_dir.name}: "
                f"raw={files_raw} files ({bytes_raw / 1e6:.1f} MB), "
                f"run_logs={files_runs} files ({bytes_runs / 1e6:.1f} MB)"
            )
        total_files += files_raw + files_runs
        total_bytes += bytes_raw + bytes_runs

    verb = "removed" if args.commit else "would remove"
    print(f"[prune] {verb} {total_files} files ({total_bytes / 1e6:.1f} MB total)")
    if not args.commit and total_files:
        print("[prune] re-run with --commit to actually delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
