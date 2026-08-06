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


# ---------------------------------------------------------------------------
# Cross-product aggregation (Admin > Token usage tracker)
# ---------------------------------------------------------------------------


# Prefix-match endpoint → provider label. Ordered because "openai.com" would
# match Azure OpenAI too; put more specific matches first.
_PROVIDER_MAP = (
    (".anthropic.com",  "Anthropic"),
    ("api.openai.com",  "OpenAI"),
    ("azure.com",       "Azure OpenAI"),
    ("googleapis.com",  "Google"),
    ("mistral.ai",      "Mistral"),
    ("localhost:5273",  "Foundry Local"),
    ("127.0.0.1:5273",  "Foundry Local"),
    ("localhost:11434", "Ollama"),
    ("127.0.0.1:11434", "Ollama"),
    ("localhost:",      "Local (other)"),
    ("127.0.0.1:",      "Local (other)"),
)

# Stages that call the assistant LLM (ADR-0002 global connection) vs the
# per-product classify/relevance LLM. Anything not classified is "other".
_ASSISTANT_STAGES = frozenset({
    "digest", "digest_headline", "headlines",
    "assistant_wizard",
    "wizard_scope", "wizard_taxonomy", "wizard_prompts", "wizard_snippets",
    "profile_draft", "taxonomy_proposal", "stream_suggestions",
    "prompt_suggestions", "rationale",
})


def provider_from_endpoint(endpoint: str) -> str:
    """Best-effort provider label from endpoint URL. 'Unknown' when blank,
    'Other' when the URL doesn't match any known prefix."""
    if not endpoint:
        return "Unknown"
    e = endpoint.lower()
    for needle, name in _PROVIDER_MAP:
        if needle in e:
            return name
    return "Other"


def role_from_stage(stage: str) -> str:
    """assistant / classify / relevance / other. Rough categorization of
    which LLM role paid for the call."""
    if not stage:
        return "other"
    if stage in _ASSISTANT_STAGES:
        return "assistant"
    if stage == "classify":
        return "classify"
    if stage == "relevance":
        return "relevance"
    return "other"


# --- Cross-product cache -----------------------------------------------------
#
# 60-second TTL keyed on the aggregation parameters. Rebuilds cheap for
# 5-10 products, so a per-request scan is fine even without this — the
# cache just blunts repeat clicks (e.g. changing a filter).

import time as _time
import threading as _threading

_CROSS_CACHE: dict[tuple, tuple[float, Any]] = {}
_CROSS_CACHE_LOCK = _threading.Lock()
_CROSS_CACHE_TTL_S = 60.0


def _cache_get(key: tuple) -> Optional[Any]:
    with _CROSS_CACHE_LOCK:
        entry = _CROSS_CACHE.get(key)
        if entry is None:
            return None
        ts, val = entry
        if _time.monotonic() - ts > _CROSS_CACHE_TTL_S:
            _CROSS_CACHE.pop(key, None)
            return None
        return val


def _cache_put(key: tuple, val: Any) -> None:
    with _CROSS_CACHE_LOCK:
        _CROSS_CACHE[key] = (_time.monotonic(), val)


def clear_cross_cache() -> None:
    """Purge the cross-product aggregation cache. Callers can invoke this
    when they know a run just finished and want fresh data."""
    with _CROSS_CACHE_LOCK:
        _CROSS_CACHE.clear()


# --- Warehouse iteration -----------------------------------------------------


def _warehouse_paths() -> list[tuple[str, Path]]:
    """Return [(product_id, warehouse_path)] for every product that has a
    warehouse on disk. Non-existent warehouses are silently skipped so
    freshly-scaffolded products don't crash the admin page."""
    from pipeline.config import app_config, resolve_path
    from pipeline.product import available_products
    data_root = resolve_path(app_config()["paths"]["data_root"])
    out: list[tuple[str, Path]] = []
    for pid in available_products():
        p = data_root / pid / "warehouse.duckdb"
        if p.exists():
            out.append((pid, p))
    return out


