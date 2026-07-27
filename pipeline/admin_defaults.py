"""Admin-set global defaults.

Small helpers for choices the admin makes once and every new product
inherits. Currently just the default LLM provider — the one that gets
pre-selected on the wizard's Review screen.

Stored in `config/admin_defaults.yaml` (a tiny file — keep it out of the
kitchen-sink `config/app.yaml` so operators can find it). If the file is
missing, `default_llm_provider()` returns None and the wizard uses its
usual ordering.
"""

from __future__ import annotations

import os
import random
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from pipeline.config import CONFIG_DIR

_DEFAULTS_YAML = CONFIG_DIR / "admin_defaults.yaml"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not _DEFAULTS_YAML.exists():
        return {}
    try:
        return yaml.safe_load(_DEFAULTS_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def default_llm_provider() -> Optional[str]:
    """Return the currently-set default LLM provider id (e.g. 'anthropic'),
    or None when no default has been set. Callers use this only for
    ordering — never to gate features, since the admin may pick a
    provider whose key gets removed later."""
    val = _load().get("default_llm_provider")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def set_default_llm_provider(provider_id: Optional[str]) -> None:
    """Persist the choice. Passing None or empty string clears the setting
    (default_llm_provider() will then return None)."""
    data = dict(_load())
    if not provider_id:
        data.pop("default_llm_provider", None)
    else:
        data["default_llm_provider"] = provider_id.strip()
    _atomic_write(data)
    clear_cache()


def _atomic_write(data: dict) -> None:
    """Windows-safe atomic write — retries on AV-lock PermissionError."""
    _DEFAULTS_YAML.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(data, sort_keys=False, default_flow_style=False,
                              allow_unicode=True)
    last_err: Optional[Exception] = None
    for attempt in range(6):
        tmp = _DEFAULTS_YAML.with_suffix(
            f"{_DEFAULTS_YAML.suffix}.{os.getpid()}.{random.randint(0, 1_000_000):06d}.tmp"
        )
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, _DEFAULTS_YAML)
            return
        except PermissionError as e:
            last_err = e
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            time.sleep(min(0.02 * (2 ** attempt), 0.5))
    raise PermissionError(
        f"could not save admin defaults to {_DEFAULTS_YAML} "
        f"after 6 retries (last: {last_err!r})"
    )


def clear_cache() -> None:
    _load.cache_clear()
