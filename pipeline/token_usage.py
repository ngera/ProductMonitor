"""LLM token attribution and cost accounting (POST_V1_PLAN §4.11, ADR-0005).

Every LLM call is tagged with attribution context (run_id, stage,
source_id, item_id) via `contextvars.ContextVar`. Token counts are
extracted from the SDK response and written to the `llm_usage` warehouse
table.

Callers set attribution once (e.g., at run start, or per stage) and
LLMClient reads it on every call without needing threading context.

Usage:
    from pipeline.token_usage import set_context, TokenContext

    with set_context(TokenContext(run_id="r1", stage="classify")):
        result = llm.structured(...)
        # llm.py internally calls record_usage(...) with token counts.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml


# ---------------------------------------------------------------------------
# Attribution context (ADR-0005)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenContext:
    """Attributes attached to every LLM call while this context is active.

    Fields with empty defaults are OK — the warehouse table tolerates blanks
    for calls that aren't per-item or per-source (e.g., wizard prompts).
    """

    run_id: str = ""
    stage: str = ""           # relevance | classify | assistant_wizard | ...
    source_id: str = ""       # instance id like "reddit-1" for per-source attribution
    item_id: str = ""         # for per-item attribution during classify
    product_id: str = ""      # useful for cross-run aggregates


_current_context: contextvars.ContextVar[Optional[TokenContext]] = contextvars.ContextVar(
    "token_context", default=None,
)


@contextmanager
def set_context(ctx: TokenContext) -> Iterator[TokenContext]:
    """Push a TokenContext onto the stack. Yields the pushed context so the
    caller can pattern-match without re-holding the object.

    Nested contexts inherit missing fields from the parent:
        set_context(TokenContext(run_id='r1', stage='classify')):
            with set_context(TokenContext(item_id='hn:1234')):
                # sees run_id='r1', stage='classify', item_id='hn:1234'
    """
    parent = _current_context.get()
    if parent is not None:
        # Merge: child fields override parent's; parent fills in blanks
        merged = TokenContext(
            run_id=ctx.run_id or parent.run_id,
            stage=ctx.stage or parent.stage,
            source_id=ctx.source_id or parent.source_id,
            item_id=ctx.item_id or parent.item_id,
            product_id=ctx.product_id or parent.product_id,
        )
    else:
        merged = ctx
    token = _current_context.set(merged)
    try:
        yield merged
    finally:
        _current_context.reset(token)


def get_context() -> Optional[TokenContext]:
    """Return the currently active TokenContext (or None)."""
    return _current_context.get()


# ---------------------------------------------------------------------------
# Usage recording (writes to warehouse)
# ---------------------------------------------------------------------------


@dataclass
class UsageRecord:
    """One LLM call's token counts + attribution."""

    ts: datetime
    run_id: str
    stage: str
    source_id: str
    item_id: str
    product_id: str
    endpoint: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_input_tokens: int    # Anthropic prompt cache reads; 0 if unsupported
    total_tokens: int


