"""Classify + Extract — the consequential stage (DESIGN.md §4.6, §4.7).

One full LLM call per relevant item: classification + tags + repro steps +
entity extraction (with regex hints). Output is normalized (§4.6.1), then the
primary area (§4.8.1) is chosen and persisted along with all attribute tables.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config, area_ids, taxonomy_config, vendors_config
from pipeline.extract import extract
from pipeline.group import choose_primary_area
from pipeline.llm import LLMClient
from pipeline.models import Classification, Entity, normalize_classification

log = structlog.get_logger()

SYSTEM = (
    "You are classifying user feedback about Microsoft Windows. "
    "Return only JSON matching the requested schema. Use multi-label where "
    'applicable. Use "unknown" or null rather than guessing.'
)


def _content_types_block() -> str:
    return (
        "bug_report, feature_request, feedback, praise, question, workaround, "
        "comparison, news_discussion, rant"
    )


def _areas_block() -> str:
    lines = []
    for a in taxonomy_config().get("areas", []):
        if a.get("enabled", True):
            lines.append(f"- {a['id']}: {a.get('display', a['id'])}")
    return "\n".join(lines)


PROMPT = """Read the post and return JSON matching the schema.

ENABLED AREAS (multi-select; use the id):
{areas}

CONTENT TYPES (multi-select):
{content_types}

If you tag bug_report, fill bug_* including repro_steps extracted verbatim if
present (else null and bug_repro_steps_quality="none").
If you tag feature_request, fill request_*.
Always attempt context including windows_major. Mark windows_version_confidence
"explicit" only if the user named the version directly.

For each entity assign:
  type (controlled vocab), vendor, product, version, role, confidence (0-1), verbatim.
  role: feature_implicated (user blames it) | hardware_in_use | software_in_use.

REGEX PRE-PASS HINTS (confirm/correct, add what was missed, discard false positives):
  vendors: {vendor_hits}
  KB numbers: {kb_numbers}
  build numbers: {build_numbers}
{parent_block}
POST:
TITLE: {title}
BODY: {body}
ENGAGEMENT: {engagement}
SOURCE: {source}

