"""Per-item scoring (DESIGN.md §4.10).

    score = (1 + log(1 + upvotes + 2*comments))
          * source_credibility_weight
          * exp(-age_days / halflife)
          * confidence

The `1 +` floor keeps zero-engagement items (RSS/news feeds, most Tavily
results — anything without an upvote/comment channel) from collapsing to a
score of 0 and losing every ranking tie to a Reddit post with a single
upvote. Engagement still tilts the ordering; it just can't zero it.

Runs before Group, because canonical selection (§4.8.3) reads item scores.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config, sources_config

log = structlog.get_logger()


# ADR-0028: global content_type defaults, applied when neither the
# source config nor the operator sets a per-content-type override.
# Media coverage weights lower than user_feedback by convention —
# editorial articles are one voice; ten users saying the same thing
# are ten independent signals.
_DEFAULT_CREDIBILITY_BY_CONTENT_TYPE: dict[str, float] = {
    "media_coverage": 0.5,
    "user_feedback":  0.8,
}


def _credibility_source_config() -> dict[str, dict[str, Any]]:
    """Return `{source_id: config_block}` for every configured source.
    Callers dig `credibility_weight` and `credibility_weight_by_content_type`
    out of each block."""
    return {
        s["id"]: s
        for s in sources_config().get("sources", [])
    }


def _credibility_for(cfg: dict[str, Any] | None, content_type: str) -> float:
    """Resolution chain (highest priority first):
      1. operator per-instance per-type: sources.yaml sources[].credibility_weight_by_content_type[content_type]
      2. operator per-instance single:   sources.yaml sources[].credibility_weight
      3. global default:                 _DEFAULT_CREDIBILITY_BY_CONTENT_TYPE[content_type]
      4. 1.0 fallback for unknown content_types (defensive).
    """
    if cfg:
        by_type = cfg.get("credibility_weight_by_content_type") or {}
        if content_type in by_type:
            try:
                return float(by_type[content_type])
            except (TypeError, ValueError):
                pass
        if "credibility_weight" in cfg:
            try:
                return float(cfg["credibility_weight"])
            except (TypeError, ValueError):
                pass
    return _DEFAULT_CREDIBILITY_BY_CONTENT_TYPE.get(content_type, 1.0)


def run_score(week_id: str) -> dict[str, Any]:
    halflife = app_config().get("scoring", {}).get("recency_halflife_days", 7)
    source_cfgs = _credibility_source_config()
    now = datetime.now(timezone.utc)

    rows = storage.query(
        "SELECT i.id, i.source, i.content_type, i.created_at, i.engagement_json, "
        "ic.confidence AS confidence "
        "FROM items i JOIN item_classifications ic ON ic.item_id=i.id "
        "WHERE i.week_id=? AND i.is_relevant=TRUE",
        [week_id],
    )

    out: list[list[Any]] = []
    for r in rows:
        eng = _engagement(r.get("engagement_json"))
        engagement_w = 1.0 + math.log1p(eng["upvotes"] + 2 * eng["comment_count"])
        content_type = r.get("content_type") or "user_feedback"
        source_w = _credibility_for(source_cfgs.get(r["source"]), content_type)
        recency_w = _recency(r["created_at"], now, halflife)
        conf = float(r.get("confidence") or 0.5)
        score = engagement_w * source_w * recency_w * conf
        out.append([r["id"], score, engagement_w, source_w, recency_w, now])

    storage.execute("DELETE FROM scores WHERE item_id IN (SELECT id FROM items WHERE week_id=?)", [week_id])
    storage.executemany(
        "INSERT INTO scores(item_id, score, engagement_w, source_w, recency_w, computed_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT (item_id) DO UPDATE SET score=excluded.score, "
        "engagement_w=excluded.engagement_w, source_w=excluded.source_w, "
        "recency_w=excluded.recency_w, computed_at=excluded.computed_at",
        out,
    )
    log.info("scored", items=len(out))
    return {"scored": len(out)}


def _engagement(engagement_json: str | None) -> dict[str, int]:
    try:
        e = json.loads(engagement_json or "{}")
    except json.JSONDecodeError:
        e = {}
    return {"upvotes": int(e.get("upvotes", 0)), "comment_count": int(e.get("comment_count", 0))}


def _recency(created_at: Any, now: datetime, halflife: float) -> float:
    if isinstance(created_at, str):
        created = datetime.fromisoformat(created_at)
    else:
        created = created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = max((now - created).total_seconds() / 86400.0, 0.0)
    return math.exp(-age_days / halflife)
