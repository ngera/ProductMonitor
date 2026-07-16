"""Feature flag helper (ADR-0006).

Every post-V1 capability ships behind a flag in `config/features.yaml`,
off by default. Product-level overrides in `products/<pid>/features.yaml`
take precedence.

Reads are cheap (small YAML, cached). Users can hot-reload by calling
`clear_cache()` — the webui does this after saves.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from pipeline.config import CONFIG_DIR
from pipeline.product import PRODUCTS_DIR

_FEATURES_YAML = CONFIG_DIR / "features.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _global_features() -> dict[str, bool]:
    data = _load_yaml(_FEATURES_YAML)
    return data.get("features") or {}


@lru_cache(maxsize=32)
def _product_features(product_id: str) -> dict[str, bool]:
    path = PRODUCTS_DIR / product_id / "features.yaml"
    data = _load_yaml(path)
    return data.get("features") or {}


def enabled(flag: str, product_id: Optional[str] = None) -> bool:
    """Return True if the flag is enabled for the given product.

    Precedence:
      1. Product-level override in `products/<pid>/features.yaml`
      2. Global default in `config/features.yaml`
      3. Code default (False)
    """
    if product_id:
        product_val = _product_features(product_id).get(flag)
        if product_val is not None:
            return bool(product_val)
    global_val = _global_features().get(flag)
    if global_val is not None:
        return bool(global_val)
    return False


def all_flags(product_id: Optional[str] = None) -> dict[str, bool]:
    """Return the effective flag map for a product (or global), useful for
    diagnostics and the UI."""
    result = dict(_global_features())
    if product_id:
        result.update(_product_features(product_id))
    return {k: bool(v) for k, v in result.items()}


def clear_cache() -> None:
    """Drop cached flag data. Call after config edits so subsequent reads
    see fresh values without a restart."""
    _global_features.cache_clear()
    _product_features.cache_clear()
