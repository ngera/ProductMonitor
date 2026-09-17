"""Digest section data — per-section query builders.

Produces the row shapes the Jinja templates consume:
- summary_counts(): top Summary tiles
- summary_callouts(): critical/high-severity + week-over-week deltas (§4.1)
- section_top_issues(): top N persistent-issues per section (index)
- section_all_issues(): all persistent-issues per section (detail)
- raw_items_for_issue(): raw items in a persistent-issue (detail expand)
- competition_rows() / media_coverage_items(): standalone digest sections

`section_top_issues` and `section_all_issues` return richer rows in v2 —
`max_severity`, `avg_sentiment`, `churn_count` — so both the templates
can render badges without a second round of queries and so per-section
priority formulas (report_v2_design.md §10) can rank them cheaply.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pipeline import storage


SECTIONS = ("bugs", "features", "positive", "negative")


# Section-specific priority formulas per report_v2_design.md §10. Applied
# as ORDER BY on the enriched issue query so the templates get rows in the
# right order without a second sort pass.
_SEVERITY_RANK_SQL = (
    "CASE COALESCE(agg.max_severity,'') "
    "WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"
)

_PRIORITY_ORDER_BY = {
    "bugs":     f"{_SEVERITY_RANK_SQL} * 100 + pi.total_mentions",
    "features": "pi.total_mentions",
    "negative": "ABS(COALESCE(agg.avg_sentiment, 0)) * pi.total_mentions",
    "positive": "COALESCE(agg.avg_sentiment, 0) * pi.total_mentions",
}


def summary_counts(week_id: str, thresholds: dict) -> dict[str, Any]:
    """Section counts + unique-item total + avg_sentiment + n_sources.

    Counts are non-additive: an item classified as both bug_report and
    feature_request contributes to both `bugs` and `features`. The
    `unique_items` total is the honest per-item count.

    Adds (report_v2 mockup):
      - `avg_sentiment`: mean of non-null sentiments across relevant items.
      - `n_sources`: distinct source_display_name values this week.
    """
    rows = storage.query(
        "SELECT i.source_display_name, ic.content_types_json, ic.sentiment, "
        "ic.churn_signal "
        "FROM items i "
        "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        "WHERE i.week_id=? AND i.is_relevant=TRUE",
        [week_id],
    )
    unique_items = len(rows)
    pos_min = float(thresholds.get("positive", 0.2))
    neg_max = float(thresholds.get("negative", -0.2))
    counts: dict[str, Any] = {"bugs": 0, "features": 0, "positive": 0,
                              "negative": 0, "churn": 0}
    sentiments: list[float] = []
    sources: set[str] = set()
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
        if s is not None:
            sentiments.append(float(s))
        src = r.get("source_display_name")
        if src:
            sources.add(src)
    counts["unique_items"] = unique_items
    counts["avg_sentiment"] = (
        sum(sentiments) / len(sentiments) if sentiments else None
    )
    counts["n_sources"] = len(sources)
    return counts


def _issue_aggregate_sql(section: str) -> str:
    """Sub-SELECT that computes avg_sentiment, churn_count, and max_severity
    per persistent_issue in a section by walking wgpi → wgm → items.

    Used by both section_top_issues and section_all_issues so severity/
    sentiment badges and priority ordering are computed once, in SQL.
    """
    return (
        "SELECT wgpi.issue_id, "
        "       AVG(ic.sentiment) AS avg_sentiment, "
        "       SUM(CASE WHEN ic.churn_signal THEN 1 ELSE 0 END) AS churn_count, "
        "       MAX(ba.severity) AS max_severity "
        "FROM week_group_persistent_issue wgpi "
        "JOIN week_group_members wgm ON wgm.week_id = wgpi.week_id "
        "  AND wgm.area = wgpi.area AND wgm.group_key = wgpi.group_key "
        "LEFT JOIN item_classifications ic ON ic.item_id = wgm.item_id "
        "LEFT JOIN bug_attributes ba ON ba.item_id = wgm.item_id "
        f"WHERE wgpi.section = '{section}' "
        "GROUP BY wgpi.issue_id"
    )


def section_top_issues(
    product_id: str, week_id: str, section: str, top_n: int = 5
) -> list[dict[str, Any]]:
    """Top-N persistent-issues in `section` active this week.

    "Active this week" = at least one week_group in this week maps to the
    issue in this section. Ordering follows §10 formulas per section:
      - bugs:     severity_rank × 100 + total_mentions
      - features: total_mentions
      - negative: abs(avg_sentiment) × total_mentions
      - positive: avg_sentiment × total_mentions

    Rows carry `avg_sentiment`, `churn_count`, `max_severity` so the
    index template can render badges without a second pass.
    """
    if section not in _PRIORITY_ORDER_BY:
        return []
    order_by = _PRIORITY_ORDER_BY[section]
    agg = _issue_aggregate_sql(section)
    return storage.query(
        f"SELECT pi.issue_id, pi.canonical_title, pi.total_mentions, "
        f"       pi.first_seen_week, pi.last_seen_week, "
        f"       agg.avg_sentiment, agg.churn_count, agg.max_severity "
        f"FROM persistent_issues pi "
        f"JOIN (SELECT DISTINCT issue_id FROM week_group_persistent_issue "
        f"      WHERE section = ? AND week_id = ?) active "
        f"  ON active.issue_id = pi.issue_id "
        f"LEFT JOIN ({agg}) agg ON agg.issue_id = pi.issue_id "
        f"WHERE pi.product_id = ? AND pi.section = ? "
        f"ORDER BY {order_by} DESC LIMIT ?",
        [section, week_id, product_id, section, top_n],
    )


def section_all_issues(
    product_id: str, section: str, limit: int = 500
) -> list[dict[str, Any]]:
    """All persistent-issues in this section, sorted per §10 formulas.

    Same enrichment as section_top_issues so detail-page templates can
    render severity badges and header-line churn/sentiment info without a
    second query.
    """
    if section not in _PRIORITY_ORDER_BY:
        return []
    order_by = _PRIORITY_ORDER_BY[section]
    agg = _issue_aggregate_sql(section)
    return storage.query(
        f"SELECT pi.issue_id, pi.canonical_title, pi.total_mentions, "
        f"       pi.first_seen_week, pi.last_seen_week, "
        f"       agg.avg_sentiment, agg.churn_count, agg.max_severity "
        f"FROM persistent_issues pi "
        f"LEFT JOIN ({agg}) agg ON agg.issue_id = pi.issue_id "
        f"WHERE pi.product_id = ? AND pi.section = ? "
        f"ORDER BY {order_by} DESC LIMIT ?",
        [product_id, section, limit],
    )


def summary_callouts(
    week_id: str,
    prior_week_id: Optional[str] = None,
    thresholds: Optional[dict] = None,
) -> dict[str, Any]:
    """Callouts row + WoW context for the digest Summary (report_v2 mockup).

    Returns:
      critical_bugs, high_severity   — counts this week
      wow_volume                     — this_unique − prior_unique (int)
      wow_volume_pct                 — signed percentage (float, or None)
      wow_sentiment                  — avg_this − avg_prior (float, or None)
      prior_counts                   — per-section prior-week counts, dict
                                       with keys {bugs, features, positive,
                                       negative, churn} for tile deltas
    `wow_*` and `prior_counts` are None / empty when no prior week exists.
    """
    def _sev_counts(wid: str) -> tuple[int, int]:
        rows = storage.query(
            "SELECT ba.severity FROM items i "
            "JOIN item_classifications ic ON ic.item_id = i.id "
            "JOIN bug_attributes ba ON ba.item_id = i.id "
            "WHERE i.week_id=? AND i.is_relevant=TRUE "
            "AND ic.content_types_json LIKE '%bug_report%'",
            [wid],
        )
        crit = sum(1 for r in rows if r.get("severity") == "critical")
        high = sum(1 for r in rows if r.get("severity") in ("critical", "high"))
        return crit, high

    critical_bugs, high_severity = _sev_counts(week_id)

    # Per-section this-week counts via the existing helper — mirror on prior
    # so tile deltas + WoW volume+sentiment all read the same thresholds.
    thr = thresholds or {"positive": 0.2, "negative": -0.2}
    this = summary_counts(week_id, thr)

    wow_volume: Optional[int] = None
    wow_volume_pct: Optional[float] = None
    wow_sentiment: Optional[float] = None
    prior_counts: dict[str, int] = {}
    if prior_week_id:
        prior = summary_counts(prior_week_id, thr)
        prior_counts = {
            k: int(prior.get(k) or 0)
            for k in ("bugs", "features", "positive", "negative", "churn",
                       "unique_items")
        }
        wow_volume = int(this["unique_items"]) - prior_counts["unique_items"]
        if prior_counts["unique_items"]:
            wow_volume_pct = 100.0 * wow_volume / prior_counts["unique_items"]
        if this.get("avg_sentiment") is not None and prior.get("avg_sentiment") is not None:
            wow_sentiment = float(this["avg_sentiment"]) - float(prior["avg_sentiment"])

    return {
        "critical_bugs": critical_bugs,
        "high_severity": high_severity,
        "wow_volume": wow_volume,
        "wow_volume_pct": wow_volume_pct,
        "wow_sentiment": wow_sentiment,
        "prior_counts": prior_counts,
    }


def canonical_item_for_issue(issue_id: str, section: str) -> Optional[dict[str, Any]]:
    """Latest canonical item joined to a persistent-issue, or None.

    Used to feed the headline LLM pass with real item context (title +
    body + source) rather than only the persistent_issues.canonical_title
    (which is frozen from first_seen_week and may be stale).
    """
    rows = storage.query(
        "SELECT i.id, i.title, i.body, i.source_display_name, "
        "i.url, i.author, i.created_at "
        "FROM week_group_persistent_issue wgpi "
        "JOIN week_groups wg ON wg.week_id = wgpi.week_id "
        "  AND wg.area = wgpi.area AND wg.group_key = wgpi.group_key "
        "JOIN items i ON i.id = wg.canonical_item_id "
        "WHERE wgpi.issue_id = ? AND wgpi.section = ? "
        "ORDER BY wgpi.week_id DESC LIMIT 1",
        [issue_id, section],
    )
    return rows[0] if rows else None


def raw_items_for_issue(issue_id: str, section: str) -> list[dict[str, Any]]:
    """Raw items belonging to a persistent-issue (detail-page expand).

    Selects `author` explicitly — the §13 attribution partial reads it and
    would silently drop the byline if absent from the row.

    Also joins bug_attributes.severity + churn_signal so detail rows can
    show the badges §8 / §4.4 call for without another query.
    """
    return storage.query(
        "SELECT i.id, i.url, i.title, i.body, i.author, i.created_at, "
        "i.source_display_name, i.source, i.engagement_json, "
        "ic.summary, ic.sentiment, ic.content_types_json, ic.churn_signal, "
        "ba.severity AS bug_severity "
        "FROM week_group_persistent_issue wgpi "
        "JOIN week_group_members wgm ON wgm.week_id = wgpi.week_id "
        "  AND wgm.area = wgpi.area AND wgm.group_key = wgpi.group_key "
        "JOIN items i ON i.id = wgm.item_id "
        "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        "LEFT JOIN bug_attributes ba ON ba.item_id = i.id "
        "WHERE wgpi.issue_id = ? AND wgpi.section = ? "
        "ORDER BY i.created_at DESC",
        [issue_id, section],
    )


def competition_rows(
    week_id: str, prior_week_id: Optional[str], competitors: list[Any]
) -> list[dict[str, Any]]:
    """Per-competitor: mentions this week + delta vs prior + avg sentiment.

    Case-insensitive substring match against title||body. Matches on the
    competitor `name` AND every declared `alias` (see report_v2_design.md
    §7.2 — "aliases feed entity matching so `Mac` matches `macOS`").

    `competitors` items may be plain strings (legacy) or dicts of the
    rich `{name, aliases, color}` shape. Competitors with zero mentions
    this week still appear (mentions=0) so the table shows the full
    tracked list. Order: declared order from product.competitors.
    """
    if not competitors:
        return []

    def _needles(c: Any) -> tuple[str, list[str]]:
        """(display_name, [lowered-search-terms])"""
        if isinstance(c, dict):
            name = str(c.get("name") or "").strip()
            aliases = [
                str(a).strip() for a in (c.get("aliases") or [])
                if str(a).strip()
            ]
            terms = [t.lower() for t in [name, *aliases] if t]
            return name, terms
        s = str(c).strip()
        return s, [s.lower()] if s else []

    rows: list[dict[str, Any]] = []
    for c in competitors:
        name, terms = _needles(c)
        if not name or not terms:
            continue
        like_clauses = " OR ".join(
            "LOWER(COALESCE(i.title,'') || ' ' || COALESCE(i.body,'')) LIKE ?"
            for _ in terms
        )
        needle_params = [f"%{t}%" for t in terms]

        cur = storage.query(
            f"SELECT COUNT(*) AS n, AVG(ic.sentiment) AS s "
            f"FROM items i "
            f"LEFT JOIN item_classifications ic ON ic.item_id = i.id "
            f"WHERE i.week_id=? AND i.is_relevant=TRUE AND ({like_clauses})",
            [week_id, *needle_params],
        )
        prior = 0
        if prior_week_id:
            r_prior = storage.query(
                f"SELECT COUNT(*) AS n FROM items i "
                f"WHERE i.week_id=? AND i.is_relevant=TRUE AND ({like_clauses})",
                [prior_week_id, *needle_params],
            )
            prior = int(r_prior[0]["n"]) if r_prior else 0
        mentions = int(cur[0]["n"]) if cur else 0
        sent = float(cur[0]["s"]) if cur and cur[0].get("s") is not None else None
        context = ""
        if isinstance(c, dict):
            context = str(c.get("context") or "").strip()
        rows.append({
            "name": name,
            "mentions": mentions,
            "delta": mentions - prior,
            "sentiment": sent,
            "context": context,
        })
    return rows


def press_coverage_items(
    week_id: str, top_n: int = 5,
) -> list[dict[str, Any]]:
    """Top-N items tagged `content_type='media_coverage'` for the week,
    ranked by score DESC.

    Feeds the ADR-0028 top-of-digest "Press Coverage" summary block —
    a compact "here's what the press wrote about the product this week"
    scannable list above the area sections. Items still appear inside
    their classified area sections; this is an additional read-first
    grouping, not a routing change.

    Returns [] when top_n <= 0 or when the product has no media_coverage
    items this week — the template hides the block in that case so a
    zero-media product doesn't get an empty header.
    """
    if top_n <= 0:
        return []
    rows = storage.query(
        "SELECT i.id, i.url, i.title, i.source_display_name, "
        "i.created_at, ic.summary, s.score "
        "FROM items i "
        "JOIN item_classifications ic ON ic.item_id = i.id "
        "LEFT JOIN scores s ON s.item_id = i.id "
        "WHERE i.week_id=? AND i.is_relevant=TRUE "
        "AND i.content_type='media_coverage' "
        "ORDER BY s.score DESC NULLS LAST, i.created_at DESC "
        "LIMIT ?",
        [week_id, top_n],
    )
    return [
        {
            "id": r["id"], "url": r["url"],
            "title": r["title"] or "(no title)",
            "source_display_name": r.get("source_display_name") or "",
            "created_at": str(r.get("created_at") or "")[:10],
            "summary": r.get("summary") or "",
            "score": float(r.get("score") or 0.0),
        }
        for r in rows
    ]


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