def _fetch_raw_rows(
    since: datetime, until: datetime,
    product_filter: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch the raw llm_usage rows across every product's warehouse in
    [since, until). Only reads columns needed downstream — keeps memory
    small on wide windows.

    `product_filter`, when set, restricts to a single product (skips other
    warehouses entirely).
    """
    import duckdb
    out: list[dict[str, Any]] = []
    for pid, wpath in _warehouse_paths():
        if product_filter and pid != product_filter:
            continue
        try:
            con = duckdb.connect(str(wpath), read_only=True)
        except Exception:
            continue
        # try/finally so `con` closes on EVERY branch — including the
        # "no llm_usage table" continue path. Prior version leaked
        # connections for tableless warehouses, and DuckDB blocks OTHER
        # processes (e.g. a pipeline subprocess) from opening any warehouse
        # this process still holds open. Symptom was:
        # "IO Error: Could not set lock on file .../warehouse.duckdb:
        #  Conflicting lock is held in /usr/local/bin/python3.11 (PID 1)."
        try:
            # Some warehouses may not have llm_usage yet (fresh products).
            has = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name='llm_usage'"
            ).fetchone()
            if not (has and has[0]):
                continue
            rows = con.execute(
                """SELECT ts, run_id, stage, source_id, product_id, endpoint,
                          model, prompt_tokens, completion_tokens,
                          cached_input_tokens, total_tokens
                   FROM llm_usage
                   WHERE ts >= ? AND ts < ?""",
                [since, until],
            ).fetchall()
            # The product_id column exists in every row but may be blank on
            # early runs; prefer the warehouse's known product_id.
            for r in rows:
                out.append({
                    "ts": r[0],
                    "run_id": r[1] or "",
                    "stage": r[2] or "",
                    "source_id": r[3] or "",
                    "product_id": r[4] or pid,
                    "endpoint": r[5] or "",
                    "model": r[6] or "",
                    "prompt_tokens": int(r[7] or 0),
                    "completion_tokens": int(r[8] or 0),
                    "cached_input_tokens": int(r[9] or 0),
                    "total_tokens": int(r[10] or 0),
                })
        except Exception:
            # Silent — one bad warehouse doesn't fail the whole page.
            pass
        finally:
            try:
                con.close()
            except Exception:
                pass
    return out


# --- Time bucketing ----------------------------------------------------------


def _bucket_key(ts: datetime, bucket: str) -> str:
    """Format a datetime as a bucket label. day='YYYY-MM-DD', week='YYYY-Www',
    month='YYYY-MM'."""
    if not isinstance(ts, datetime):
        # DuckDB may hand back a python datetime or a date; coerce.
        try:
            ts = datetime.fromisoformat(str(ts))
        except Exception:
            return ""
    if bucket == "day":
        return ts.strftime("%Y-%m-%d")
    if bucket == "week":
        iso = ts.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if bucket == "month":
        return ts.strftime("%Y-%m")
    return ts.strftime("%Y-%m-%d")


# --- Public aggregation API --------------------------------------------------


def cross_product_totals(
    since: datetime,
    until: datetime,
    *,
    group_by: tuple[str, ...] = ("product_id",),
    filters: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Aggregate llm_usage across every product's warehouse in [since, until).

    Supported group_by axes:
      day | week | month           — time bucket
      product_id | provider | model | stage | role

    Filters (all optional, exact-match string):
      product_id, provider, stage, role, model

    Returns:
      {
        "series": [{group_by_key1: ..., ..., "tokens": N, "calls": N,
                     "cost_usd": F, "prompt_tokens": N, ...}, ...],
        "totals": {"tokens": ..., "cost_usd": ..., "calls": ...,
                    "products": N, "prior_tokens": N, "prior_cost_usd": F},
      }
    """
    filters = filters or {}
    key = (
        since.isoformat(), until.isoformat(),
        tuple(group_by), tuple(sorted(filters.items())),
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached

    product_filter = filters.get("product_id")
    raw = _fetch_raw_rows(since, until, product_filter=product_filter)

    # Derive virtual columns + apply non-product filters in Python.
    pricing = _load_pricing()

    def _cost(row: dict[str, Any]) -> float:
        return _cost_for_row(row, pricing)

    enriched: list[dict[str, Any]] = []
    for row in raw:
        row["provider"] = provider_from_endpoint(row["endpoint"])
        row["role"] = role_from_stage(row["stage"])
        row["cost_usd"] = _cost(row)
        if filters.get("provider") and row["provider"] != filters["provider"]:
            continue
        if filters.get("stage") and row["stage"] != filters["stage"]:
            continue
        if filters.get("role") and row["role"] != filters["role"]:
            continue
        if filters.get("model") and row["model"] != filters["model"]:
            continue
        enriched.append(row)

    # Bucket key extractor per axis.
    def _key(row: dict[str, Any], axis: str) -> str:
        if axis in ("day", "week", "month"):
            return _bucket_key(row["ts"], axis)
        return str(row.get(axis) or "")

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in enriched:
        k = tuple(_key(row, a) for a in group_by)
        agg = grouped.setdefault(k, {
            "tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cached_input_tokens": 0, "cost_usd": 0.0, "calls": 0,
        })
        agg["tokens"] += row["total_tokens"]
        agg["prompt_tokens"] += row["prompt_tokens"]
        agg["completion_tokens"] += row["completion_tokens"]
        agg["cached_input_tokens"] += row["cached_input_tokens"]
        agg["cost_usd"] += row["cost_usd"]
        agg["calls"] += 1

    series = []
    for k, agg in grouped.items():
        entry = {axis: k[i] for i, axis in enumerate(group_by)}
        entry.update(agg)
        entry["cost_usd"] = round(entry["cost_usd"], 4)
        series.append(entry)
    # Stable sort — time buckets ascending, tokens descending for other axes.
    if any(a in ("day", "week", "month") for a in group_by):
        # sort by the first time-like axis
        time_axis = next(a for a in group_by if a in ("day", "week", "month"))
        series.sort(key=lambda e: (e[time_axis], -e["tokens"]))
    else:
        series.sort(key=lambda e: -e["tokens"])

    totals_tokens = sum(e["tokens"] for e in series)
    totals_cost = round(sum(e["cost_usd"] for e in series), 4)
    totals_calls = sum(e["calls"] for e in series)
    distinct_products = len({r["product_id"] for r in enriched})

    # Prior-window delta: same length window immediately preceding [since, until).
    span = until - since
    prior_since = since - span
    prior_raw = _fetch_raw_rows(prior_since, since, product_filter=product_filter)
    prior_tokens = 0
    prior_cost = 0.0
    for row in prior_raw:
        row["provider"] = provider_from_endpoint(row["endpoint"])
        row["role"] = role_from_stage(row["stage"])
        if filters.get("provider") and row["provider"] != filters["provider"]:
            continue
        if filters.get("stage") and row["stage"] != filters["stage"]:
            continue
        if filters.get("role") and row["role"] != filters["role"]:
            continue
        if filters.get("model") and row["model"] != filters["model"]:
            continue
        prior_tokens += row["total_tokens"]
        prior_cost += _cost(row)

    result = {
        "series": series,
        "totals": {
            "tokens": totals_tokens,
            "cost_usd": totals_cost,
            "calls": totals_calls,
            "products": distinct_products,
            "prior_tokens": prior_tokens,
            "prior_cost_usd": round(prior_cost, 4),
        },
    }
    _cache_put(key, result)
    return result


def raw_rows_for_csv(
    since: datetime, until: datetime,
    filters: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Row-per-call export for the CSV download. Same filter semantics as
    cross_product_totals. Adds `provider`, `role`, `cost_usd` virtual
    columns per row."""
    filters = filters or {}
    raw = _fetch_raw_rows(since, until, product_filter=filters.get("product_id"))
    pricing = _load_pricing()
    out: list[dict[str, Any]] = []
    for row in raw:
        row["provider"] = provider_from_endpoint(row["endpoint"])
        row["role"] = role_from_stage(row["stage"])
        if filters.get("provider") and row["provider"] != filters["provider"]:
            continue
        if filters.get("stage") and row["stage"] != filters["stage"]:
            continue
        if filters.get("role") and row["role"] != filters["role"]:
            continue
        if filters.get("model") and row["model"] != filters["model"]:
            continue
        row["cost_usd"] = round(_cost_for_row(row, pricing), 6)
        out.append(row)
    return out


def _cost_for_row(row: dict[str, Any], pricing: dict) -> float:
    """Per-row cost estimate. Isolated so cross_product_totals and
    raw_rows_for_csv share the same pricing lookup path."""
    model = row.get("model") or ""
    if model not in pricing:
        return 0.0
    p = pricing[model]
    input_per_m = float(p.get("input_per_million") or 0.0)
    output_per_m = float(p.get("output_per_million") or 0.0)
    cached_per_m = float(p.get("cached_input_per_million") or input_per_m)
    prompt = row["prompt_tokens"] - row["cached_input_tokens"]
    cost = (
        prompt * input_per_m / 1_000_000.0
        + row["completion_tokens"] * output_per_m / 1_000_000.0
        + row["cached_input_tokens"] * cached_per_m / 1_000_000.0
    )
    return cost


def known_facets(
    since: datetime, until: datetime,
) -> dict[str, list[str]]:
    """Return distinct filter-facet values across all products in the window.
    Used to populate the admin page's filter dropdowns."""
    raw = _fetch_raw_rows(since, until)
    products = sorted({r["product_id"] for r in raw if r["product_id"]})
    providers = sorted({provider_from_endpoint(r["endpoint"]) for r in raw})
    stages = sorted({r["stage"] for r in raw if r["stage"]})
    models = sorted({r["model"] for r in raw if r["model"]})
    roles = ["assistant", "classify", "relevance", "other"]
    return {
        "products": products,
        "providers": providers,
        "stages": stages,
        "models": models,
        "roles": roles,
    }
