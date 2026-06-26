"""Config loading. Hand-edited YAML in V1 (DESIGN.md §6)."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _version_hash(name: str) -> str:
    """Stable hash of a config file's bytes; recorded in runs/rollups."""
    path = CONFIG_DIR / name
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=1)
def app_config() -> dict[str, Any]:
    return _load_yaml("app.yaml")


@lru_cache(maxsize=1)
def sources_config() -> dict[str, Any]:
    return _load_yaml("sources.yaml")


@lru_cache(maxsize=1)
def taxonomy_config() -> dict[str, Any]:
    return _load_yaml("taxonomy.yaml")


@lru_cache(maxsize=1)
def vendors_config() -> dict[str, Any]:
    return _load_yaml("vendors.yaml")


def taxonomy_version() -> str:
    """Declared version string preferred; fall back to content hash."""
    return str(taxonomy_config().get("version") or _version_hash("taxonomy.yaml"))


def vendors_version() -> str:
    return str(vendors_config().get("version") or _version_hash("vendors.yaml"))


def enabled_areas() -> list[dict[str, Any]]:
    return [a for a in taxonomy_config().get("areas", []) if a.get("enabled", True)]


def area_ids() -> list[str]:
    return [a["id"] for a in enabled_areas()]


def entity_type_to_area() -> dict[str, str]:
    """Map an entity `type` -> area id, from taxonomy entity_type_hint (§4.8.1).

    First-declared area wins on conflict (stable, config-driven).
    """
    mapping: dict[str, str] = {}
    for area in enabled_areas():
        for t in area.get("entity_type_hint", []) or []:
            mapping.setdefault(t, area["id"])
    return mapping


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_path(p: str) -> Path:
    """Resolve a config path relative to project root."""
    path = Path(p)
    return path if path.is_absolute() else (project_root() / path)