Return ONLY valid JSON.
"""


def classify_one(
    item: dict[str, Any], client: LLMClient, min_conf: float = 0.5
) -> tuple[Classification, Any, str]:
    """Pure classify+normalize for a single item dict (no DB writes).

    Used by run_classify and by the eval harness (§7). `item` needs
    title/body/source_display_name and may carry raw.parent_context.
    Returns (normalized Classification, RegexExtractions, primary_area).
    """
    regex_res = extract(f"{item.get('title') or ''}\n{item.get('body') or ''}")
    prompt = _build_prompt(item, regex_res)
    raw: Classification = client.structured(SYSTEM, prompt, Classification)
    norm, _report = normalize_classification(raw, feature_implicated_min_confidence=min_conf)
    primary = choose_primary_area(
        norm.areas, norm.entities, item.get("title") or "", item.get("body") or ""
    )
    return norm, regex_res, primary


def run_classify(week_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    app = app_config()
    min_conf = app.get("grouping", {}).get("feature_implicated_min_confidence", 0.5)
    client = client or LLMClient("classify")

    # Only items that survived filter + relevance gate.
    items = storage.query(
        "SELECT * FROM items WHERE week_id=? AND filter_status='passed' "
        "AND (is_relevant IS NULL OR is_relevant=TRUE)",
        [week_id],
    )
    counters = {"classified": 0, "failed": 0, "conditional_violations": 0, "irrelevant": 0}

    for it in items:
        regex_res = extract(f"{it.get('title') or ''}\n{it.get('body') or ''}")
        prompt = _build_prompt(it, regex_res)
        try:
            raw: Classification = client.structured(SYSTEM, prompt, Classification)
        except Exception as e:
            counters["failed"] += 1
            storage.set_filter_status(it["id"], "classification_failed")
            log.warning("classify_failed", item=it["id"], error=str(e))
            continue

        norm, report = normalize_classification(raw, feature_implicated_min_confidence=min_conf)
        counters["conditional_violations"] += report.conditional_violations

        # Reconcile final relevance: classify is the last gate.
        if not norm.is_windows_relevant:
            storage.set_relevance(it["id"], it.get("relevance_score") or 1.0, False)
            storage.set_filter_status(it["id"], "dropped:not_windows_relevant")
            counters["irrelevant"] += 1
            continue
        storage.set_relevance(it["id"], it.get("relevance_score") or 1.0, True)

        primary = choose_primary_area(
            norm.areas, norm.entities, it.get("title") or "", it.get("body") or ""
        )
        _persist(it["id"], norm, regex_res, primary, client.model)
        counters["classified"] += 1

    log.info("classified", **counters)
    return {"counters": counters}


def _build_prompt(it: dict[str, Any], regex_res) -> str:
    parent_block = ""
    raw = it.get("raw_ref")  # parent context lives in raw JSONL; reload lazily
    parent_ctx = _parent_context(it)
    if parent_ctx:
        parent_block = (
            f"\nPARENT POST (context only — classify the COMMENT below):\n"
            f"  title: {parent_ctx.get('title')}\n"
            f"  body: {parent_ctx.get('body')}\n"
        )
    eng = it.get("engagement_json") or "{}"
    return PROMPT.format(
        areas=_areas_block(),
        content_types=_content_types_block(),
        vendor_hits=", ".join(regex_res.vendor_hits) or "none",
        kb_numbers=", ".join(regex_res.kb_numbers) or "none",
        build_numbers=", ".join(regex_res.build_numbers) or "none",
        parent_block=parent_block,
        title=it.get("title") or "",
        body=(it.get("body") or "")[:4000],
        engagement=eng,
        source=it.get("source_display_name") or it.get("source"),
    )


def _parent_context(it: dict[str, Any]) -> dict[str, Any] | None:
    """Pull parent_context: directly from item.raw (eval) or the raw JSONL (pipeline)."""
    inline = (it.get("raw") or {}).get("parent_context")
    if inline:
        return inline
    if not it.get("parent_id"):
        return None
    from pipeline.util import read_jsonl

    raw_ref = it.get("raw_ref")
    if not raw_ref:
        return None
    try:
        from pathlib import Path

        for rec in read_jsonl(Path(raw_ref)):
            if rec.get("external_id") == it.get("external_id"):
                return (rec.get("raw") or {}).get("parent_context")
    except Exception:
        return None
    return None


def _persist(
    item_id: str, c: Classification, regex_res, primary_area: str, model: str
) -> None:
    now = datetime.now(timezone.utc)

    storage.execute(
        "INSERT INTO item_classifications(item_id, content_types_json, sentiment, summary, "
        "confidence, primary_area, model, classified_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT (item_id) DO UPDATE SET content_types_json=excluded.content_types_json, "
        "sentiment=excluded.sentiment, summary=excluded.summary, confidence=excluded.confidence, "
        "primary_area=excluded.primary_area, model=excluded.model, classified_at=excluded.classified_at",
        [
            item_id, json.dumps(c.content_types), c.sentiment, c.summary[:300],
            c.confidence, primary_area, model, now,
        ],
    )

    # item_areas (exactly one is_primary=TRUE)
    storage.execute("DELETE FROM item_areas WHERE item_id=?", [item_id])
    areas = c.areas or [primary_area]
    if primary_area not in areas:
        areas = [*areas, primary_area]
    storage.executemany(
        "INSERT INTO item_areas(item_id, area, is_primary) VALUES (?,?,?)",
        [[item_id, a, a == primary_area] for a in areas],
    )

    # bug attributes
    storage.execute("DELETE FROM bug_attributes WHERE item_id=?", [item_id])
    if "bug_report" in c.content_types:
        storage.execute(
            "INSERT INTO bug_attributes(item_id, severity, is_regression, reproducibility, "
            "repro_steps_quality, repro_steps_json, preconditions_json) VALUES (?,?,?,?,?,?,?)",
            [
                item_id, c.bug_severity, c.bug_is_regression, c.bug_reproducibility,
                c.bug_repro_steps_quality,
                json.dumps(c.bug_repro_steps) if c.bug_repro_steps else None,
                json.dumps(c.bug_preconditions) if c.bug_preconditions else None,
            ],
        )

    # request attributes
    storage.execute("DELETE FROM request_attributes WHERE item_id=?", [item_id])
    if "feature_request" in c.content_types:
        storage.execute(
            "INSERT INTO request_attributes(item_id, specificity, existing_workaround_mentioned) "
            "VALUES (?,?,?)",
            [item_id, c.request_specificity, c.request_existing_workaround],
        )

    # context
    storage.execute("DELETE FROM item_context WHERE item_id=?", [item_id])
    storage.execute(
        "INSERT INTO item_context(item_id, user_context, windows_version_major, "
        "windows_version_feature_update, windows_version_build, windows_version_channel, "
        "windows_version_confidence) VALUES (?,?,?,?,?,?,?)",
        [
            item_id, c.user_context, c.windows_major, c.windows_feature_update,
            c.windows_build, c.windows_channel, c.windows_version_confidence,
        ],
    )

    # entities (PK includes type + product_key + role)
    storage.execute("DELETE FROM entity_mentions WHERE item_id=?", [item_id])
    seen: set[tuple] = set()
    ent_rows: list[list[Any]] = []
    for e in c.entities:
        product_key = e.product or "__unknown__"
        pk = (e.type, e.vendor, product_key, e.role)
        if pk in seen:
            continue
        seen.add(pk)
        ent_rows.append(
            [item_id, e.type, e.vendor, product_key, e.role, e.product, e.version,
             e.confidence, e.verbatim]
        )
    storage.executemany(
        "INSERT INTO entity_mentions(item_id, type, vendor, product_key, role, product, "
        "version, confidence, verbatim) VALUES (?,?,?,?,?,?,?,?,?)",
        ent_rows,
    )

    # regex extractions
    storage.execute("DELETE FROM regex_extractions WHERE item_id=?", [item_id])
    storage.execute(
        "INSERT INTO regex_extractions(item_id, kb_numbers, cve_ids, build_numbers, vendor_hits) "
        "VALUES (?,?,?,?,?)",
        [
            item_id, json.dumps(regex_res.kb_numbers), json.dumps(regex_res.cve_ids),
            json.dumps(regex_res.build_numbers), json.dumps(regex_res.vendor_hits),
        ],
    )
    _ = (vendors_config, area_ids)  # referenced for vocab context
