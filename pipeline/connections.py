"""Global connection state — the "pause" toggle for source types.

Stored at `config/connections.yaml`. Kept separate from `app.yaml` because
app.yaml is for pipeline tuning knobs (edited via the /admin/tuning form),
whereas connection state is runtime-y — flipped from the /connections page
during operations.

Schema:
    paused:
      <source_type>: true
      <source_type>: false     # or omit; missing == not paused

Precedence: a source type paused here is skipped for every product, even
if that product has an instance configured with `paused: false`. See
`pipeline/fetch.py` for the check point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pipeline.config import CONFIG_DIR

_CONNECTIONS_YAML = CONFIG_DIR / "connections.yaml"


def _load() -> dict[str, Any]:
    if not _CONNECTIONS_YAML.exists():
        return {}
    try:
        with _CONNECTIONS_YAML.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _save(state: dict[str, Any]) -> None:
    _CONNECTIONS_YAML.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONNECTIONS_YAML.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(state, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    if _CONNECTIONS_YAML.exists():
        backup = _CONNECTIONS_YAML.with_suffix(".yaml.bak")
        _CONNECTIONS_YAML.replace(backup)
    tmp.replace(_CONNECTIONS_YAML)


def paused_types() -> set[str]:
    """Return the set of source-type names currently paused globally.

    Cheap enough to call once per fetch run — the file is small and we want
    fresh state (so pausing during a live webui session takes effect on the
    next run without a webui restart).
    """
    state = _load()
    paused = state.get("paused") or {}
    return {t for t, is_paused in paused.items() if bool(is_paused)}


def is_paused(source_type: str) -> bool:
    return source_type in paused_types()


def set_paused(source_type: str, paused: bool) -> None:
    """Flip the global pause state for a source type. No-op if already set."""
    state = _load()
    cur = state.get("paused") or {}
    if bool(cur.get(source_type)) == bool(paused):
        return
    cur[source_type] = bool(paused)
    state["paused"] = cur
    _save(state)
