"""Per-item scoring (DESIGN.md §4.10).

    score = log(1 + upvotes + 2*comments)
          * source_credibility_weight
          * exp(-age_days / halflife)
          * confidence

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


def _credibility_weights() -> dict[str, float]:
    return {
        s["id"]: float(s.get("credibility_weight", 1.0))
        for s in sources_config().get("sources", [])
    }


def run_score(week_id: str) -> dict[str, Any]:
    halflife = app_config().get("scoring", {}).get("recency_halflife_days", 7)
    weights = _credibility_weights()
    now = datetime.now(timezone.utc)

    rows = storage.query(
        "SELECT i.id, i.source, i.created_at, i.engagement_json, "
        "ic.confidence AS confidence "
        "FROM items i JOIN item_classifications ic ON ic.item_id=i.id "
        "WHERE i.week_id=? AND i.is_relevant=TRUE",
        [week_id],
    )

    out: list[list[Any]] = []
    for r in rows:
        eng = _engagement(r.get("engagement_json"))
        engagement_w = math.log1p(eng["upvotes"] + 2 * eng["comment_count"])
        source_w = weights.get(r["source"], 1.0)
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
