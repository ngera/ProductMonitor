"""Report rationale + highlights (POST_V1_PLAN §4.7, ADR-0003).

Separate on-demand pass at report render time, NOT part of classify.
Rationale:
  - Only report items need rationale — running it during classify would
    pay for items that later get dropped by grouping/deduplication.
  - Report re-renders (prompt tweaks, model swaps) can re-generate
    without re-classifying everything.
  - Users who don't want rationale get zero LLM cost from this feature.

Cache key covers `(item_id, classify_prompt, model, model_version)` so
that when *either* the prompt or the model changes, cache misses and
rationale regenerates.

Feature-flagged by `features.rationale_enabled`. Off by default. When
off, `run_render` skips this stage entirely; when on, cache hits are
free and cache misses run one classify-LLM call per report item.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field

from pipeline.config import app_config, resolve_path

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class RationaleResponse(BaseModel):
    """LLM output for one item's rationale + highlights."""

    rationale: str = Field(
        description="One sentence explaining why this item is in the report. <= 200 chars.",
    )
    highlights: list[str] = Field(
        description="2-3 short bullets summarizing the key claims / observations.",
        min_length=0,
        max_length=5,
    )


_RATIONALE_SYSTEM = (
    "You annotate items for a customer-feedback report. For each item, "
    "you return a one-sentence rationale explaining why it is in the "
    "report and 2-3 highlight bullets summarizing the substantive claims. "
    "Focus on the user-visible content — do not editorialize."
)


# ---------------------------------------------------------------------------
# Cache — one JSON file per (product, week, cache-key)
# ---------------------------------------------------------------------------


def _rationale_root(product_id: str) -> Path:
    return resolve_path(app_config()["paths"]["data_root"]) / product_id / "report_rationale"


def _cache_key(*, item_id: str, prompt_hash: str, model: str, model_version: str) -> str:
    """Deterministic content-addressed cache key. §4.7 D3."""
    payload = f"{item_id}|{prompt_hash}|{model}|{model_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _cache_path(product_id: str, week_id: str, key: str) -> Path:
    return _rationale_root(product_id) / week_id / f"{key}.json"


def prompt_hash(classify_prompt: dict[str, Any]) -> str:
    """Stable digest of the classify prompt block. Changes on any edit."""
    blob = json.dumps(classify_prompt or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_cached(
    *, product_id: str, week_id: str, item_id: str,
    prompt_hash: str, model: str, model_version: str = "",
) -> Optional[RationaleResponse]:
    key = _cache_key(item_id=item_id, prompt_hash=prompt_hash,
                     model=model, model_version=model_version)
    path = _cache_path(product_id, week_id, key)
    if not path.exists():
        return None
    try:
        return RationaleResponse.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def store_cached(
    *, product_id: str, week_id: str, item_id: str,
    prompt_hash: str, model: str, model_version: str,
    response: RationaleResponse,
) -> None:
    key = _cache_key(item_id=item_id, prompt_hash=prompt_hash,
                     model=model, model_version=model_version)
    path = _cache_path(product_id, week_id, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(response.model_dump_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@dataclass
class RationaleContext:
    """Everything needed to generate rationale for one item — pure data,
    no I/O. Passed to the LLM in a structured prompt."""

    item_id: str
    title: str
    body: str
    source_display_name: str
    primary_area: str
    summary: str


def generate(
    ctx: RationaleContext,
    *,
    product_id: str,
    week_id: str,
    prompt_hash: str,
    model: str,
    model_version: str = "",
) -> Optional[RationaleResponse]:
    """Cache-first: return cached RationaleResponse if present, else call
    the classify LLM to generate and store one.

    Uses the per-product classify LLM (§4.7 D4) via LLMResponseContract
    so retry-with-feedback + structured output apply. Returns None on
    unrecoverable errors — callers should render a fallback ("summary
    only, click to generate").
    """
    cached = load_cached(
        product_id=product_id, week_id=week_id, item_id=ctx.item_id,
        prompt_hash=prompt_hash, model=model, model_version=model_version,
    )
    if cached is not None:
        return cached

    try:
        from pipeline.llm_contract import LLMCallSpec, LLMResponseContract
        contract = LLMResponseContract("classify")
        result = contract.call(LLMCallSpec(
            system=_RATIONALE_SYSTEM,
            user=_build_user_prompt(ctx),
            response_model=RationaleResponse,
            cacheable_system=True,
        ))
    except Exception as e:
        log.warning("rationale_generate_failed", item_id=ctx.item_id, error=str(e))
        return None

    store_cached(
        product_id=product_id, week_id=week_id, item_id=ctx.item_id,
        prompt_hash=prompt_hash, model=model, model_version=model_version,
        response=result,
    )
    return result


def _build_user_prompt(ctx: RationaleContext) -> str:
    """Wrap item content in structured tags to isolate prompt-injection
    attempts in the body."""
    body = (ctx.body or "")[:2000]
    return (
        f"<item>\n"
        f"  <source>{ctx.source_display_name}</source>\n"
        f"  <area>{ctx.primary_area}</area>\n"
        f"  <title>{ctx.title or '(no title)'}</title>\n"
        f"  <body>{body}</body>\n"
        f"  <existing_summary>{ctx.summary or ''}</existing_summary>\n"
        f"</item>\n\n"
        f"Return JSON: {{\"rationale\": \"...\", \"highlights\": [\"...\", \"...\"]}}"
    )
