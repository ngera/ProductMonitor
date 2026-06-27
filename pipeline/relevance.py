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
from pipeline.config import app_config, current_topic
from pipeline.llm import LLMClient
from pipeline.models import RelevanceResult

log = structlog.get_logger()


def _render_prompt(title: str, body: str) -> tuple[str, str]:
    """Return (system, user_prompt) interpolated from the current topic."""
    topic = current_topic()
    prompts = (topic.prompts or {}).get("relevance") or {}
    system = prompts.get("system") or "You are a strict relevance classifier. Reply with JSON only."
    template = prompts.get("template") or (
        "Is this post about {topic_display}?\n\n"
        'Reply with a single JSON object: {{"relevant": true|false, "confidence": 0.0-1.0}}\n\n'
        "Title: {title}\nBody: {body}\n"
    )
    user_prompt = template.format(
        topic_display=topic.display,
        topic_description=topic.description or topic.display,
        title=title or "",
        body=(body or "")[:1000],
    )
    return system, user_prompt


def run_relevance(week_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    app = app_config()
    drop_conf = app.get("filter", {}).get("relevance_drop_confidence", 0.7)
    client = client or LLMClient("relevance")

    items = storage.items_for_week(week_id, filter_status="passed")
    counters = {"evaluated": 0, "dropped": 0, "kept": 0, "errors": 0}

    drop_status_label = f"dropped:not_topic_relevant"

    for it in items:
        system, prompt = _render_prompt(it.get("title") or "", it.get("body") or "")
        try:
            res: RelevanceResult = client.structured(system, prompt, RelevanceResult)
        except Exception as e:
            counters["errors"] += 1
            log.warning("relevance_failed", item=it["id"], error=str(e))
            # On error, keep the item (fail-open) — Classify is the final gate.
            storage.set_relevance(it["id"], 0.0, True)
            continue

        counters["evaluated"] += 1
        if (not res.relevant) and res.confidence >= drop_conf:
            storage.set_relevance(it["id"], res.confidence, False)
            storage.set_filter_status(it["id"], drop_status_label)
            counters["dropped"] += 1
        else:
            storage.set_relevance(it["id"], res.confidence, True)
            counters["kept"] += 1

    log.info("relevance_done", **counters)
    return {"counters": counters}
