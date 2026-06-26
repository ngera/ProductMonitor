"""Relevance gate — cheap LLM call before expensive extraction (DESIGN.md §4.5).

Items with relevant=false AND confidence >= drop threshold are dropped (logged).
Borderline items pass through and are re-evaluated at Classify.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config
from pipeline.llm import LLMClient
from pipeline.models import RelevanceResult

log = structlog.get_logger()

SYSTEM = "You are a strict relevance classifier. Reply with JSON only."

PROMPT = """Is this post about Microsoft Windows (the operating system) — including its
features, apps, drivers, updates, or user experience?

Reply with a single JSON object: {{"relevant": true|false, "confidence": 0.0-1.0}}

Title: {title}
Body: {body}
"""


def run_relevance(week_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    app = app_config()
    drop_conf = app.get("filter", {}).get("relevance_drop_confidence", 0.7)
    client = client or LLMClient("relevance")

    items = storage.items_for_week(week_id, filter_status="passed")
    counters = {"evaluated": 0, "dropped": 0, "kept": 0, "errors": 0}

    for it in items:
        body = (it.get("body") or "")[:1000]
        prompt = PROMPT.format(title=it.get("title") or "", body=body)
        try:
            res: RelevanceResult = client.structured(SYSTEM, prompt, RelevanceResult)
        except Exception as e:
            counters["errors"] += 1
            log.warning("relevance_failed", item=it["id"], error=str(e))
            # On error, keep the item (fail-open) — Classify is the final gate.
            storage.set_relevance(it["id"], 0.0, True)
            continue

        counters["evaluated"] += 1
        if (not res.relevant) and res.confidence >= drop_conf:
            storage.set_relevance(it["id"], res.confidence, False)
            storage.set_filter_status(it["id"], "dropped:not_windows_relevant")
            counters["dropped"] += 1
        else:
            storage.set_relevance(it["id"], res.confidence, True)
            counters["kept"] += 1

    log.info("relevance_gate", **counters)
    return {"counters": counters}
