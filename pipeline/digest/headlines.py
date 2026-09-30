"""Live headline LLM pass — one-sentence headlines for digest section rows.

See [ADR 0016 §5.3](../../documents/archive/decisions/0016-persistent-issue-stage.md)
and [report_v2_design.md §5.3](../../documents/report_v2_design.md).

- Cache-first (via the `headlines` warehouse table).
- Uses the **assistant LLM** (global, ADR 0002) per design — this is a
  digest-time cosmetic pass, budget-capped centrally rather than
  per-product classify.
- Gracefully returns None on any failure so templates fall back to
  the canonical_title / raw item title.
- Prompt text is editable at `/admin/prompts` under the "Assistant LLM
  (digest v2)" bucket — the two templates registered in Slice 3a.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

import structlog
from pydantic import BaseModel, Field

from pipeline import assistant_llm, prompt_templates, storage

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Structured LLM output
# ---------------------------------------------------------------------------


class HeadlineResponse(BaseModel):
    """LLM output — one headline string."""

    headline: str = Field(
        description="One sentence, 12-18 words, no trailing period.",
    )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _content_hash(title: str, body: str, source_display_name: str, summary: str) -> str:
    """Stable hash of the item content that feeds the prompt."""
    h = hashlib.sha256()
    for part in (title or "", (body or "")[:2000], source_display_name or "", summary or ""):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _prompt_hash() -> str:
    """Hash of the current prompt templates. Prompt edits at /admin/prompts
    invalidate cache automatically because this hash changes.
    """
    system = prompt_templates.get("assistant_digest_headline_system")
    template = prompt_templates.get("assistant_digest_headline_template")
    h = hashlib.sha256()
    h.update(system.encode("utf-8"))
    h.update(b"\0")
    h.update(template.encode("utf-8"))
    return h.hexdigest()[:16]


def _cache_key(content_hash: str, prompt_hash: str, model: str) -> str:
    """Composite key: content + prompt version + model. Any change invalidates."""
    return f"{content_hash}:{prompt_hash}:{model}"[:120]


def _get_cached(cache_key: str) -> Optional[str]:
    rows = storage.query(
        "SELECT headline FROM headlines WHERE cache_key = ?", [cache_key],
    )
    return rows[0]["headline"] if rows else None


def _put_cached(cache_key: str, item_id: str, headline: str, model: str) -> None:
    storage.execute(
        "INSERT INTO headlines(cache_key, item_id, headline, model, generated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT (cache_key) DO UPDATE SET "
        "headline=excluded.headline, model=excluded.model, "
        "generated_at=excluded.generated_at",
        [cache_key, item_id, headline, model, datetime.now(timezone.utc)],
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _assistant_contract():
    """LLMResponseContract wired to the assistant LLM. Mirrors the trick
    used in pipeline/profile_draft.py so structured output + retry-with-
    feedback flow through the same path as classify/relevance.
    """
    from pipeline.llm_contract import LLMResponseContract
    try:
        client = assistant_llm.client()
    except RuntimeError:
        return None
    contract = LLMResponseContract.__new__(LLMResponseContract)
    contract._client = client
    contract.role = "assistant"
    contract.model = client.model
    contract.endpoint = client.endpoint
    return contract


def generate_headline(
    *,
    item_id: str,
    title: str,
    body: str,
    source_display_name: str,
    summary: str = "",
    product_id: str = "",
) -> Optional[str]:
    """Return a cached or freshly-generated headline. None on any failure.

    Failure modes (all return None; digest templates fall back to
    canonical_title):
      - assistant LLM not configured
      - product over monthly budget
      - LLM call raises
      - LLM returns an empty headline
    """
    if not assistant_llm.is_configured():
        return None

    cfg = assistant_llm.current_config()
    if cfg is None:
        return None
    model = cfg.model

    ph = _prompt_hash()
    ch = _content_hash(title, body, source_display_name, summary)
    key = _cache_key(ch, ph, model)

    cached = _get_cached(key)
    if cached is not None:
        return cached

    # Budget guard for cache misses only — a cached hit costs nothing.
    if product_id:
        within, spent, cap = assistant_llm.is_within_budget(product_id)
        if not within:
            log.warning(
                "headline_over_budget",
                product_id=product_id, spent=spent, cap=cap,
            )
            return None

    contract = _assistant_contract()
    if contract is None:
        return None

    system = prompt_templates.get("assistant_digest_headline_system")
    user_template = prompt_templates.get("assistant_digest_headline_template")
    user = user_template.format(
        source_display_name=source_display_name or "unknown",
        title=(title or "(untitled)")[:200],
        body=(body or "")[:1500],
        summary=(summary or "")[:300],
    )

    try:
        from pipeline.llm_contract import LLMCallSpec
        result = contract.call(LLMCallSpec(
            system=system,
            user=user,
            response_model=HeadlineResponse,
            cacheable_system=True,
        ))
    except Exception as e:  # pragma: no cover - never fail the digest on LLM
        log.warning("headline_generate_failed", item_id=item_id, error=str(e))
        return None

    headline = (result.headline or "").strip().rstrip(".")[:200]
    if not headline:
        return None
    _put_cached(key, item_id, headline, model)
    return headline


def generate_batch(items: list[dict], *, product_id: str = "") -> dict[str, str]:
    """Best-effort batch: {item_id -> headline}. Missing keys mean skipped.

    Sequential in v1 to keep behavior predictable + honor rate limits;
    a future slice can add bounded concurrency once we've observed the
    typical assistant-LLM latency in production.
    """
    out: dict[str, str] = {}
    for it in items:
        item_id = it.get("item_id") or it.get("id")
        if not item_id:
            continue
        h = generate_headline(
            item_id=item_id,
            title=it.get("title") or "",
            body=it.get("body") or "",
            source_display_name=it.get("source_display_name") or "",
            summary=it.get("summary") or "",
            product_id=product_id,
        )
        if h:
            out[item_id] = h
    return out
