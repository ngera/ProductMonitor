"""Config loading.

Two layers:

  - **Global** (cross-product): `config/app.yaml` for paths, fetching
    defaults, filter thresholds, grouping/scoring/reporting knobs. Loaded
    once.

  - **Per-product**: a `ProductSpec` produced by
    `pipeline.product.load_product(product_id)`. Carries sources, taxonomy,
    prompts, llm_routing, and the composed Classification schema.

This module preserves the old function API (`sources_config()`,
`taxonomy_config()`, etc.) but delegates to the current product. The
orchestrator calls `set_current_product(...)` at the start of a run;
callers that don't yet take a ProductSpec parameter keep working by
reading from the current product.

Back-compat aliases keep the old `current_topic` / `set_current_topic`
names working until existing callers are migrated.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# --- Global (cross-product) config ------------------------------------------


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


# --- Current product (set by the orchestrator) ------------------------------

# We deliberately avoid circular-import pain by deferring the ProductSpec import.
_current_product: Optional["object"] = None  # actually ProductSpec at runtime


def set_current_product(product: "object") -> None:
    """Set the active ProductSpec for this process. Called by the orchestrator."""
    global _current_product
    _current_product = product


def current_product() -> "object":
    """Return the active ProductSpec. Lazy-loads the default product on first use
    so older entry points (eval harness, tests) that don't yet call
    `set_current_product` keep working."""
    global _current_product
    if _current_product is None:
        from pipeline.product import DEFAULT_PRODUCT, load_product

        _current_product = load_product(DEFAULT_PRODUCT)
    return _current_product


# Back-compat aliases for callers that still use the old "topic" naming.
set_current_topic = set_current_product
current_topic = current_product


# --- Per-product shims (delegate to current product) -----------------------


def sources_config() -> dict[str, Any]:
    return {"sources": current_product().sources}


def taxonomy_config() -> dict[str, Any]:
    return current_product().taxonomy


def taxonomy_version() -> str:
    return current_product().taxonomy_version


def enabled_areas() -> list[dict[str, Any]]:
    return current_product().enabled_areas()


def area_ids() -> list[str]:
    return current_product().area_ids()


def entity_type_to_area() -> dict[str, str]:
    return current_product().entity_type_to_area()


# --- Paths ------------------------------------------------------------------


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_path(p: str) -> Path:
    """Resolve a config path relative to project root."""
    path = Path(p)
    return path if path.is_absolute() else (project_root() / path)


def product_data_root(product_id: Optional[str] = None) -> Path:
    """Per-product root under data/<product_id>/.

    Falls back to the legacy `data/` location when product_id is None, so an
    upgrade that has not yet moved data still works.
    """
    base = resolve_path(app_config()["paths"]["data_root"])
    if product_id is None:
        return base
    return base / product_id


def product_reports_root(product_id: Optional[str] = None) -> Path:
    base = resolve_path(app_config()["paths"]["reports_root"])
    if product_id is None:
        return base
    return base / product_id


# Back-compat aliases.
topic_data_root = product_data_root
topic_reports_root = product_reports_root
