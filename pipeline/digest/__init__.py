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

from pipeline import report_config, storage
from pipeline.config import app_config, current_product, resolve_path
from pipeline.digest import charts, engagement, headlines, render, sections

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

    # Gap #1 (Slice 6) — acceptance gate on the digest path. Per
    # ADR 0017 consequences: "Acceptance gate needs to survive. Slice 3
    # relocates the gate check into the digest build path." Without this,
    # a product with `eval.acceptance_gate: true` loses the safeguard
    # the moment `digest_v2_enabled` flips on. Uses the helpers still
    # living in pipeline/render.py so behavior matches the legacy path
    # exactly.
    from pipeline.render import _check_acceptance_gate, _render_gate_failure_page
    gate = _check_acceptance_gate(product.id, run_id)
    if gate is not None:
        (out_dir / "index.html").write_text(
            _render_gate_failure_page(env, week_id, now, gate),
            encoding="utf-8",
        )
        log.warning("digest_gated_by_evals", **gate)
        return {
            "status": "gated",
            "counters": {"pages": 1, "gated": 1},
            "gate": gate,
            "out_dir": str(out_dir),
        }

    # Header run_id must always point at the pipeline run that produced
    # the warehouse state we're rendering, NOT at whatever id the render
    # was called with. This matters because digest.build() gets invoked
    # from ad-hoc calls (smoke tests, re-renders, manual triggers) with
    # arbitrary caller-supplied ids that don't correspond to real fetch
    # runs. We look up the most recent completed run for this week in the
    # `runs` warehouse table and use its id in the header; falls back to
    # the caller-supplied id only when the table has no entry.
    header_run_id = _latest_completed_run_id(week_id) or run_id

    # Base context shared across templates.
    base_ctx = {
        "product_id": product.id,
        "product_display": product.display,
        "week_id": week_id,
        "week_start": "",   # filled below once week_dates is computed
        "week_end": "",
        "run_id": header_run_id,
        "generated_at": now,
    }

    # Prior-week id used by callouts (WoW deltas) and Competition (Δ vs prior).
    prior_week_id = _prior_week_id(week_id)

    # ---- Summary + per-section top rows for the index ----
    counts = sections.summary_counts(week_id, cfg["sentiment_thresholds"])
    callouts = sections.summary_callouts(
        week_id, prior_week_id, cfg["sentiment_thresholds"],
    )

    # Header week-range dates. Mockup shows `Week of {start} → {end}` and
    # `Data through {end}`. Derived from the ISO week id so no schema change.
    week_dates = _week_date_range(week_id)
    if week_dates:
        base_ctx["week_start"], base_ctx["week_end"] = week_dates
    # Per §5.4: engagement distribution is per-source across the trailing
    # window. Compute once here so both the index's section rows and the
    # detail pages can annotate their raw items without recomputing.
    eng_dist = engagement.distribution_by_source(
        engagement.trailing_week_ids(int(cfg.get("trend_buckets", 12)))
    )

    # Gap #2 (Slice 6) — honor cfg["headline_top_n"]. Was hardcoded 5,
    # which meant the WebUI knob + app.yaml default were dead. Users who
    # want more polished rows / are willing to pay the LLM tokens now
    # actually get what they configured.
    index_top_n = int(cfg.get("headline_top_n", 25))
    section_rows: dict[str, list[dict]] = {}
    for section in ("positive", "negative", "bugs", "features"):
        if cfg["sections"].get(section):
            rows = sections.section_top_issues(
                product.id, week_id, section, top_n=index_top_n
            )
            for row in rows:
                _annotate_issue_row(row, section, eng_dist)
            section_rows[section] = rows
        else:
            section_rows[section] = []

    # ---- Live headline LLM pass for top-N section rows (ADR 0016 §5.3) ----
    # Cache-first via the `headlines` table. When the assistant LLM isn't
    # configured / over budget / call fails, `row.headline` stays None and
    # the template falls back to canonical_title.
    headlines_generated = 0
    headlines_cached = 0
    for section, rows in section_rows.items():
        for row in rows:
            canonical = sections.canonical_item_for_issue(row["issue_id"], section)
            if canonical is None:
                continue
            h = headlines.generate_headline(
                item_id=canonical["id"],
                title=canonical["title"] or row["canonical_title"] or "",
                body=canonical["body"] or "",
                source_display_name=canonical["source_display_name"] or "",
                product_id=product.id,
            )
            if h:
                row["headline"] = h
                headlines_generated += 1

    # ---- Trend charts (matplotlib PNGs; gracefully empty if matplotlib
    # missing or no history yet) ----
    # Competition-analysis flag also gates the per-competitor sentiment
    # lines on the trend chart. When off, `competitors=[]` skips them
    # cleanly.
    from pipeline import features as _features
    _competition_flag = _features.enabled(
        "competition_analysis_enabled", product.id,
    )
    competitors = (
        list(getattr(product, "competitors", None) or [])
        if _competition_flag else []
    )
    chart_paths = charts.build_charts(
        out_dir,
        competitors=competitors,
        buckets=int(cfg.get("trend_buckets", 12)),
        sentiment_thresholds=cfg["sentiment_thresholds"],
    )

    # ---- Competition rows (opt-in AND feature-flag-gated) ----
    # Global `competition_analysis_enabled` flag overrides the per-product
    # opt-in while the feature is being reworked. When off, the digest
    # never queries or renders competitor data even if the product's
    # report_config.sections.competition is true.
    from pipeline import features as _features
    competition = []
    competition_flag_on = _features.enabled(
        "competition_analysis_enabled", product.id,
    )
    if (competition_flag_on
            and cfg["sections"].get("competition")
            and competitors):
        competition = sections.competition_rows(
            week_id, prior_week_id=prior_week_id, competitors=competitors,
        )

    # ---- Media Coverage (auto when data exists) ----
    product_names = [product.display, *(getattr(product, "aliases", None) or [])]
    media = sections.media_coverage_items(week_id, product_names)

    # ---- Press Coverage summary block (ADR-0028) ----
    # Top-of-digest scannable list of items tagged content_type='media_coverage'.
    # Items still appear in their classified area sections; this is an
    # additional grouping so readers can distinguish "the press wrote" from
    # "users said" at a glance. Hidden when press_coverage_top_n <= 0.
    _app_digest = (app_config().get("digest") or {})
    press_top_n = int(_app_digest.get("press_coverage_top_n", 5))
    press_enabled = bool(_app_digest.get("press_coverage_enabled", True))
    press_coverage = (
        sections.press_coverage_items(week_id, top_n=press_top_n)
        if press_enabled else []
    )

    # Force-hide the competition section when the global flag is off,
    # even if the per-product report_config says on. Copy the dict so
    # we don't mutate the underlying cfg.
    _enabled_sections = dict(cfg["sections"] or {})
    if not _competition_flag:
        _enabled_sections["competition"] = False

    (out_dir / "index.html").write_text(
        env.get_template("digest_index.html.j2").render(
            **base_ctx,
            counts=counts,
            callouts=callouts,
            section_rows=section_rows,
            enabled_sections=_enabled_sections,
            charts=chart_paths,
            competitors=competitors,
            competition=competition,
            media=media,
            press_coverage=press_coverage,
        ),
        encoding="utf-8",
    )

    # ---- Per-section detail pages ----
    #
    # Per §5.3: raw items inside each group get a headline pass too
    # (cache-hits are free, budget guard already covers new generations).
    pages_written = 1  # index.html
    top_n = int(cfg.get("headline_top_n", 25))
    for section, display in SECTION_DISPLAY.items():
        if not cfg["sections"].get(section):
            continue
        issues = sections.section_all_issues(product.id, section, limit=top_n * 4)
        unique_sources_section: set[str] = set()
        sev_counts_section = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        total_mentions_section = 0
        for issue in issues:
            raw = sections.raw_items_for_issue(issue["issue_id"], section)
            engagement.annotate_items(raw, eng_dist)
            _annotate_issue_row(issue, section, eng_dist, prefetched_raw=raw)
            # Per-item headline pass — capped at the same top_n groups the
            # index already paid for; each raw item is best-effort.
            per_item_h = headlines.generate_batch(
                [
                    {
                        "item_id": r["id"], "title": r.get("title") or "",
                        "body": r.get("body") or "",
                        "source_display_name": r.get("source_display_name") or "",
                        "summary": r.get("summary") or "",
                    }
                    for r in raw
                ],
                product_id=product.id,
            )
            for r in raw:
                r["headline"] = per_item_h.get(r["id"])
                if r.get("source_display_name"):
                    unique_sources_section.add(r["source_display_name"])
            issue["raw_items"] = raw
            total_mentions_section += int(issue.get("total_mentions") or 0)
            if issue.get("max_severity") in sev_counts_section:
                sev_counts_section[issue["max_severity"]] += 1
        detail_stats = {
            "n_issues": len(issues),
            "total_mentions": total_mentions_section,
            "sev_counts": sev_counts_section,
        }
        (out_dir / f"{section}_details.html").write_text(
            env.get_template("digest_detail.html.j2").render(
                **base_ctx,
                section_id=section,
                section_display=display,
                issues=issues,
                sources=sorted(unique_sources_section),
                detail_stats=detail_stats,
                trend_buckets=int(cfg.get("trend_buckets", 12)),
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


def _prior_week_id(week_id: str) -> str | None:
    """Return the most recent week_id in `items` strictly before this one,
    or None on the first-ever run. Used for WoW deltas and Competition Δ."""
    rows = storage.query(
        "SELECT DISTINCT week_id FROM items WHERE week_id < ? "
        "ORDER BY week_id DESC LIMIT 1",
        [week_id],
    )
    return rows[0]["week_id"] if rows else None


def _latest_completed_run_id(week_id: str) -> str | None:
    """Return the run_id of the most recent completed pipeline run for this
    week. "Completed" = status success OR partial (partial is a real run
    with skipped stages, e.g. --skip-llm). Excludes 'failed' and 'running'
    so a crashed retry doesn't get credit for the good data on disk.

    Fallback source of truth for the digest header — see comment at the
    call site for why we don't just trust the caller's run_id.
    """
    try:
        rows = storage.query(
            "SELECT run_id FROM runs WHERE week_id = ? "
            "AND status IN ('success', 'partial') "
            "ORDER BY finished_at DESC NULLS LAST LIMIT 1",
            [week_id],
        )
    except Exception:
        return None
    return rows[0]["run_id"] if rows else None


def _annotate_issue_row(
    row: dict, section: str, eng_dist: dict,
    *, prefetched_raw: list | None = None,
) -> None:
    """Attach `max_score`, `sources_summary`, `span_weeks` to an issue row.

    Used both for section_top_issues rows (index page) and section_all_issues
    rows (detail page). `prefetched_raw`, when supplied by the detail loop
    that's already loaded raw items for other reasons, avoids the second
    round-trip; otherwise we fetch just enough to compute the annotation.
    """
    if prefetched_raw is None:
        raw = sections.raw_items_for_issue(row["issue_id"], section)
        engagement.annotate_items(raw, eng_dist)
    else:
        raw = prefetched_raw
    scored = [
        int(r["engagement_percentile"]) for r in raw
        if r.get("engagement_percentile") is not None
    ]
    if scored:
        row["max_score"] = max(scored)
    else:
        # Fallback: sources without engagement metrics (RSS, TechCommunity)
        # still deserve a Score column reader can compare across rows. Use
        # a log-scaled mentions proxy that caps at 90 so we never claim the
        # same tier as a truly viral engagement-scored issue.
        import math
        m = int(row.get("total_mentions") or 0)
        row["max_score"] = min(90, int(math.log1p(m) * 25)) if m else None
    row["span_weeks"] = _week_span(
        row.get("first_seen_week"), row.get("last_seen_week"),
    )
    seen_src: list[str] = []
    for r in raw:
        s = r.get("source_display_name") or r.get("source") or ""
        if s and s not in seen_src:
            seen_src.append(s)
    row["sources_summary"] = " · ".join(seen_src[:3])
    # Canonical article — the highest-scored raw item is a good proxy for
    # "the specific article a reader should click through to". Fall back to
    # the most recent raw item when nothing is scored (RSS/media items).
    canonical: dict | None = None
    if raw:
        by_score = sorted(
            raw,
            key=lambda r: (
                r.get("engagement_percentile") or 0,
                str(r.get("created_at") or ""),
            ),
            reverse=True,
        )
        canonical = by_score[0]
    row["canonical_url"] = (canonical or {}).get("url") or ""
    row["canonical_source"] = (
        (canonical or {}).get("source_display_name")
        or (canonical or {}).get("source")
        or ""
    )
    row["canonical_created_at"] = str((canonical or {}).get("created_at") or "")[:10]


def _week_date_range(week_id: str) -> tuple[str, str] | None:
    """Parse an ISO week id like '2026-W31' to (Monday, Sunday) date strings.

    Used by the header to show `Week of 2026-07-27 → 2026-08-02` per the
    mockup. Returns None on parse failure so the template falls back to the
    bare week id.
    """
    from datetime import date, timedelta
    try:
        year_str, wk_str = str(week_id).split("-W")
        monday = date.fromisocalendar(int(year_str), int(wk_str), 1)
    except Exception:
        return None
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def _week_span(first_week: str | None, last_week: str | None) -> int | None:
    """Inclusive week count between two ISO week ids. `2026-W20`→`2026-W22`
    is 3 weeks. Returns None on any parse error."""
    if not first_week or not last_week:
        return None
    from datetime import date
    try:
        y1, w1 = str(first_week).split("-W")
        y2, w2 = str(last_week).split("-W")
        d1 = date.fromisocalendar(int(y1), int(w1), 1)
        d2 = date.fromisocalendar(int(y2), int(w2), 1)
    except Exception:
        return None
    days = (d2 - d1).days
    return max(1, days // 7 + 1)
