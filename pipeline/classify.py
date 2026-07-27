"""Classify + Extract — the consequential stage (DESIGN.md §4.6, §4.7).

One full LLM call per relevant item: classification + tags + repro steps +
entity extraction (with regex hints). Output is normalized (§4.6.1), then the
primary area (§4.8.1) is chosen and persisted along with all attribute tables.

Topic-agnostic since Phase 0: the system/template prompts and the per-topic
`extras` schema come from `pipeline.topic.current_product()`. The dynamically-
composed `CoreClassification + extras` class constrains LLM output. The
item_context table still uses windows_* column names — for the Windows topic
those are populated from `c.extras.windows_*`; for topics whose extras don't
expose those attributes, they fall back to None / "unknown".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config, current_product
from pipeline.extract import extract
from pipeline.group import choose_primary_area
from pipeline.llm import LLMClient
from pipeline.models import CoreClassification, normalize_classification
from pipeline.product_facts_prompt import render_product_facts_block
from pipeline.prompt_safety import SYSTEM_PROMPT_SAFETY_PREAMBLE
from pipeline.snippets import few_shot_subset, render_classify_few_shot

log = structlog.get_logger()


def _content_types_block() -> str:
    return (
        "bug_report, feature_request, feedback, praise, question, workaround, "
        "comparison, news_discussion, rant"
    )


def _areas_block() -> str:
    topic = current_product()
    lines: list[str] = []
    for a in topic.taxonomy.get("areas", []):
        if a.get("enabled", True):
            lines.append(f"- {a['id']}: {a.get('display', a['id'])}")
    return "\n".join(lines)


def _features_block(*, max_desc_chars: int = 200) -> str:
    """Render the area -> feature hierarchy with each feature's description.

    The description is the LLM-recognition prompt the user wrote for that
    feature (taxonomy form's "Description" field). Injecting this block
    teaches the classifier what specific things to watch for inside each
    area — much higher signal than the bare area list alone.

    Each description is single-lined and truncated to `max_desc_chars` so
    a 25-feature product stays under ~6KB in the prompt.
    """
    topic = current_product()
    out: list[str] = []
    for a in topic.taxonomy.get("areas", []):
        if not a.get("enabled", True):
            continue
        feats = a.get("features") or []
        if not feats:
            continue
        out.append(f"{a['id']} ({a.get('display', a['id'])}):")
        for f in feats:
            desc = (f.get("description") or "").strip()
            # collapse whitespace; one line per feature keeps the prompt scannable
            desc = " ".join(desc.split())
            if len(desc) > max_desc_chars:
                desc = desc[: max_desc_chars - 1].rstrip() + "…"
            out.append(f"  - {f['id']}: {f.get('display', f['id'])} — {desc}")
        out.append("")
    return "\n".join(out).rstrip()


def _build_prompt(it: dict[str, Any], regex_res) -> tuple[str, str]:
    """Return (system, user_prompt) for one item, interpolated from the current topic."""
    topic = current_product()
    cprompts = (topic.prompts or {}).get("classify") or {}
    system = cprompts.get("system") or (
        f"You are classifying user feedback about {topic.display}. "
        "Return only JSON matching the requested schema."
    )
    template = cprompts.get("template") or _DEFAULT_TEMPLATE
    extras_instructions = cprompts.get("extras_instructions") or ""

    parent_ctx = _parent_context(it)
    if parent_ctx:
        parent_block = (
            f"\nPARENT POST (context only — classify the COMMENT below):\n"
            f"  title: {parent_ctx.get('title')}\n"
            f"  body: {parent_ctx.get('body')}\n"
        )
    else:
        parent_block = ""

    # Phase 5 few-shot: snippet examples injected into the prompt when
    # topics/<id>/prompts.yaml has `classify.few_shot.enabled: true`.
    fs_cfg = cprompts.get("few_shot") or {}
    few_shot_block = ""
    if fs_cfg.get("enabled") and topic.snippets:
        picked = few_shot_subset(
            topic.snippets,
            n_positive=int(fs_cfg.get("n_positive", 2)),
            n_negative=int(fs_cfg.get("n_negative", 1)),
        )
        few_shot_block = render_classify_few_shot(picked)

    # Merge competitors from product facts into vendor_hits so the classifier
    # sees them as additional named entities to check. De-duplicate case-
    # insensitively while preserving original casing (regex hits first, then
    # competitors not already present).
    seen_ci = {v.lower() for v in regex_res.vendor_hits}
    merged_vendor_hits = list(regex_res.vendor_hits)
    for comp in (getattr(topic, "competitors", []) or []):
        if comp and comp.lower() not in seen_ci:
            merged_vendor_hits.append(comp)
            seen_ci.add(comp.lower())

    facts_block = render_product_facts_block(topic)

    eng = it.get("engagement_json") or "{}"
    user_prompt = template.format(
        areas=_areas_block(),
        features=_features_block(),
        content_types=_content_types_block(),
        extras_instructions=extras_instructions,
        few_shot_block=few_shot_block,
        vendor_hits=", ".join(merged_vendor_hits) or "none",
        kb_numbers=", ".join(regex_res.kb_numbers) or "none",
        build_numbers=", ".join(regex_res.build_numbers) or "none",
        parent_block=parent_block,
        title=it.get("title") or "",
        body=(it.get("body") or "")[:4000],
        engagement=eng,
        source=it.get("source_display_name") or it.get("source"),
        product_facts_block=facts_block,
    )
    if facts_block and "{product_facts_block}" not in template:
        user_prompt = facts_block + "\n\n" + user_prompt

    if facts_block:
        system = SYSTEM_PROMPT_SAFETY_PREAMBLE + "\n\n" + system

    return system, user_prompt


_DEFAULT_TEMPLATE = """Read the post and return JSON matching the schema.

ENABLED AREAS (multi-select; use the id):
{areas}

CONTENT TYPES (multi-select):
{content_types}

If you tag bug_report, fill bug_* including repro_steps extracted verbatim if
present (else null and bug_repro_steps_quality="none").
If you tag feature_request, fill request_*.
{extras_instructions}

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
) -> tuple[CoreClassification, Any, str]:
    """Pure classify+normalize for a single item dict (no DB writes).

    Used by run_classify and by the eval harness (§7). `item` needs
    title/body/source_display_name and may carry raw.parent_context.
    Returns (normalized Classification with composed extras, RegexExtractions,
    primary_area).
    """
    regex_res = extract(f"{item.get('title') or ''}\n{item.get('body') or ''}")
    system, prompt = _build_prompt(item, regex_res)
    schema = current_product().classification_schema
    raw = client.structured(system, prompt, schema)
    norm, _report = normalize_classification(raw, feature_implicated_min_confidence=min_conf)
    primary = choose_primary_area(
        norm.areas, norm.entities, item.get("title") or "", item.get("body") or ""
    )
    return norm, regex_res, primary


