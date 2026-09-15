"""Relevance gate — cheap LLM call before expensive extraction (DESIGN.md §4.5).

Reads the system + template prompts from the current topic's prompts.yaml,
so a new topic just edits topics/<id>/prompts.yaml (or the UI editor) to
retarget the gate at its product.

Items with relevant=false AND confidence >= drop threshold are dropped
(logged with reason). Borderline items pass through and are re-evaluated
at Classify.
"""

from __future__ import annotations

from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config, current_product
from pipeline.llm import LLMClient
from pipeline.models import RelevanceResult
from pipeline.product_facts_prompt import render_product_facts_block
from pipeline.prompt_safety import SYSTEM_PROMPT_SAFETY_PREAMBLE
from pipeline.snippets import few_shot_subset, render_relevance_few_shot

log = structlog.get_logger()


def _render_prompt(title: str, body: str) -> tuple[str, str]:
    """Return (system, user_prompt) interpolated from the current topic.

    Few-shot examples are pulled from topic.snippets when the topic's
    prompts.yaml has `relevance.few_shot.enabled: true`. Held-out snippets
    are excluded from the few-shot pool so eval gold doesn't leak.
    """
    product = current_product()
    prompts = (product.prompts or {}).get("relevance") or {}
    system = prompts.get("system") or "You are a strict relevance classifier. Reply with JSON only."
    template = prompts.get("template") or (
        "Is this post about {product_display}?\n\n"
        "{few_shot_block}\n"
        'Reply with a single JSON object: {{"relevant": true|false, "confidence": 0.0-1.0}}\n\n'
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

    facts_block = render_product_facts_block(product)

    # Pass both `product_*` (new) and `topic_*` (legacy) placeholder names
    # so prompts written under either convention keep working. Templates
    # that reference {product_facts_block} substitute in place; templates
    # that don't get facts prepended below.
    user_prompt = template.format(
        product_display=product.display,
        product_description=product.description or product.display,
        topic_display=product.display,
        topic_description=product.description or product.display,
        title=title or "",
        body=(body or "")[:1000],
        few_shot_block=few_shot_block,
        product_facts_block=facts_block,
    )
    if facts_block and "{product_facts_block}" not in template:
        user_prompt = facts_block + "\n\n" + user_prompt

    if facts_block:
        system = SYSTEM_PROMPT_SAFETY_PREAMBLE + "\n\n" + system

    return system, user_prompt


def run_relevance(week_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    app = app_config()
    drop_conf = app.get("filter", {}).get("relevance_drop_confidence", 0.7)
    client = client or LLMClient("relevance")

    items = storage.items_for_week(week_id, filter_status="passed")
    counters = {"evaluated": 0, "dropped": 0, "kept": 0, "errors": 0}

    drop_status_label = f"dropped:not_topic_relevant"

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
        system, prompt = _render_prompt(it.get("title") or "", it.get("body") or "")
        try:
            res: RelevanceResult = client.structured(system, prompt, RelevanceResult)
        except Exception as e:
            counters["errors"] += 1
            log.warning("relevance_failed", item=it["id"], error=str(e))
            # On error, keep the item (fail-open) — Classify is the final gate.
            rel_buf.append((it["id"], 0.0, True))
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
