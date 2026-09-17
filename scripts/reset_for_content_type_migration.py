"""One-shot reset for the ADR-0028 fresh-start (per-item content_type).

Wipes derived data across every product so the next pipeline run
recreates warehouses under the new schema. KEEPS products/<pid>/
(your configuration) so you don't have to reconfigure sources,
schedules, LLM routing, etc.

    # See what would be deleted (dry-run is the default):
    python scripts/reset_for_content_type_migration.py

    # Actually wipe:
    python scripts/reset_for_content_type_migration.py --commit

    # Also re-run init_db per product after wiping:
    python scripts/reset_for_content_type_migration.py --commit --reinit

Rationale
---------
ADR-0028 changes `items.content_type` from optional metadata to a
NOT NULL column and drops `items.author_intent` entirely. The user
confirmed no backfill of existing data is required — this script is
how you enact that choice. `pipeline/run.py` refuses to run against a
pre-ADR-0028 warehouse until this script has been executed with
`--commit`, so forgetting is non-catastrophic.

Deletes (per product):
  data/<pid>/warehouse.duckdb
  data/<pid>/state.sqlite
  data/<pid>/raw/
  data/<pid>/run_logs/
  reports/<pid>/

Keeps:
  products/<pid>/     (all your configuration)
  config/             (global settings, features, prompts)
  .env                (secrets)

Any `.wal` / `.shm` journal siblings of the .duckdb / .sqlite files
are also removed.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _size(p: Path) -> int:
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in p.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def _remove(p: Path, commit: bool) -> tuple[int, int]:
    """Return (files_removed, bytes_freed). No-op on missing paths."""
    if not p.exists():
        return 0, 0
    bytes_freed = _size(p)
    if p.is_file():
        files = 1
        if commit:
            try:
                p.unlink()
            except OSError:
                return 0, 0
    else:
        files = sum(1 for c in p.rglob("*") if c.is_file())
        if commit:
            try:
                shutil.rmtree(p)
            except OSError:
                return 0, 0
    return files, bytes_freed


def _iter_products(data_root: Path):
    if not data_root.exists():
        return
    for child in sorted(data_root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield child.name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true",
                    help="Actually delete. Without this, prints what WOULD be deleted.")
    ap.add_argument("--reinit", action="store_true",
                    help="After wiping, re-run init_db for each product so the "
                         "new schema is live. Off by default so operators can "
                         "inspect the reset before re-initializing.")
    ap.add_argument("--data-root", type=Path, default=None,
                    help="Override data root (default from app.yaml).")
    ap.add_argument("--reports-root", type=Path, default=None,
                    help="Override reports root (default from app.yaml).")
    args = ap.parse_args()

    if args.data_root is not None:
        data_root = args.data_root
    else:
        from pipeline.config import app_config, resolve_path
        data_root = resolve_path(app_config()["paths"]["data_root"])
    if args.reports_root is not None:
        reports_root = args.reports_root
    else:
        from pipeline.config import app_config, resolve_path
        reports_root = resolve_path(app_config()["paths"]["reports_root"])

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"[reset] {mode} data_root={data_root}")
    print(f"[reset] {mode} reports_root={reports_root}")

    total_files = 0
    total_bytes = 0
    products = list(_iter_products(data_root))
    for pid in products:
        product_data = data_root / pid
        product_reports = reports_root / pid
        # Individual targets so a failure on one doesn't hide the others.
        targets = [
            product_data / "warehouse.duckdb",
            product_data / "warehouse.duckdb.wal",
            product_data / "state.sqlite",
            product_data / "state.sqlite-wal",
            product_data / "state.sqlite-shm",
            product_data / "raw",
            product_data / "run_logs",
            product_reports,
        ]
        pfiles = pbytes = 0
        for t in targets:
            f, b = _remove(t, args.commit)
            pfiles += f
            pbytes += b
        if pfiles:
            print(f"[reset]   {pid}: {pfiles} files ({pbytes / 1e6:.1f} MB)")
        total_files += pfiles
        total_bytes += pbytes

    verb = "removed" if args.commit else "would remove"
    print(f"[reset] {verb} {total_files} files ({total_bytes / 1e6:.1f} MB total) "
          f"across {len(products)} product(s)")

    if args.commit and args.reinit:
        print("[reset] re-initializing warehouses under new schema...")
        from scripts import init_db as _init_db  # noqa: E402
        for pid in products:
            try:
                # init_db.main reads --product from argv; call the functional
                # entry points directly.
                from pipeline.config import set_current_product
                set_current_product(pid)
                from pipeline import storage
                _init_db.init_warehouse(storage.warehouse_path())
                _init_db.init_state(storage.state_path())
                print(f"[reset]   {pid}: reinitialized")
            except Exception as e:
                print(f"[reset]   {pid}: reinit FAILED ({e}); "
                      f"run `python scripts/init_db.py --product {pid}` manually")

    if not args.commit and total_files:
        print("[reset] re-run with --commit to actually delete "
              "(add --reinit to also re-run init_db afterward)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