def record_usage(
    *,
    endpoint: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
) -> None:
    """Record one LLM call's token counts to the warehouse.

    Attribution (run_id/stage/source_id/item_id/product_id) is pulled from
    the current context — no need to pass it explicitly. Safe to call
    without an active context; missing fields default to "".

    Best-effort: warehouse write failures are logged, not raised. Never
    let telemetry crash the pipeline.
    """
    ctx = get_context() or TokenContext()
    record = UsageRecord(
        ts=datetime.now(timezone.utc),
        run_id=ctx.run_id,
        stage=ctx.stage,
        source_id=ctx.source_id,
        item_id=ctx.item_id,
        product_id=ctx.product_id,
        endpoint=endpoint,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_input_tokens=cached_input_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    _write_to_warehouse(record)


def _write_to_warehouse(record: UsageRecord) -> None:
    """Best-effort append to the llm_usage table in the current product's
    warehouse. Silent on failure — telemetry never crashes the pipeline."""
    try:
        # Only write if we have a product context — assistant-LLM work at
        # setup time may not, and we don't want to spuriously create warehouses.
        if not record.product_id:
            return
        from pipeline import storage
        _ensure_llm_usage_table(storage)
        with storage.warehouse() as con:
            con.execute(
                """INSERT INTO llm_usage (
                    ts, run_id, stage, source_id, item_id, product_id,
                    endpoint, model, prompt_tokens, completion_tokens,
                    cached_input_tokens, total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    record.ts, record.run_id, record.stage, record.source_id,
                    record.item_id, record.product_id, record.endpoint,
                    record.model, record.prompt_tokens, record.completion_tokens,
                    record.cached_input_tokens, record.total_tokens,
                ],
            )
    except Exception:
        # Silent — telemetry must never crash a run.
        pass


_LLM_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    ts                  TIMESTAMP,
    run_id              VARCHAR,
    stage               VARCHAR,
    source_id           VARCHAR,
    item_id             VARCHAR,
    product_id          VARCHAR,
    endpoint            VARCHAR,
    model               VARCHAR,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    cached_input_tokens INTEGER,
    total_tokens        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_run ON llm_usage(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_product_ts ON llm_usage(product_id, ts);
"""


def _ensure_llm_usage_table(storage_module) -> None:
    """Create llm_usage table if missing. Idempotent, cheap."""
    with storage_module.warehouse() as con:
        con.execute(_LLM_USAGE_SCHEMA)


# ---------------------------------------------------------------------------
# Cost estimation (POST_V1_PLAN §4.11)
# ---------------------------------------------------------------------------


_PRICING_YAML = Path(__file__).resolve().parent.parent / "config" / "model_pricing.yaml"


def _load_pricing() -> dict[str, dict[str, float]]:
    """Load model pricing from config/model_pricing.yaml. Silent if missing."""
    if not _PRICING_YAML.exists():
        return {}
    try:
        data = yaml.safe_load(_PRICING_YAML.read_text(encoding="utf-8")) or {}
        return data.get("pricing") or {}
    except Exception:
        return {}


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_input_tokens: int = 0,
) -> Optional[float]:
    """Estimate USD cost using config/model_pricing.yaml.

    Framed as an estimate lower bound (assumes no cache hits by default;
    apply cache_read rate to cached_input_tokens if provided).

    Returns None if the model isn't in the pricing file — caller shows
    "tokens only" instead of a dollar figure.
    """
    pricing = _load_pricing().get(model)
    if not pricing:
        return None

    fresh_input = max(prompt_tokens - cached_input_tokens, 0)
    cost = 0.0
    cost += fresh_input * (pricing.get("input", 0.0) / 1_000_000)
    cost += cached_input_tokens * (pricing.get("cache_read", pricing.get("input", 0.0)) / 1_000_000)
    cost += completion_tokens * (pricing.get("output", 0.0) / 1_000_000)
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Aggregate queries (used by UI)
# ---------------------------------------------------------------------------


def per_run_totals(product_id: str, run_id: str) -> dict[str, Any]:
    """Return {total_tokens, prompt_tokens, completion_tokens, cached_input_tokens,
    by_stage: {stage: {tokens, calls}}, by_source: {source: {tokens, calls}}}."""
    try:
        from pipeline import storage
        _ensure_llm_usage_table(storage)
        with storage.warehouse() as con:
            totals_row = con.execute(
                """SELECT
                    COALESCE(SUM(total_tokens), 0)      AS total,
                    COALESCE(SUM(prompt_tokens), 0)     AS pin,
                    COALESCE(SUM(completion_tokens), 0) AS pout,
                    COALESCE(SUM(cached_input_tokens), 0) AS pcached,
                    COALESCE(COUNT(*), 0)               AS calls
                FROM llm_usage
                WHERE product_id = ? AND run_id = ?""",
                [product_id, run_id],
            ).fetchone()
            by_stage_rows = con.execute(
                """SELECT stage, SUM(total_tokens) AS tokens, COUNT(*) AS calls
                FROM llm_usage
                WHERE product_id = ? AND run_id = ?
                GROUP BY stage""",
                [product_id, run_id],
            ).fetchall()
            by_source_rows = con.execute(
                """SELECT source_id, SUM(total_tokens) AS tokens, COUNT(*) AS calls
                FROM llm_usage
                WHERE product_id = ? AND run_id = ? AND source_id <> ''
                GROUP BY source_id""",
                [product_id, run_id],
            ).fetchall()
    except Exception:
        return {"total_tokens": 0, "calls": 0, "by_stage": {}, "by_source": {}}

    total = totals_row[0] or 0
    return {
        "total_tokens": total,
        "prompt_tokens": totals_row[1] or 0,
        "completion_tokens": totals_row[2] or 0,
        "cached_input_tokens": totals_row[3] or 0,
        "calls": totals_row[4] or 0,
        "by_stage": {r[0]: {"tokens": r[1] or 0, "calls": r[2] or 0} for r in by_stage_rows},
        "by_source": {r[0]: {"tokens": r[1] or 0, "calls": r[2] or 0} for r in by_source_rows},
    }
