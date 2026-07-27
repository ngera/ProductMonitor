"""Digest section data — per-section query builders.

Produces the row shapes the Jinja templates consume. Slice 3a:
- summary_counts(): the top Summary tiles
- section_top_issues(): top N persistent-issues per section (index page)
- section_all_issues(): all persistent-issues per section (detail page)
- raw_items_for_issue(): raw items in a persistent-issue (detail expand)

Deferred to Slice 3b: engagement percentile, competition rows, media coverage.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pipeline import storage


SECTIONS = ("bugs", "features", "positive", "negative")


def summary_counts(week_id: str, thresholds: dict) -> dict[str, int]:
    """Section counts + unique-item total for the Summary tiles.

    Counts are non-additive: an item classified as both bug_report and
    feature_request contributes to both `bugs` and `features`. The
    `unique_items` total is the honest per-item count.
    """
    rows = storage.query(
        "SELECT ic.content_types_json, ic.sentiment, ic.churn_signal "
        "FROM item_classifications ic "
        "JOIN items i ON i.id = ic.item_id "
        "WHERE i.week_id=? AND i.is_relevant=TRUE",
        [week_id],
    )
    unique_items = len(rows)
    pos_min = float(thresholds.get("positive", 0.2))
    neg_max = float(thresholds.get("negative", -0.2))
    counts = {"bugs": 0, "features": 0, "positive": 0, "negative": 0, "churn": 0}
    for r in rows:
        cts = set(json.loads(r.get("content_types_json") or "[]"))
        s = r.get("sentiment")
        if "bug_report" in cts:
            counts["bugs"] += 1
        if "feature_request" in cts:
            counts["features"] += 1
        if s is not None and s > pos_min:
            counts["positive"] += 1
        if s is not None and s < neg_max:
            counts["negative"] += 1
        if r.get("churn_signal"):
            counts["churn"] += 1
    counts["unique_items"] = unique_items
    return counts


def section_top_issues(
    product_id: str, week_id: str, section: str, top_n: int = 5
) -> list[dict[str, Any]]:
    """Top-N persistent-issues in `section` active this week, by total_mentions.

    "Active this week" = at least one week_group in this week maps to the
    issue in this section. Ordering: total_mentions DESC (cross-week count).
    """
    active = storage.query(
        "SELECT DISTINCT wgpi.issue_id FROM week_group_persistent_issue wgpi "
        "JOIN persistent_issues pi ON pi.issue_id = wgpi.issue_id "
        "  AND pi.section = wgpi.section AND pi.product_id = ? "
        "WHERE pi.section = ? AND wgpi.week_id = ?",
        [product_id, section, week_id],
    )
    if not active:
        return []
    issue_ids = [r["issue_id"] for r in active]
    ph = ",".join("?" * len(issue_ids))
    return storage.query(
        f"SELECT issue_id, canonical_title, total_mentions, first_seen_week, "
        f"last_seen_week FROM persistent_issues "
        f"WHERE product_id=? AND section=? AND issue_id IN ({ph}) "
        f"ORDER BY total_mentions DESC LIMIT ?",
        [product_id, section, *issue_ids, top_n],
    )


def section_all_issues(
    product_id: str, section: str, limit: int = 500
) -> list[dict[str, Any]]:
    """All persistent-issues in this section, most-mentioned first (detail page)."""
    return storage.query(
        "SELECT issue_id, canonical_title, total_mentions, first_seen_week, "
        "last_seen_week FROM persistent_issues "
        "WHERE product_id=? AND section=? ORDER BY total_mentions DESC LIMIT ?",
        [product_id, section, limit],
    )


def raw_items_for_issue(issue_id: str, section: str) -> list[dict[str, Any]]:
    """Raw items belonging to a persistent-issue (detail-page expand)."""
    return storage.query(
        "SELECT i.id, i.url, i.title, i.created_at, i.source_display_name, "
        "i.source, i.engagement_json, "
        "ic.summary, ic.sentiment, ic.content_types_json "
        "FROM week_group_persistent_issue wgpi "
        "JOIN week_group_members wgm ON wgm.week_id = wgpi.week_id "
        "  AND wgm.area = wgpi.area AND wgm.group_key = wgpi.group_key "
        "JOIN items i ON i.id = wgm.item_id "
        "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        "WHERE wgpi.issue_id = ? AND wgpi.section = ? "
        "ORDER BY i.created_at DESC",
        [issue_id, section],
    )


def competition_rows(
    week_id: str, prior_week_id: Optional[str], competitors: list[str]
) -> list[dict[str, Any]]:
    """Per-competitor: mentions this week + delta vs prior + avg sentiment.

    Empty list when no competitors declared. Vendors with zero mentions this
    week still appear (with `mentions=0`) so the table shows the full tracked
    list. Order: declared order from product.competitors.
    """
    if not competitors:
        return []
    rows: list[dict[str, Any]] = []
    for vendor in competitors:
        cur = storage.query(
            "SELECT COUNT(*) AS n, AVG(ic.sentiment) AS s "
            "FROM entity_mentions em JOIN items i ON i.id = em.item_id "
            "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
            "WHERE em.vendor=? AND i.week_id=? AND i.is_relevant=TRUE",
            [vendor, week_id],
        )
        prior = 0
        if prior_week_id:
            r_prior = storage.query(
                "SELECT COUNT(*) AS n FROM entity_mentions em "
                "JOIN items i ON i.id = em.item_id "
                "WHERE em.vendor=? AND i.week_id=? AND i.is_relevant=TRUE",
                [vendor, prior_week_id],
            )
            prior = int(r_prior[0]["n"]) if r_prior else 0
        mentions = int(cur[0]["n"]) if cur else 0
        sent = float(cur[0]["s"]) if cur and cur[0].get("s") is not None else None
        rows.append({
            "vendor": vendor,
            "mentions": mentions,
            "delta": mentions - prior,
            "sentiment": sent,
        })
    return rows


def media_coverage_items(
    week_id: str, product_names: list[str], limit: int = 50
) -> dict[str, list[dict[str, Any]]]:
    """News-tagged items split into Product Coverage vs Industry News.

    Signal: `news_discussion` in content_types_json. Split heuristic per
    report_v2_design.md §4.8: item is Product Coverage when any name/alias
    from `product_names` appears (case-insensitive) in the title or body;
    else Industry News.
    """
    rows = storage.query(
        "SELECT i.id, i.url, i.title, i.body, i.created_at, i.source_display_name, "
        "ic.summary "
        "FROM items i JOIN item_classifications ic ON ic.item_id = i.id "
        "WHERE i.week_id=? AND i.is_relevant=TRUE "
        "AND ic.content_types_json LIKE '%news_discussion%' "
        "ORDER BY i.created_at DESC LIMIT ?",
        [week_id, limit],
    )
    needles = [n.lower() for n in product_names if n]
    product, industry = [], []
    for r in rows:
        haystack = f"{r.get('title') or ''} {r.get('body') or ''}".lower()
        is_product = any(n in haystack for n in needles)
        bucket = product if is_product else industry
        bucket.append({
            "id": r["id"], "url": r["url"], "title": r["title"] or "(no title)",
            "created_at": str(r["created_at"] or "")[:10],
            "source_display_name": r["source_display_name"],
            "summary": r.get("summary") or "",
        })
    return {"product": product, "industry": industry}
