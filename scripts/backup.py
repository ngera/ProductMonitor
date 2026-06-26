"""Backup script (DESIGN.md §11.7).

Zips data/ + reports/ + config/ into a dated archive under backups/.

    python scripts/backup.py
"""

from __future__ import annotations

import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["data", "reports", "config"]
SKIP_SUFFIXES = {".wal"}


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups = ROOT / "backups"
    backups.mkdir(exist_ok=True)
    archive = backups / f"backup-{stamp}.zip"

    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for target in TARGETS:
            base = ROOT / target
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_file() and path.suffix not in SKIP_SUFFIXES:
                    zf.write(path, path.relative_to(ROOT))
                    count += 1
    print(f"[backup] wrote {archive} ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
