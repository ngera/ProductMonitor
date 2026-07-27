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


def _competitor_sentiment_per_week(vendor_name: str) -> dict[str, float]:
    """Avg sentiment of items mentioning `vendor_name`, keyed by week_id.

    Item-level attribution per report_v2_design.md §4.2 (honestly labeled on
    the chart). Missing weeks omitted; chart handles gaps as NaN.
    """
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

    history = _weekly_history(buckets)
    if not history:
        log.info("weekly_history_empty_charts_skipped")
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
        by_week = _competitor_sentiment_per_week(comp)
        # gaps rendered as broken lines via NaN
        y = [by_week.get(w, float("nan")) for w in weeks]
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

    log.info("digest_charts_written", **outputs)
    return outputs