def run_classify(week_id: str, client: LLMClient | None = None) -> dict[str, Any]:
    app = app_config()
    min_conf = app.get("grouping", {}).get("feature_implicated_min_confidence", 0.5)
    client = client or LLMClient("classify")
    schema = current_product().classification_schema

    # Only items that survived filter + relevance gate.
    items = storage.query(
        "SELECT * FROM items WHERE week_id=? AND filter_status='passed' "
        "AND (is_relevant IS NULL OR is_relevant=TRUE)",
        [week_id],
    )
    counters = {"classified": 0, "failed": 0, "conditional_violations": 0, "irrelevant": 0}

    drop_status_label = "dropped:not_topic_relevant"

    for it in items:
        regex_res = extract(f"{it.get('title') or ''}\n{it.get('body') or ''}")
        system, prompt = _build_prompt(it, regex_res)
        try:
            raw = client.structured(system, prompt, schema)
        except Exception as e:
            counters["failed"] += 1
            storage.set_filter_status(it["id"], "classification_failed")
            log.warning("classify_failed", item=it["id"], error=str(e))
            continue

        norm, report = normalize_classification(raw, feature_implicated_min_confidence=min_conf)
        counters["conditional_violations"] += report.conditional_violations

        # Reconcile final relevance: classify is the last gate.
        if not norm.is_topic_relevant:
            storage.set_relevance(it["id"], it.get("relevance_score") or 1.0, False)
            storage.set_filter_status(it["id"], drop_status_label)
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
    item_id: str, c: CoreClassification, regex_res, primary_area: str, model: str
) -> None:
    now = datetime.now(timezone.utc)

    storage.execute(
        "INSERT INTO item_classifications(item_id, content_types_json, sentiment, summary, "
        "confidence, primary_area, model, classified_at, churn_signal, churn_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (item_id) DO UPDATE SET content_types_json=excluded.content_types_json, "
        "sentiment=excluded.sentiment, summary=excluded.summary, confidence=excluded.confidence, "
        "primary_area=excluded.primary_area, model=excluded.model, classified_at=excluded.classified_at, "
        "churn_signal=excluded.churn_signal, churn_reason=excluded.churn_reason",
        [
            item_id, json.dumps(c.content_types), c.sentiment, c.summary[:300],
            c.confidence, primary_area, model, now,
            c.churn_signal, c.churn_reason,
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

    # context — windows_* columns are populated best-effort from c.extras for
    # topics whose extras expose them; other topics get NULL/unknown for now.
    # A topic-agnostic `extras_json` column is V2.
    storage.execute("DELETE FROM item_context WHERE item_id=?", [item_id])
    extras = getattr(c, "extras", None)
    storage.execute(
        "INSERT INTO item_context(item_id, user_context, windows_version_major, "
        "windows_version_feature_update, windows_version_build, windows_version_channel, "
        "windows_version_confidence) VALUES (?,?,?,?,?,?,?)",
        [
            item_id, c.user_context,
            getattr(extras, "windows_major", "unknown"),
            getattr(extras, "windows_feature_update", None),
            getattr(extras, "windows_build", None),
            getattr(extras, "windows_channel", None),
            getattr(extras, "windows_version_confidence", "unknown"),
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
