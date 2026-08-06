"""Digest v2 trend charts — matplotlib PNGs to reports/<product>/<week>/data/.

Lazily imports matplotlib so the digest still builds (with template
placeholders) when matplotlib isn't installed. See report_v2_design.md §4.2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import structlog

from pipeline import storage

log = structlog.get_logger()




def _mpl():
    """Import matplotlib lazily so unpolished envs still build the digest."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # no display; server-friendly
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        return None


def _weekly_history(
    buckets: int, pos_threshold: float = 0.2, neg_threshold: float = -0.2,
) -> list[dict[str, Any]]:
    """Trailing N weeks — per-bucket counts for the 4 categories plus
    sentiment averages. Aggregates from item_classifications so positive /
    negative counts stay in sync with the digest's sentiment thresholds
    (both are threshold-derived, not stored).

    Oldest → newest.
    """
    rows = storage.query(
        "SELECT i.week_id AS week_id, "
        "SUM(CASE WHEN ic.content_types_json LIKE '%bug_report%' "
        "         THEN 1 ELSE 0 END) AS bugs, "
        "SUM(CASE WHEN ic.content_types_json LIKE '%feature_request%' "
        "         THEN 1 ELSE 0 END) AS features, "
        "SUM(CASE WHEN ic.sentiment > ? THEN 1 ELSE 0 END) AS positive, "
        "SUM(CASE WHEN ic.sentiment < ? THEN 1 ELSE 0 END) AS negative, "
        "AVG(ic.sentiment) AS avg_sent "
        "FROM items i "
        "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        "WHERE i.is_relevant = TRUE "
        "GROUP BY i.week_id "
        "ORDER BY i.week_id DESC LIMIT ?",
        [pos_threshold, neg_threshold, buckets],
    )
    # weighted_sentiment lives on weekly_rollup only — pull it in a small
    # side query so we don't lose the "weighted" line on the sentiment chart.
    if rows:
        week_ids = [r["week_id"] for r in rows]
        ph = ",".join("?" * len(week_ids))
        wsent_rows = storage.query(
            f"SELECT week_id, AVG(weighted_sentiment) AS weighted_sent "
            f"FROM weekly_rollup WHERE week_id IN ({ph}) GROUP BY week_id",
            week_ids,
        )
        ws_map = {r["week_id"]: r["weighted_sent"] for r in wsent_rows}
        for r in rows:
            r["weighted_sent"] = ws_map.get(r["week_id"]) or r.get("avg_sent") or 0.0
    return list(reversed(rows))


def _week_id_to_date(week_id: str):
    """Parse an ISO week id like '2026-W30' to the Monday date. None on error."""
    from datetime import date
    try:
        year, wk = week_id.split("-W")
        return date.fromisocalendar(int(year), int(wk), 1)
    except Exception:
        return None


