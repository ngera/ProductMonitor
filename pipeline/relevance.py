"""Relevance gate — cheap LLM call before expensive extraction (DESIGN.md §4.5).

Reads the system + template prompts from the current topic's prompts.yaml,
so a new topic just edits topics/<id>/prompts.yaml (or the UI editor) to
retarget the gate at its product.

Items with relevant=false AND confidence >= drop threshold are dropped
(logged with reason). Borderline items pass through and are re-evaluated
at Classify.

Items whose title+body never overlap the built product relevance context
(brand/aliases/host, scope_in, or theme/feature display+description tokens)
are dropped before the LLM — RSS/media noise otherwise sails through weak
local models.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from pipeline import storage
from pipeline.config import app_config, current_product
from pipeline.llm import LLMClient, LLMError
from pipeline.models import RelevanceResult
from pipeline.product_facts_prompt import (
    build_relevance_context,
    text_mentions_product_brand,
    text_mentions_product_context,
)
from pipeline.prompt_safety import SYSTEM_PROMPT_SAFETY_PREAMBLE
from pipeline.snippets import few_shot_subset, render_relevance_few_shot
from pipeline.token_usage import TokenContext, set_context
from pipeline.util import safe_error_text

log = structlog.get_logger()


def text_mentions_product(title: str, body: str, product) -> bool:
    """True when title/body overlaps brand or theme/scope context needles."""
    return text_mentions_product_context(title, body, product)


def _passes_pre_gate(item: dict, product) -> bool:
    """Deterministic pre-gate before the LLM call.

    - media_coverage items: require a brand-name mention (display or alias).
      General tech news frequently shares scope vocabulary without naming
      the product, and weak local LLMs mark them relevant. Brand-only gate
      keeps them out entirely.
    - user_feedback items: allow the broader context needle overlap
      (brand OR scope_in OR theme tokens). Reddit / HN threads about
      product-adjacent topics stay in the funnel and let the LLM decide.
    """
    title = item.get("title") or ""
    body = item.get("body") or ""
    ct = (item.get("content_type") or "").strip().lower()
    if ct == "media_coverage":
        return text_mentions_product_brand(title, body, product)
    return text_mentions_product_context(title, body, product)


def _render_prompt(title: str, body: str) -> tuple[str, str]:
    """Return (system, user_prompt) interpolated from the current topic.

    Few-shot examples are pulled from topic.snippets when the topic's
    prompts.yaml has `relevance.few_shot.enabled: true`. Held-out snippets
    are excluded from the few-shot pool so eval gold doesn't leak.
    """
    product = current_product()
    prompts = (product.prompts or {}).get("relevance") or {}
    system = prompts.get("system") or (
        "You are a strict relevance classifier. Reply with JSON only.\n\n"
        "Rules:\n"
        "1. relevant=true ONLY if the post specifically discusses "
        "{product_display} or one of its aliases (see PRODUCT CONTEXT).\n"
        "2. Items whose primary topic is one of the OUT OF SCOPE bullets "
        "MUST be relevant=false with confidence >= 0.9.\n"
        "3. Items primarily about a NOT THIS PRODUCT entry or a COMPETITOR "
        "are relevant=false with high confidence — mentioning a competitor "
        "alone does NOT count as being about {product_display}.\n"
        "4. General industry news, unrelated vendors, or adjacent tech that "
        "does not name {product_display} are relevant=false with confidence "
        ">= 0.85.\n"
        "5. When in doubt, prefer relevant=false. False positives pollute "
        "the report; false negatives are recoverable via calibration."
    )
    template = prompts.get("template") or (
        "Decide whether this post is specifically about {product_display}.\n\n"
        "{few_shot_block}\n"
        'Reply with ONE JSON object: '
        '{{"relevant": true|false, "confidence": 0.0-1.0}}\n\n'
        "Title: {title}\nBody: {body}\n"
    )

    fs_cfg = prompts.get("few_shot") or {}
    few_shot_block = ""
    if fs_cfg.get("enabled") and product.snippets:
        picked = few_shot_subset(
            product.snippets,
            n_positive=int(fs_cfg.get("n_positive", 3)),
            n_negative=int(fs_cfg.get("n_negative", 2)),
        )
        few_shot_block = render_relevance_few_shot(picked)

    # Built context = product facts + enabled theme descriptions. Kept under
    # product_facts_block for existing prompt templates; relevance_context
    # is the same string for new templates.
    context_block = build_relevance_context(product)

    # Pass both `product_*` (new) and `topic_*` (legacy) placeholder names
    # so prompts written under either convention keep working. Templates
    # that reference {product_facts_block}/{relevance_context} substitute in
    # place; templates that don't get context prepended below.
    user_prompt = template.format(
        product_display=product.display,
        product_description=product.description or product.display,
        topic_display=product.display,
        topic_description=product.description or product.display,
        title=title or "",
        body=(body or "")[:1000],
        few_shot_block=few_shot_block,
        product_facts_block=context_block,
        relevance_context=context_block,
    )
    placeholders = ("{product_facts_block}", "{relevance_context}")
    if context_block and not any(p in template for p in placeholders):
        user_prompt = context_block + "\n\n" + user_prompt

    # System prompt may reference {product_display} — substitute so weak
    # models see the actual name in rules 1-4 rather than a literal
    # placeholder. Safe: only formats fields we control here.
    try:
        system = system.format(product_display=product.display)
    except (KeyError, IndexError):
        # Custom prompts.yaml overrides may contain literal braces; leave
        # them alone rather than crashing.
        pass

    if context_block:
        system = SYSTEM_PROMPT_SAFETY_PREAMBLE + "\n\n" + system

    return system, user_prompt


def run_relevance(week_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    app = app_config()
    drop_conf = app.get("filter", {}).get("relevance_drop_confidence", 0.7)
    client = client or LLMClient("relevance")
    product = current_product()

    items = storage.items_for_week(week_id, filter_status="passed")
    counters = {
        "evaluated": 0,
        "dropped": 0,
        "kept": 0,
        "errors": 0,
        "no_mention": 0,
    }

    drop_status_label = "dropped:not_topic_relevant"
    no_mention_label = "dropped:no_product_mention"

    # Batched writes — flush every FLUSH_EVERY items and at end-of-stage.
    # Previously each item triggered 1-2 warehouse open/close cycles; on a
    # 2,000-item week that was ~3,000 lock-contended connections competing
    # with the webui. Bounds crash-loss to <FLUSH_EVERY items.
    FLUSH_EVERY = 100
    rel_buf: list[tuple[str, float, bool]] = []
    status_buf: list[tuple[str, str]] = []

    def _flush() -> None:
        if rel_buf:
            storage.set_relevance_batch(rel_buf)
            rel_buf.clear()
        if status_buf:
            storage.set_filter_status_batch(status_buf)
            status_buf.clear()

    for it in items:
        title = it.get("title") or ""
        body = it.get("body") or ""
        # Content-type-aware pre-gate: media items require a literal brand
        # mention; user_feedback items get the broader context match. See
        # `_passes_pre_gate` for the rationale.
        if not _passes_pre_gate(it, product):
            rel_buf.append((it["id"], 1.0, False))
            status_buf.append((it["id"], no_mention_label))
            counters["dropped"] += 1
            counters["no_mention"] += 1
            if len(rel_buf) >= FLUSH_EVERY:
                _flush()
            continue

        system, prompt = _render_prompt(title, body)
        try:
            with set_context(TokenContext(
                item_id=it.get("id") or "",
                source_id=it.get("source") or "",
            )):
                res: RelevanceResult = client.structured(
                    system, prompt, RelevanceResult,
                )
        except (LLMError, ValidationError) as e:
            counters["errors"] += 1
            log.warning("relevance_failed", item=it["id"], error=safe_error_text(e))
            # Unusable model JSON: fail-closed. Fail-open used to dump junk
            # into classify (and the digest) when Ollama returned {}.
            rel_buf.append((it["id"], 1.0, False))
            status_buf.append((it["id"], drop_status_label))
            counters["dropped"] += 1
        except Exception as e:
            counters["errors"] += 1
            log.warning("relevance_failed", item=it["id"], error=safe_error_text(e))
            # Transient/unexpected errors still fail-open into classify.
            rel_buf.append((it["id"], 0.0, True))
            counters["kept"] += 1
        else:
            counters["evaluated"] += 1
            if (not res.relevant) and res.confidence >= drop_conf:
                rel_buf.append((it["id"], res.confidence, False))
                status_buf.append((it["id"], drop_status_label))
                counters["dropped"] += 1
            else:
                rel_buf.append((it["id"], res.confidence, True))
                counters["kept"] += 1

        if len(rel_buf) >= FLUSH_EVERY:
            _flush()

    _flush()

    log.info("relevance_done", **counters)
    return {"counters": counters}


# ---------------------------------------------------------------------------
# Ad-hoc relevance evaluation (wizard minifetch — Slice E, 2026-07-29)
# ---------------------------------------------------------------------------


def evaluate_ad_hoc(
    *,
    title: str,
    body: str,
    product_facts: dict,
):
    """Score one item's relevance without needing a loaded ProductSpec.

    Used by the wizard's minifetch (`pipeline.minifetch._llm_gate_survivors`)
    so we can filter the calibration deck through an LLM before the user
    has materialized a product. Uses the **assistant LLM** (ADR-0002) —
    the wizard has no per-product LLM routing yet.

    `product_facts` is a dict with (any subset of):
      display: str, aliases: list[str], scope_in: list[str],
      scope_out: list[str], description: str.

    Returns a `RelevanceResult` (relevant + confidence), or None when the
    assistant LLM isn't configured. Callers should treat None as "gate
    unavailable, fail open" and treat exceptions as the same.
    """
    from pipeline import assistant_llm
    from pipeline.llm_contract import LLMCallSpec, LLMResponseContract

    if not assistant_llm.is_configured():
        return None
    try:
        client = assistant_llm.client()
    except RuntimeError:
        return None

    display = (product_facts.get("display") or "").strip() or "the product"
    description = (product_facts.get("description") or "").strip()
    aliases = [str(a).strip() for a in (product_facts.get("aliases") or []) if a]
    scope_in = [str(s).strip() for s in (product_facts.get("scope_in") or []) if s]
    scope_out = [str(s).strip() for s in (product_facts.get("scope_out") or []) if s]

    context_lines = []
    if description:
        context_lines.append(f"Description: {description}")
    if aliases:
        context_lines.append(f"Also known as: {', '.join(aliases)}")
    if scope_in:
        context_lines.append(f"In scope: {'; '.join(scope_in)}")
    if scope_out:
        context_lines.append(f"Out of scope: {'; '.join(scope_out)}")
    context_block = "\n".join(context_lines) if context_lines else "(no extra context)"

    system = (
        "You are a strict relevance classifier for a customer-feedback "
        "monitoring wizard. Reply with JSON only."
    )
    user = (
        f"Is this post about {display}?\n\n"
        f"PRODUCT CONTEXT:\n{context_block}\n\n"
        f"POST TITLE: {title or '(none)'}\n"
        f"POST BODY: {(body or '')[:1500]}\n\n"
        'Reply with ONE JSON object: {"relevant": true|false, "confidence": 0.0-1.0}. '
        "Be strict: mentions of a competitor or the general industry do NOT count as relevant."
    )

    contract = LLMResponseContract.__new__(LLMResponseContract)
    contract._client = client
    contract.role = "assistant"
    contract.model = client.model
    contract.endpoint = client.endpoint
    return contract.call(LLMCallSpec(
        system=system, user=user, response_model=RelevanceResult,
        cacheable_system=True,
    ))
