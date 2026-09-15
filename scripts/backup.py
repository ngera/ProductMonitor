"""Backup script (#12d).

Zips the state you'd need to restore a ProductMonitor install onto a
different machine. See documents/OPERATING.md for what's in scope and why.

    # Default (safe — no secrets):
    python scripts/backup.py

    # Include .env (contains API keys — encrypt or move offline):
    python scripts/backup.py --include-secrets

What's included by default:
  - products/           per-product config (wizard output, schedules)
  - config/             global settings, feature flags
  - data/               warehouses + state DBs + raw JSONL + run logs

What's NOT included:
  - reports/            derived from data/ — rebuild via re-render
  - backups/            circular
  - .venv/, __pycache__ operator machine state
  - .env                unless --include-secrets

Warehouses (`*.duckdb`) are backed up as-is; DuckDB is file-based and
locks are process-scoped, so a copy taken while the pipeline is idle
is consistent. Don't back up during a run — either wait, or preserve
raw JSONL and accept the ~30-minute re-classify cost of rebuilding
from raw if the copy is torn.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

# Ordered so the archive listing reads config → products → data (small to
# large). Consumer can extract a subset with `unzip -l | grep config/`.
_DEFAULT_TARGETS = ["config", "products", "data"]
_SECRET_FILES = [".env"]

# Skip transient DuckDB / SQLite journal files. These are consistent
# only alongside their .duckdb / .sqlite parent AND only when that
# process is idle; excluding them makes the archive smaller and easier
# to reason about.
_SKIP_SUFFIXES = {".wal", ".shm"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--include-secrets", action="store_true",
        help="Include .env (API keys). Off by default — .env belongs in a "
             "password manager, not a shareable zip.",
    )
    ap.add_argument(
        "--output-dir", type=Path, default=ROOT / "backups",
        help="Where to write the zip (default: ./backups/).",
    )
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / f"backup-{stamp}.zip"

    targets = list(_DEFAULT_TARGETS)
    secrets: list[Path] = []
    if args.include_secrets:
        for name in _SECRET_FILES:
            p = ROOT / name
            if p.exists():
                secrets.append(p)

    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for target in targets:
            base = ROOT / target
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix in _SKIP_SUFFIXES:
                    continue
                zf.write(path, path.relative_to(ROOT))
                count += 1
        for secret in secrets:
            zf.write(secret, secret.relative_to(ROOT))
            count += 1

    print(f"[backup] wrote {archive} ({count} files)")
    if not args.include_secrets:
        print("[backup] .env NOT included — pass --include-secrets to add it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
