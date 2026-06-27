"""Config loading.

Two layers:

  - **Global** (cross-topic): `config/app.yaml` for paths, fetching defaults,
    filter thresholds, grouping/scoring/reporting knobs. Loaded once.

  - **Per-topic**: a `TopicSpec` produced by `pipeline.topic.load_topic(topic_id)`.
    Carries sources, taxonomy, vendors, prompts, llm_routing, and the composed
    Classification schema.

This module preserves the old function API (`sources_config()`, `taxonomy_config()`,
etc.) but delegates to the current topic. The orchestrator calls
`set_current_topic(...)` at the start of a run; callers that don't yet take a
TopicSpec parameter keep working by reading from the current topic.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# --- Global (cross-topic) config --------------------------------------------


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _version_hash(name: str) -> str:
    """Stable hash of a config file's bytes."""
    path = CONFIG_DIR / name
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=1)
def app_config() -> dict[str, Any]:
    return _load_yaml("app.yaml")


# --- Current topic (set by the orchestrator) --------------------------------

# We deliberately avoid circular-import pain by deferring the TopicSpec import.
_current_topic: Optional["object"] = None  # actually TopicSpec at runtime


def set_current_topic(topic: "object") -> None:
    """Set the active TopicSpec for this process. Called by the orchestrator."""
    global _current_topic
    _current_topic = topic


def current_topic() -> "object":
    """Return the active TopicSpec. Lazy-loads the default topic on first use
    so older entry points (eval harness, tests) that don't yet call
    `set_current_topic` keep working."""
    global _current_topic
    if _current_topic is None:
        from pipeline.topic import DEFAULT_TOPIC, load_topic

        _current_topic = load_topic(DEFAULT_TOPIC)
    return _current_topic


# --- Per-topic shims (delegate to current topic) ----------------------------


def sources_config() -> dict[str, Any]:
    return {"sources": current_topic().sources}


def taxonomy_config() -> dict[str, Any]:
    return current_topic().taxonomy


def vendors_config() -> dict[str, Any]:
    return current_topic().vendors


def taxonomy_version() -> str:
    return current_topic().taxonomy_version


def vendors_version() -> str:
    return current_topic().vendors_version


def enabled_areas() -> list[dict[str, Any]]:
    return current_topic().enabled_areas()


def area_ids() -> list[str]:
    return current_topic().area_ids()


def entity_type_to_area() -> dict[str, str]:
    return current_topic().entity_type_to_area()


# --- Paths ------------------------------------------------------------------


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_path(p: str) -> Path:
    """Resolve a config path relative to project root."""
    path = Path(p)
    return path if path.is_absolute() else (project_root() / path)


def topic_data_root(topic_id: Optional[str] = None) -> Path:
    """Per-topic root under data/<topic_id>/.

    Falls back to the legacy `data/` location when topic_id is None, so an
    upgrade that has not yet moved data still works.
    """
    base = resolve_path(app_config()["paths"]["data_root"])
    if topic_id is None:
        return base
    return base / topic_id


def topic_reports_root(topic_id: Optional[str] = None) -> Path:
    base = resolve_path(app_config()["paths"]["reports_root"])
    if topic_id is None:
        return base
    return base / topic_id
