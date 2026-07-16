"""Prompt versioning + history (POST_V1_PLAN §4.5, D19 — reproducibility).

Every prompt file gets an `id` and monotonically-increasing `version`.
When the user applies a suggestion, we bump the version and copy the
previous file to `products/<pid>/prompts.yaml.history/v<N>.yaml` so
the old prompt can be reproduced later (e.g. re-running eval against
a historical prompt to compare).

Migration: existing prompts.yaml files without id/version get
`id: <slug>` and `version: 1` retrofitted at first load. Idempotent.

The versioning system is INDEPENDENT of the classify LLM's `seed` and
`temperature`. Two runs with the same prompt version can still produce
different outputs if the model changed under us — but we can still
prove "these two runs used the same prompt text."
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "prompts"


def ensure_versioned(product_dir: Path) -> dict[str, Any]:
    """Read products/<pid>/prompts.yaml, retrofit id + version if missing,
    and return the (possibly-updated) blob. Idempotent.

    New shape:
      id: <slug>
      version: <int>          # monotonic; starts at 1
      updated_at: <ISO>       # last edit
      relevance: {...}
      classify: {...}
    """
    path = product_dir / "prompts.yaml"
    if not path.exists():
        return {}

    blob = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    changed = False
    if "id" not in blob or not blob.get("id"):
        blob["id"] = _slug(product_dir.name)
        changed = True
    if "version" not in blob or not isinstance(blob.get("version"), int):
        blob["version"] = 1
        changed = True
    if "updated_at" not in blob:
        blob["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changed = True

    if changed:
        _atomic_write(path, blob)
    return blob


def bump_version(product_dir: Path, new_blob: dict[str, Any]) -> int:
    """Persist `new_blob` as the new prompts.yaml, archiving the previous
    version to `prompts.yaml.history/v<N>.yaml`.

    Returns the new version number. Idempotent-ish: two rapid calls both
    bump and both archive; the on-disk history is complete.
    """
    path = product_dir / "prompts.yaml"
    history_dir = product_dir / "prompts.yaml.history"
    history_dir.mkdir(parents=True, exist_ok=True)

    prev = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    prev_version = int(prev.get("version") or 0)

    if prev:
        archive = history_dir / f"v{prev_version}.yaml"
        # If someone else already archived this exact version, don't clobber.
        if not archive.exists():
            _atomic_write(archive, prev)

    new_blob = dict(new_blob)
    new_blob["id"] = new_blob.get("id") or prev.get("id") or _slug(product_dir.name)
    new_blob["version"] = prev_version + 1
    new_blob["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write(path, new_blob)
    return new_blob["version"]


def list_history(product_dir: Path) -> list[dict[str, Any]]:
    """Return every archived version, oldest first, with metadata."""
    hdir = product_dir / "prompts.yaml.history"
    if not hdir.exists():
        return []
    versions = []
    for f in sorted(hdir.glob("v*.yaml")):
        try:
            n = int(f.stem.lstrip("v"))
        except ValueError:
            continue
        try:
            blob = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            blob = {}
        versions.append({
            "version": n,
            "path": f,
            "updated_at": blob.get("updated_at"),
            "blob": blob,
        })
    return versions


def load_version(product_dir: Path, version: int) -> Optional[dict[str, Any]]:
    """Return the archived version's full blob, or None if not present."""
    path = product_dir / "prompts.yaml.history" / f"v{version}.yaml"
    if not path.exists():
        # Maybe the caller asked for the current version:
        current = product_dir / "prompts.yaml"
        if current.exists():
            cur_blob = yaml.safe_load(current.read_text(encoding="utf-8")) or {}
            if int(cur_blob.get("version") or 0) == version:
                return cur_blob
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def _atomic_write(path: Path, blob: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(blob, sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(path)
