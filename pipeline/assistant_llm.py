"""Assistant LLM — the global-scope LLM used by post-V1 features
(POST_V1_PLAN §4.8, ADR-0002).

The assistant LLM is distinct from per-product routing:
- Configured once in `config/assistant_llm.yaml` (edited via /connections/assistant_llm)
- Used by the wizard (before any product exists), snippet candidates,
  and prompt suggestions
- Own budget cap in USD per product per month, enforced against the
  llm_usage warehouse table (see pipeline/token_usage.py)

Callers should:
1. `is_configured()` — check the connection exists
2. `is_within_budget(product_id)` — check the monthly cap isn't exceeded
3. `client()` — get an LLMClient pointed at the assistant endpoint

All calls made through the assistant client are attributed with
`stage='assistant_<use_case>'` so per-use aggregation is possible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

from pipeline.config import CONFIG_DIR

log = logging.getLogger(__name__)

_CONFIG_PATH = CONFIG_DIR / "assistant_llm.yaml"


@dataclass
class AssistantLLMConfig:
    """Persisted assistant-LLM connection settings."""

    endpoint: str
    model: str
    temperature: float = 0.2
    seed: Optional[int] = 42
    timeout_seconds: int = 60
    max_retries: int = 3
    budget_usd_per_product_per_month: float = 10.0


def _load_config() -> Optional[AssistantLLMConfig]:
    if not _CONFIG_PATH.exists():
        return None
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not data.get("endpoint") or not data.get("model"):
        return None
    return AssistantLLMConfig(
        endpoint=data["endpoint"],
        model=data["model"],
        temperature=float(data.get("temperature", 0.2)),
        seed=data.get("seed"),
        timeout_seconds=int(data.get("timeout_seconds", 60)),
        max_retries=int(data.get("max_retries", 3)),
        budget_usd_per_product_per_month=float(
            data.get("budget_usd_per_product_per_month", 10.0)
        ),
    )


def save_config(cfg: AssistantLLMConfig) -> None:
    """Persist the assistant-LLM config. Atomic write with .bak backup."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".tmp")
    payload = {
        "endpoint": cfg.endpoint,
        "model": cfg.model,
        "temperature": cfg.temperature,
        "seed": cfg.seed,
        "timeout_seconds": cfg.timeout_seconds,
        "max_retries": cfg.max_retries,
        "budget_usd_per_product_per_month": cfg.budget_usd_per_product_per_month,
    }
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    if _CONFIG_PATH.exists():
        backup = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".bak")
        _CONFIG_PATH.replace(backup)
    tmp.replace(_CONFIG_PATH)


def is_configured() -> bool:
    """True if config/assistant_llm.yaml has usable endpoint + model."""
    return _load_config() is not None


def current_config() -> Optional[AssistantLLMConfig]:
    return _load_config()


# ---------------------------------------------------------------------------
# Budget enforcement (ADR-0002 D22)
# ---------------------------------------------------------------------------


def month_to_date_spend(product_id: str) -> float:
    """Sum estimated USD spent by assistant_* stages on this product this
    calendar month. Returns 0.0 if the warehouse or table doesn't exist."""
    try:
        from pipeline import storage, token_usage
        token_usage._ensure_llm_usage_table(storage)
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        with storage.warehouse() as con:
            rows = con.execute(
                """SELECT model,
                          SUM(prompt_tokens) AS pin,
                          SUM(completion_tokens) AS pout,
                          SUM(cached_input_tokens) AS pcached
                    FROM llm_usage
                    WHERE product_id = ?
                      AND stage LIKE 'assistant_%'
                      AND ts >= ?
                    GROUP BY model""",
                [product_id, month_start],
            ).fetchall()
    except Exception:
        return 0.0

    total = 0.0
    from pipeline.token_usage import estimate_cost_usd
    for model, pin, pout, pcached in rows:
        cost = estimate_cost_usd(
            model=model,
            prompt_tokens=int(pin or 0),
            completion_tokens=int(pout or 0),
            cached_input_tokens=int(pcached or 0),
        )
        if cost is not None:
            total += cost
    return round(total, 4)


def is_within_budget(product_id: str) -> tuple[bool, float, float]:
    """Check if the assistant LLM is within budget for this product.

    Returns (within_budget, spent_this_month, cap). If no cap is set
    (0.0), always within budget. If assistant LLM not configured, returns
    (True, 0, 0)."""
    cfg = _load_config()
    if cfg is None:
        return (True, 0.0, 0.0)
    if cfg.budget_usd_per_product_per_month <= 0:
        return (True, 0.0, 0.0)
    spent = month_to_date_spend(product_id)
    return (spent < cfg.budget_usd_per_product_per_month, spent, cfg.budget_usd_per_product_per_month)


# ---------------------------------------------------------------------------
# LLM client factory
# ---------------------------------------------------------------------------


def client():
    """Return an LLMClient configured for the assistant endpoint.

    Raises RuntimeError if not configured — callers should check
    is_configured() first.
    """
    cfg = _load_config()
    if cfg is None:
        raise RuntimeError(
            "Assistant LLM not configured. Set endpoint + model in "
            "config/assistant_llm.yaml or via /connections/assistant_llm."
        )

    # Reuse the pipeline's LLMClient infrastructure — same endpoint
    # resolution, same api_key logic. The 'role' arg maps to a slot in
    # per-product llm_routing.yaml, but we override cfg here so the routing
    # lookup is bypassed.
    from pipeline.llm import LLMClient

    # A little hackery to instantiate LLMClient with our config directly.
    # LLMClient.__init__ looks in current_product().llm_routing first, then
    # app_config().llm; we build an object bypassing both.
    inst = LLMClient.__new__(LLMClient)
    import os
    from openai import OpenAI
    from pipeline.llm import _resolve_api_key

    inst.role = "assistant"
    inst.cfg = {
        "model": cfg.model,
        "endpoint": cfg.endpoint,
        "temperature": cfg.temperature,
        "seed": cfg.seed,
        "timeout_seconds": cfg.timeout_seconds,
        "max_retries": cfg.max_retries,
    }
    inst.model = cfg.model
    inst.endpoint = cfg.endpoint
    api_key = _resolve_api_key(inst.cfg, dict(os.environ))
    inst._client = OpenAI(
        base_url=cfg.endpoint,
        api_key=api_key,
        timeout=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
    )
    return inst
