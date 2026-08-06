"""Aggregation — weekly_rollup per (week, area) (DESIGN.md §4.8, §5.2).

Aggregates over an item's PRIMARY area so counts don't double across areas.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import taxonomy_version

log = structlog.get_logger()

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def run_aggregate(week_id: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    tax_v = taxonomy_version()

    rows = storage.query(
        """
        SELECT ic.primary_area AS area,
               ic.content_types_json AS content_types_json,
               ic.sentiment AS sentiment,
               s.score AS score,
               ba.severity AS severity
        FROM item_classifications ic
        JOIN items i ON i.id = ic.item_id
        LEFT JOIN scores s ON s.item_id = ic.item_id
        LEFT JOIN bug_attributes ba ON ba.item_id = ic.item_id
        WHERE i.week_id = ? AND i.is_relevant = TRUE
        """,
        [week_id],
    )

    # group counts per area
    group_counts = {
        r["area"]: r["n"]
        for r in storage.query(
            "SELECT area, COUNT(*) AS n FROM week_groups WHERE week_id=? GROUP BY area",
            [week_id],
        )
    }
    top_groups = _top_groups_by_area(week_id)

    by_area: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_area[r["area"] or "other"].append(r)

    storage.execute("DELETE FROM weekly_rollup WHERE week_id=?", [week_id])
    out: list[list[Any]] = []
    for area, items in by_area.items():
        ct_counter: Counter = Counter()
        sentiments: list[float] = []
        weighted_num = 0.0
        weighted_den = 0.0
        max_sev = None
        for it in items:
            cts = json.loads(it.get("content_types_json") or "[]")
            ct_counter.update(cts)
            if it.get("sentiment") is not None:
                sentiments.append(float(it["sentiment"]))
                w = float(it.get("score") or 0.0)
                weighted_num += float(it["sentiment"]) * w
                weighted_den += w
            sev = it.get("severity")
            if sev and (max_sev is None or _SEVERITY_RANK.get(sev, 0) > _SEVERITY_RANK.get(max_sev, 0)):
                max_sev = sev

        avg_sent = sum(sentiments) / len(sentiments) if sentiments else None
        weighted_sent = weighted_num / weighted_den if weighted_den else avg_sent
        out.append([
            week_id, area, tax_v, len(items),
            ct_counter.get("bug_report", 0), ct_counter.get("feature_request", 0),
            ct_counter.get("feedback", 0), ct_counter.get("praise", 0),
            ct_counter.get("workaround", 0),
            avg_sent, weighted_sent, max_sev,
            group_counts.get(area, 0),
            json.dumps(top_groups.get(area, [])),
            now,
        ])

    storage.executemany(
        "INSERT INTO weekly_rollup(week_id, area, taxonomy_version, item_count, bug_count, "
        "feature_request_count, feedback_count, praise_count, workaround_count, avg_sentiment, "
        "weighted_sentiment, severity_max, group_count, top_group_keys_json, "
        "computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        out,
    )
    log.info("aggregated", areas=len(out))
    return {"areas": len(out)}


def _top_groups_by_area(week_id: str, top_n: int = 10) -> dict[str, list[dict[str, Any]]]:
    rows = storage.query(
        "SELECT area, group_key, canonical_item_id, member_count FROM week_groups "
        "WHERE week_id=? ORDER BY member_count DESC",
        [week_id],
    )
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if len(out[r["area"]]) < top_n:
            out[r["area"]].append(
                {"group_key": r["group_key"], "canonical_item_id": r["canonical_item_id"],
                 "member_count": r["member_count"]}
            )
    return out