def _bucket_mode() -> str:
    """Return 'weekly' or 'monthly' based on trailing history span.

    Per report_v2_design.md §4.2: once the product has at least
    `digest.trend_bucket_switch_months` (default 12) of data, the chart
    aggregates to monthly buckets so we don't render 100+ tiny weekly bars
    on year-old products. Below that threshold stays weekly.

    Calendar-month arithmetic (not 365-day approximation) — a run that
    hits its 12-month mark on Feb 28 doesn't need to wait for day 365.
    Back-compat: still honors the legacy `trend_bucket_switch_days` key
    when present, translating days → months via 30-day rounding.
    """
    from datetime import date
    from pipeline.config import app_config
    digest_cfg = app_config().get("digest") or {}
    if digest_cfg.get("trend_bucket_switch_months") is not None:
        switch_months = int(digest_cfg["trend_bucket_switch_months"])
    elif digest_cfg.get("trend_bucket_switch_days") is not None:
        # Legacy key — treat 365 days as 12 months.
        switch_months = max(1, int(digest_cfg["trend_bucket_switch_days"]) // 30)
    else:
        switch_months = 12
    rows = storage.query(
        "SELECT MIN(week_id) AS earliest FROM weekly_rollup"
    )
    if not rows or not rows[0].get("earliest"):
        return "weekly"
    earliest = _week_id_to_date(rows[0]["earliest"])
    if earliest is None:
        return "weekly"
    today = date.today()
    months_span = (today.year - earliest.year) * 12 + (today.month - earliest.month)
    if today.day < earliest.day:
        months_span -= 1
    return "monthly" if months_span >= switch_months else "weekly"


def _monthly_history(
    buckets: int, pos_threshold: float = 0.2, neg_threshold: float = -0.2,
) -> list[dict[str, Any]]:
    """Trailing N months — per-bucket counts for the 4 categories plus
    sentiment averages. Aggregates from item_classifications so counts
    match the digest's threshold-derived categories.

    Bucket key is `YYYY-MM`. Weeks with a non-parseable week_id are silently
    dropped. Oldest → newest.
    """
    from collections import defaultdict
    rows = storage.query(
        "SELECT i.week_id AS week_id, "
        "SUM(CASE WHEN ic.content_types_json LIKE '%bug_report%' "
        "         THEN 1 ELSE 0 END) AS bugs, "
        "SUM(CASE WHEN ic.content_types_json LIKE '%feature_request%' "
        "         THEN 1 ELSE 0 END) AS features, "
        "SUM(CASE WHEN ic.sentiment > ? THEN 1 ELSE 0 END) AS positive, "
        "SUM(CASE WHEN ic.sentiment < ? THEN 1 ELSE 0 END) AS negative, "
        "AVG(ic.sentiment) AS avg_sent "
        "FROM items i "
        "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        "WHERE i.is_relevant = TRUE "
        "GROUP BY i.week_id "
        "ORDER BY i.week_id",
        [pos_threshold, neg_threshold],
    )
    ws_rows = storage.query(
        "SELECT week_id, AVG(weighted_sentiment) AS ws "
        "FROM weekly_rollup GROUP BY week_id"
    )
    ws_map = {r["week_id"]: r["ws"] for r in ws_rows}

    by_month: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"bugs": 0, "features": 0, "positive": 0, "negative": 0,
                 "sents": [], "weighted_sents": []}
    )
    for r in rows:
        d = _week_id_to_date(r["week_id"])
        if d is None:
            continue
        key = f"{d.year}-{d.month:02d}"
        b = by_month[key]
        b["bugs"]     += int(r.get("bugs") or 0)
        b["features"] += int(r.get("features") or 0)
        b["positive"] += int(r.get("positive") or 0)
        b["negative"] += int(r.get("negative") or 0)
        if r.get("avg_sent") is not None:
            b["sents"].append(float(r["avg_sent"]))
        ws = ws_map.get(r["week_id"])
        if ws is not None:
            b["weighted_sents"].append(float(ws))
    ordered = sorted(by_month.keys())[-buckets:]
    return [
        {
            "week_id": k,
            "bugs":     by_month[k]["bugs"],
            "features": by_month[k]["features"],
            "positive": by_month[k]["positive"],
            "negative": by_month[k]["negative"],
            "avg_sent": (sum(by_month[k]["sents"]) / len(by_month[k]["sents"]))
                        if by_month[k]["sents"] else 0.0,
            "weighted_sent": (
                sum(by_month[k]["weighted_sents"]) / len(by_month[k]["weighted_sents"])
            ) if by_month[k]["weighted_sents"] else 0.0,
        }
        for k in ordered
    ]


def _competitor_terms(competitor: Any) -> list[str]:
    """Lowercased search terms for one competitor: name + declared aliases."""
    from pipeline.product import competitor_display_name
    name = competitor_display_name(competitor).lower()
    aliases = []
    if isinstance(competitor, dict):
        aliases = [str(a).strip().lower() for a in (competitor.get("aliases") or [])]
    return [t for t in [name, *aliases] if t]


def _competitor_sentiment_per_bucket(competitor: Any, mode: str) -> dict[str, float]:
    """Avg sentiment of items mentioning the competitor, keyed by bucket.

    Case-insensitive substring match against title||body, matching on the
    competitor `name` AND every declared `alias` per report_v2_design.md §7.2.
    Bucket key = `week_id` (e.g. `2026-W30`) in weekly mode, `YYYY-MM` in
    monthly mode.
    """
    terms = _competitor_terms(competitor)
    if not terms:
        return {}
    like_clauses = " OR ".join(
        "LOWER(COALESCE(i.title,'') || ' ' || COALESCE(i.body,'')) LIKE ?"
        for _ in terms
    )
    params = [f"%{t}%" for t in terms]

    if mode == "monthly":
        from collections import defaultdict
        rows = storage.query(
            f"SELECT i.week_id AS w, ic.sentiment AS s "
            f"FROM items i "
            f"LEFT JOIN item_classifications ic ON ic.item_id = i.id "
            f"WHERE i.is_relevant = TRUE AND ic.sentiment IS NOT NULL "
            f"AND ({like_clauses})",
            params,
        )
        sums: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            d = _week_id_to_date(r["w"])
            if d is None:
                continue
            key = f"{d.year}-{d.month:02d}"
            sums[key] += float(r["s"])
            counts[key] += 1
        return {k: sums[k] / counts[k] for k in sums}
    # weekly (default)
    rows = storage.query(
        f"SELECT i.week_id AS w, AVG(ic.sentiment) AS s "
        f"FROM items i "
        f"LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        f"WHERE i.is_relevant = TRUE AND ic.sentiment IS NOT NULL "
        f"AND ({like_clauses}) "
        f"GROUP BY i.week_id",
        params,
    )
    return {r["w"]: float(r["s"]) for r in rows if r.get("s") is not None}


