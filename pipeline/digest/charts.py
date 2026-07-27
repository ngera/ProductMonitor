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


# Palette used for competitor lines when product.competitors is still the
# plain-string schema (Slice 3b interim). Rich {name, aliases, color} objects
# per report_v2_design.md §7.2 land in Slice 4.
_COMPETITOR_PALETTE = ["#a2a2a2", "#4285f4", "#137333", "#b06000"]


def _mpl():
    """Import matplotlib lazily so unpolished envs still build the digest."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # no display; server-friendly
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        return None


def _weekly_history(buckets: int) -> list[dict[str, Any]]:
    """Trailing N weeks from weekly_rollup, oldest → newest."""
    rows = storage.query(
        "SELECT week_id, "
        "SUM(bug_count) AS bugs, "
        "AVG(avg_sentiment) AS avg_sent, "
        "AVG(weighted_sentiment) AS weighted_sent "
        "FROM weekly_rollup "
        "GROUP BY week_id ORDER BY week_id DESC LIMIT ?",
        [buckets],
    )
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
    `digest.trend_bucket_switch_days` (default 365) of data, the chart
    aggregates to monthly buckets so we don't render 100 tiny weekly bars
    on year-old products. Below that threshold stays weekly.
    """
    from datetime import date
    from pipeline.config import app_config
    switch_days = int(
        (app_config().get("digest") or {}).get("trend_bucket_switch_days", 365)
    )
    rows = storage.query(
        "SELECT MIN(week_id) AS earliest FROM weekly_rollup"
    )
    if not rows or not rows[0].get("earliest"):
        return "weekly"
    earliest = _week_id_to_date(rows[0]["earliest"])
    if earliest is None:
        return "weekly"
    return "monthly" if (date.today() - earliest).days >= switch_days else "weekly"


def _monthly_history(buckets: int) -> list[dict[str, Any]]:
    """Trailing N months aggregated from weekly_rollup, oldest → newest.

    Bucket key is `YYYY-MM`. Bug counts sum across the month; sentiments
    average. Weeks with a non-parseable id are silently dropped.
    """
    from collections import defaultdict
    rows = storage.query(
        "SELECT week_id, bug_count, avg_sentiment, weighted_sentiment "
        "FROM weekly_rollup ORDER BY week_id"
    )
    by_month: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"bugs": 0, "sents": [], "weighted_sents": []}
    )
    for r in rows:
        d = _week_id_to_date(r["week_id"])
        if d is None:
            continue
        key = f"{d.year}-{d.month:02d}"
        b = by_month[key]
        b["bugs"] += int(r.get("bug_count") or 0)
        if r.get("avg_sentiment") is not None:
            b["sents"].append(float(r["avg_sentiment"]))
        if r.get("weighted_sentiment") is not None:
            b["weighted_sents"].append(float(r["weighted_sentiment"]))
    ordered = sorted(by_month.keys())[-buckets:]
    return [
        {
            "week_id": k,  # reused as chart x-axis label
            "bugs": by_month[k]["bugs"],
            "avg_sent": (sum(by_month[k]["sents"]) / len(by_month[k]["sents"]))
                        if by_month[k]["sents"] else 0.0,
            "weighted_sent": (
                sum(by_month[k]["weighted_sents"]) / len(by_month[k]["weighted_sents"])
            ) if by_month[k]["weighted_sents"] else 0.0,
        }
        for k in ordered
    ]


def _competitor_sentiment_per_bucket(vendor_name: str, mode: str) -> dict[str, float]:
    """Avg sentiment of items mentioning `vendor_name`, keyed by bucket.

    Bucket key = `week_id` (e.g. `2026-W30`) in weekly mode, `YYYY-MM` in
    monthly mode. Item-level attribution per report_v2_design.md §4.2 —
    the chart legend labels the caveat.
    """
    if mode == "monthly":
        from collections import defaultdict
        rows = storage.query(
            "SELECT i.week_id AS w, ic.sentiment AS s "
            "FROM entity_mentions em "
            "JOIN items i ON i.id = em.item_id "
            "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
            "WHERE em.vendor = ? AND i.is_relevant = TRUE AND ic.sentiment IS NOT NULL",
            [vendor_name],
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
        "SELECT i.week_id AS w, AVG(ic.sentiment) AS s "
        "FROM entity_mentions em "
        "JOIN items i ON i.id = em.item_id "
        "LEFT JOIN item_classifications ic ON ic.item_id = i.id "
        "WHERE em.vendor = ? AND i.is_relevant = TRUE AND ic.sentiment IS NOT NULL "
        "GROUP BY i.week_id",
        [vendor_name],
    )
    return {r["w"]: float(r["s"]) for r in rows if r.get("s") is not None}


def build_charts(
    out_dir: Path,
    competitors: list[str],
    buckets: int = 12,
) -> dict[str, str]:
    """Generate trend chart PNGs. Returns {kind: relative_path} or empty
    dict if matplotlib is unavailable or there's no history yet."""
    plt = _mpl()
    if plt is None:
        log.warning("matplotlib_unavailable_charts_skipped")
        return {}

    # Gap #5 (Slice 6): bucket mode adapts to history length. Under a
    # year of data → weekly bars; past that → monthly aggregation so the
    # chart doesn't degenerate into dozens of tiny bars.
    mode = _bucket_mode()
    history = _monthly_history(buckets) if mode == "monthly" else _weekly_history(buckets)
    if not history:
        log.info("chart_history_empty_charts_skipped", mode=mode)
        return {}

    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    weeks = [r["week_id"] for r in history]
    bugs = [int(r["bugs"] or 0) for r in history]
    avg_sent = [float(r["avg_sent"] or 0.0) for r in history]
    weighted_sent = [float(r["weighted_sent"] or 0.0) for r in history]

    outputs: dict[str, str] = {}

    # ---- Chart 1: bug count bars, current bucket emphasized ----
    fig, ax = plt.subplots(figsize=(8, 2.5), dpi=100)
    if bugs[:-1]:
        ax.bar(weeks[:-1], bugs[:-1], color="#c5221f", alpha=0.72)
    ax.bar(weeks[-1:], bugs[-1:], color="#8b0000", alpha=1.0)
    ax.set_ylabel("Bugs")
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    (data_dir / "trend_bugs.png").parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(data_dir / "trend_bugs.png", bbox_inches="tight")
    plt.close(fig)
    outputs["bugs"] = "data/trend_bugs.png"

    # ---- Chart 2: sentiment lines (product weighted + avg + competitors) ----
    fig, ax = plt.subplots(figsize=(8, 2.5), dpi=100)
    ax.plot(weeks, weighted_sent, color="#c5221f", linewidth=2.5,
            label="Product (weighted)")
    ax.plot(weeks, avg_sent, color="#c5221f", linewidth=1.5, linestyle="--",
            alpha=0.55, label="Product (avg)")
    for i, comp in enumerate(competitors[:4]):
        by_bucket = _competitor_sentiment_per_bucket(comp, mode)
        # gaps rendered as broken lines via NaN
        y = [by_bucket.get(w, float("nan")) for w in weeks]
        ax.plot(weeks, y, color=_COMPETITOR_PALETTE[i], linewidth=2, label=comp)
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
