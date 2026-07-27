"""Digest v2 — the sole report artifact when `digest_v2_enabled` is on.

Replaces the per-area/comments pages produced by pipeline/render.py per
[ADR 0017](../../documents/decisions/0017-digest-v2-sole-render.md).
See [report_v2_design.md](../../documents/report_v2_design.md) for the
full design and [documents/report_v2_mockup.html] for the visual mock.

Slice 3a implements the MVP: config load → section queries → HTML render
of digest index + per-section detail pages. Slice 3b adds matplotlib
charts, the live headline LLM pass, engagement percentile, and the
Competition + Media Coverage sections.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from pipeline import report_config
from pipeline.config import app_config, current_product, resolve_path
from pipeline.digest import charts, render, sections

log = structlog.get_logger()


SECTION_DISPLAY = {
    "positive": "Positive mentions",
    "negative": "Negative mentions",
    "bugs": "Issues / Bugs",
    "features": "Features",
}


def build(run_id: str, week_id: str = "") -> dict[str, Any]:
    """Build the digest for a run.

    Writes:
      reports/<product>/<week>/index.html
      reports/<product>/<week>/{positive,negative,bugs,features}_details.html
        (only for sections enabled in report_config.yaml)

    Returns a counters dict for the run log.
    """
    try:
        product = current_product()
    except Exception:
        log.warning("digest_no_product")
        return {"status": "skipped", "reason": "no_product"}

    if not week_id:
        log.warning("digest_no_week_id")
        return {"status": "skipped", "reason": "no_week_id"}

    cfg = report_config.load(product.id)
    reports_root = resolve_path(app_config()["paths"]["reports_root"])
    out_dir = reports_root / product.id / week_id
    out_dir.mkdir(parents=True, exist_ok=True)

    env = render.env()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Base context shared across templates.
    base_ctx = {
        "product_id": product.id,
        "product_display": product.display,
        "week_id": week_id,
        "run_id": run_id,
        "generated_at": now,
    }

    # ---- Summary + per-section top rows for the index ----
    counts = sections.summary_counts(week_id, cfg["sentiment_thresholds"])
    section_rows: dict[str, list[dict]] = {}
    for section in ("positive", "negative", "bugs", "features"):
        if cfg["sections"].get(section):
            section_rows[section] = sections.section_top_issues(
                product.id, week_id, section, top_n=5
            )
        else:
            section_rows[section] = []

    # ---- Trend charts (matplotlib PNGs; gracefully empty if matplotlib
    # missing or no history yet) ----
    competitors = list(getattr(product, "competitors", None) or [])
    chart_paths = charts.build_charts(
        out_dir,
        competitors=competitors,
        buckets=int(cfg.get("trend_buckets", 12)),
    )

    # ---- Competition rows (opt-in) ----
    competition = []
    if cfg["sections"].get("competition") and competitors:
        competition = sections.competition_rows(
            week_id, prior_week_id=None, competitors=competitors,
        )

    # ---- Media Coverage (auto when data exists) ----
    product_names = [product.display, *(getattr(product, "aliases", None) or [])]
    media = sections.media_coverage_items(week_id, product_names)

    (out_dir / "index.html").write_text(
        env.get_template("digest_index.html.j2").render(
            **base_ctx,
            counts=counts,
            section_rows=section_rows,
            enabled_sections=cfg["sections"],
            charts=chart_paths,
            competitors=competitors,
            competition=competition,
            media=media,
        ),
        encoding="utf-8",
    )

    # ---- Per-section detail pages ----
    pages_written = 1  # index.html
    top_n = int(cfg.get("headline_top_n", 25))
    for section, display in SECTION_DISPLAY.items():
        if not cfg["sections"].get(section):
            continue
        issues = sections.section_all_issues(product.id, section, limit=top_n * 4)
        for issue in issues:
            issue["raw_items"] = sections.raw_items_for_issue(
                issue["issue_id"], section
            )
        (out_dir / f"{section}_details.html").write_text(
            env.get_template("digest_detail.html.j2").render(
                **base_ctx,
                section_id=section,
                section_display=display,
                issues=issues,
            ),
            encoding="utf-8",
        )
        pages_written += 1

    log.info("digest_built", pages=pages_written, out=str(out_dir))
    return {
        "status": "ok",
        "counters": {
            "pages": pages_written,
            "sections_enabled": sum(1 for v in cfg["sections"].values() if v),
        },
        "out_dir": str(out_dir),
    }