def build_charts(
    out_dir: Path,
    competitors: list[Any],
    buckets: int = 12,
    sentiment_thresholds: Optional[dict] = None,
) -> dict[str, str]:
    """Generate trend chart PNGs. Returns {kind: relative_path} or empty
    dict if matplotlib is unavailable or there's no history yet.

    Chart 1 shows category counts per bucket (bugs / features / positive /
    negative). Chart 2 shows sentiment lines (product + competitors).
    Weekly buckets below the switch threshold (default 365 days of
    history), monthly buckets past that.
    """
    plt = _mpl()
    if plt is None:
        log.warning("matplotlib_unavailable_charts_skipped")
        return {}

    thr = sentiment_thresholds or {}
    pos_t = float(thr.get("positive", 0.2))
    neg_t = float(thr.get("negative", -0.2))

    # Gap #5 (Slice 6): bucket mode adapts to history length. Under a
    # year of data → weekly buckets; past that → monthly aggregation.
    mode = _bucket_mode()
    history = (
        _monthly_history(buckets, pos_t, neg_t) if mode == "monthly"
        else _weekly_history(buckets, pos_t, neg_t)
    )
    if not history:
        log.info("chart_history_empty_charts_skipped", mode=mode)
        return {}
    if len(history) < 2:
        # One bucket of data isn't a trend — hide the whole Trends section
        # until we have at least two runs to compare. The digest template
        # skips the section entirely when this returns {}.
        log.info("chart_history_too_short_charts_skipped",
                 mode=mode, buckets=len(history))
        return {}

    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    weeks    = [r["week_id"] for r in history]
    bugs     = [int(r.get("bugs")     or 0) for r in history]
    features = [int(r.get("features") or 0) for r in history]
    positive = [int(r.get("positive") or 0) for r in history]
    negative = [int(r.get("negative") or 0) for r in history]
    avg_sent      = [float(r.get("avg_sent")      or 0.0) for r in history]
    weighted_sent = [float(r.get("weighted_sent") or 0.0) for r in history]

    outputs: dict[str, str] = {}

    # ---- Chart 1: 4-line category trend (bugs / features / positive /
    # negative). Filename kept as trend_bugs.png so existing templates that
    # reference `charts.bugs` keep resolving without a data-key rename.
    fig, ax = plt.subplots(figsize=(8, 2.8), dpi=100)
    series = [
        ("Bugs",     bugs,     "#c5221f"),   # red
        ("Features", features, "#1a73e8"),   # blue
        ("Positive", positive, "#137333"),   # green
        ("Negative", negative, "#b06000"),   # amber
    ]
    for label, ys, color in series:
        ax.plot(weeks, ys, color=color, linewidth=2, label=label, marker="o",
                markersize=3.5)
        # Emphasize the current bucket (last data point) with a bigger marker.
        if ys:
            ax.plot(weeks[-1:], ys[-1:], color=color, marker="o",
                    markersize=6, linestyle="none")
    ax.set_ylabel("Mentions")
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9, ncol=4)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(data_dir / "trend_bugs.png", bbox_inches="tight")
    plt.close(fig)
    outputs["bugs"] = "data/trend_bugs.png"

    # ---- Chart 2: sentiment lines (product weighted + avg + competitors) ----
    fig, ax = plt.subplots(figsize=(8, 2.5), dpi=100)
    ax.plot(weeks, weighted_sent, color="#c5221f", linewidth=2.5,
            label="Product (weighted)")
    ax.plot(weeks, avg_sent, color="#c5221f", linewidth=1.5, linestyle="--",
            alpha=0.55, label="Product (avg)")
    from pipeline.product import competitor_color, competitor_display_name
    for i, comp in enumerate(competitors[:4]):
        by_bucket = _competitor_sentiment_per_bucket(comp, mode)
        # gaps rendered as broken lines via NaN
        y = [by_bucket.get(w, float("nan")) for w in weeks]
        ax.plot(
            weeks, y,
            color=competitor_color(comp if isinstance(comp, dict) else {}, i),
            linewidth=2,
            label=competitor_display_name(comp),
        )
    ax.axhline(0, color="#dcdfe4", linewidth=1, linestyle="--")
    ax.set_ylabel("Sentiment")
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(data_dir / "trend_sentiment.png", bbox_inches="tight")
    plt.close(fig)
    outputs["sentiment"] = "data/trend_sentiment.png"

    log.info("digest_charts_written", mode=mode, **outputs)
    return outputs
