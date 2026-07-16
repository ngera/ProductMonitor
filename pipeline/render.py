"""Render stage — static HTML reports (DESIGN.md §4.11, §13).

Jinja autoescape is ON globally; Reddit-authored content is never marked |safe.
A build-time validator fails the render if any item-displaying template omits
the shared `_item_attribution.html.j2` partial.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import storage
from pipeline.config import current_product, enabled_areas, project_root, resolve_path, app_config

log = structlog.get_logger()

TEMPLATE_DIR = project_root() / "report_templates"
ATTRIBUTION_PARTIAL = "_item_attribution.html.j2"

# Templates that render individual items MUST include the attribution partial (§13).
ITEM_DISPLAYING_TEMPLATES = ["area.html.j2", "comments.html.j2"]


class AttributionViolation(RuntimeError):
    pass


def validate_templates() -> None:
    for name in ITEM_DISPLAYING_TEMPLATES:
        text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        if ATTRIBUTION_PARTIAL not in text:
            raise AttributionViolation(
                f"{name} displays items but does not include {ATTRIBUTION_PARTIAL} (§13)"
            )
    log.info("attribution_validated", templates=len(ITEM_DISPLAYING_TEMPLATES))


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2", "html.j2"], default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _group_label(group_key: str) -> str:
    parts = group_key.split(":")
    kind = parts[0]
    if kind == "entity":
        # entity:{area}:{type}:{vendor}:{product}
        return f"{parts[3]} {parts[4]} ({parts[2]})" if len(parts) >= 5 else group_key
    if kind == "kb":
        return f"Update {parts[2]}" if len(parts) >= 3 else group_key
    return "Similar reports"


def run_render(week_id: str, *, run_id: str = "") -> dict[str, Any]:
    validate_templates()
    env = _env()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reports_root = resolve_path(app_config()["paths"]["reports_root"])
    # Per-topic report tree: reports/<topic_id>/<week_id>/. Legacy fallback
    # (reports/<week_id>/) when no topic is loaded.
    try:
        product_id = current_product().id
        out_dir = reports_root / product_id / week_id
    except Exception:
        product_id = ""
        out_dir = reports_root / week_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data").mkdir(exist_ok=True)

    # POST_V1_PLAN §4.10 D9 — optional acceptance gate. When enabled AND
    # the eval scorecard has failures, we replace the normal report with a
    # diagnostic gate-failure page so operators can see what regressed.
    if product_id and run_id:
        gate = _check_acceptance_gate(product_id, run_id)
        if gate is not None:
            (out_dir / "index.html").write_text(
                _render_gate_failure_page(env, week_id, now, gate), encoding="utf-8",
            )
            log.warning("acceptance_gate_failed", **gate)
            return {"counters": {"pages": 1, "gated": 1}, "gate": gate}

    rollups = {
        r["area"]: r
        for r in storage.query("SELECT * FROM weekly_rollup WHERE week_id=?", [week_id])
    }
    area_display = {a["id"]: a.get("display", a["id"]) for a in enabled_areas()}

    total_items = sum(r["item_count"] or 0 for r in rollups.values())
    total_groups = sum(r["group_count"] or 0 for r in rollups.values())
    total_bugs = sum(r["bug_count"] or 0 for r in rollups.values())

    source_counts = storage.query(
        "SELECT source_display_name, COUNT(*) AS n FROM items "
        "WHERE week_id=? AND is_relevant=TRUE GROUP BY source_display_name ORDER BY n DESC",
        [week_id],
    )

    # Index area cards
    area_cards = []
    for area, r in sorted(rollups.items(), key=lambda kv: -(kv[1]["item_count"] or 0)):
        top_groups = [
            {**g, "label": _group_label(g["group_key"])}
            for g in json.loads(r.get("top_group_keys_json") or "[]")
        ]
        area_cards.append({
            "area": area, "display": area_display.get(area, area),
            "item_count": r["item_count"], "group_count": r["group_count"],
            "bug_count": r["bug_count"], "avg_sentiment": r["avg_sentiment"],
            "severity_max": r["severity_max"], "top_groups": top_groups,
        })

    top_vendor = _global_top_vendor(week_id)

    (out_dir / "index.html").write_text(
        env.get_template("index.html.j2").render(
            week_id=week_id, generated_at=now, areas=area_cards,
            total_items=total_items, total_groups=total_groups, total_bugs=total_bugs,
            source_counts=source_counts, top_vendor=top_vendor,
        ),
        encoding="utf-8",
    )

    pages = 1
    for area, r in rollups.items():
        _render_area(env, out_dir, week_id, now, area, area_display.get(area, area), r)
        _render_comments(env, out_dir, week_id, now, area, area_display.get(area, area))
        pages += 2

    log.info("rendered", pages=pages, out=str(out_dir))
    return {"pages": pages, "out_dir": str(out_dir)}


# ---------------------------------------------------------------------------
# Acceptance gate helpers (POST_V1_PLAN §4.10 D9)
# ---------------------------------------------------------------------------


def _check_acceptance_gate(product_id: str, run_id: str) -> dict[str, Any] | None:
    """Return a gate-failure dict when the gate is on AND this run's eval
    has failures; else None (gate off, no summary, or all metrics pass).

    Two conditions must both be true for the gate to activate:
      1. product.yaml `eval.acceptance_gate: true`
      2. eval_summary.json exists, status == "ok", and either
         `regressions` is non-empty OR any metric falls below its
         product-configured threshold.
    """
    from pipeline import eval as _eval
    from pipeline.config import current_product

    try:
        product_meta = current_product().product_meta
    except Exception:
        return None
    eval_cfg = (product_meta or {}).get("eval") or {}
    if not bool(eval_cfg.get("acceptance_gate", False)):
        return None

    summary = _eval.load_summary(product_id, run_id)
    if not summary or summary.get("status") != "ok":
        return None

    failures: list[dict[str, Any]] = []
    thresholds = eval_cfg.get("thresholds") or {}
    for name, m in (summary.get("metrics") or {}).items():
        point = m.get("f1") if m.get("f1") is not None else m.get("accuracy")
        threshold = thresholds.get(name)
        if threshold is not None and point is not None and point < float(threshold):
            failures.append({
                "metric": name, "value": point,
                "threshold": float(threshold), "reason": "below threshold",
            })

    for regressed_metric in summary.get("regressions") or []:
        failures.append({
            "metric": regressed_metric, "value": None,
            "threshold": None, "reason": "regressed vs rolling median",
        })

    if not failures:
        return None
    return {
        "run_id": run_id,
        "product_id": product_id,
        "failures": failures,
        "summary_ref": f"/products/{product_id}/runs/{run_id}",
    }


def _render_gate_failure_page(env, week_id: str, now: str, gate: dict[str, Any]) -> str:
    """Standalone HTML page shown in place of the normal index when the
    acceptance gate blocks render. Self-contained — no template needed."""
    row_parts = []
    for f in gate["failures"]:
        value_cell = "" if f["value"] is None else "{:.3f}".format(f["value"])
        threshold_cell = "" if f["threshold"] is None else "{:.3f}".format(f["threshold"])
        row_parts.append(
            "<tr>"
            f"<td><code>{f['metric']}</code></td>"
            f"<td>{value_cell}</td>"
            f"<td>{threshold_cell}</td>"
            f"<td>{f['reason']}</td>"
            "</tr>"
        )
    rows = "".join(row_parts)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>Report gated · week {week_id}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#222;}"
        "h1{color:#a00;}table{border-collapse:collapse;width:100%;margin:1rem 0;}"
        "td,th{border-bottom:1px solid #ddd;padding:0.4rem 0.6rem;text-align:left;font-size:0.9rem;}"
        "th{color:#666;text-transform:uppercase;font-size:0.7rem;letter-spacing:0.05em;}"
        "code{background:#f5f5f5;padding:0.05rem 0.25rem;border-radius:3px;}"
        ".hint{color:#666;font-size:0.9rem;}</style></head><body>"
        "<h1>Report gated by acceptance evals</h1>"
        f"<p class=\"hint\">Week <code>{week_id}</code> · generated {now} · "
        f"run <code>{gate['run_id']}</code></p>"
        "<p>The acceptance gate is enabled for this product and one or more "
        "eval metrics failed. The normal report is not shown; fix the "
        "underlying issue (prompt regression, snippet drift, model change) "
        "before rerunning, or disable "
        "<code>eval.acceptance_gate</code> in product.yaml.</p>"
        "<table><thead><tr><th>Metric</th><th>Value</th><th>Threshold</th>"
        f"<th>Reason</th></tr></thead><tbody>{rows}</tbody></table>"
        f"<p><a href=\"{gate['summary_ref']}\">Run detail →</a></p>"
        "</body></html>"
    )


def _render_area(env, out_dir, week_id, now, area, display, rollup) -> None:
    rows = storage.query(
        "SELECT wg.group_key, wg.member_count, wg.canonical_item_id, "
        "i.url, i.title, i.author, i.created_at, i.source_display_name, ic.summary "
        "FROM week_groups wg "
        "JOIN items i ON i.id = wg.canonical_item_id "
        "LEFT JOIN item_classifications ic ON ic.item_id = wg.canonical_item_id "
        "WHERE wg.week_id=? AND wg.area=? ORDER BY wg.member_count DESC",
        [week_id, area],
    )
    groups = []
    for r in rows:
        groups.append({
            "label": _group_label(r["group_key"]),
            "member_count": r["member_count"],
            "canonical": {
                "item_id": r["canonical_item_id"], "url": r["url"], "title": r["title"],
                "summary": r["summary"], "author": r["author"],
                "created_at": str(r["created_at"]), "source_display_name": r["source_display_name"],
            },
        })
    top_vendors = json.loads(rollup.get("top_vendors_json") or "[]")
    (out_dir / f"area_{area}.html").write_text(
        env.get_template("area.html.j2").render(
            week_id=week_id, generated_at=now, area=area, display=display,
            rollup=rollup, groups=groups, top_vendors=top_vendors,
        ),
        encoding="utf-8",
    )


def _render_comments(env, out_dir, week_id, now, area, display) -> None:
    rows = storage.query(
        """
        SELECT i.id, i.url, i.title, i.body, i.author, i.created_at, i.source_display_name,
               ic.summary, ic.sentiment, ic.confidence, ic.content_types_json,
               ba.severity
        FROM items i
        JOIN item_areas ia ON ia.item_id = i.id
        JOIN item_classifications ic ON ic.item_id = i.id
        LEFT JOIN bug_attributes ba ON ba.item_id = i.id
        WHERE i.week_id = ? AND ia.area = ? AND i.is_relevant = TRUE
        ORDER BY i.created_at DESC
        """,
        [week_id, area],
    )
    items = []
    for r in rows:
        ents = storage.query(
            "SELECT vendor, product, role FROM entity_mentions WHERE item_id=?", [r["id"]]
        )
        items.append({
            "url": r["url"], "title": r["title"], "body": r["body"] or "",
            "summary": r["summary"], "author": r["author"], "created_at": str(r["created_at"]),
            "source_display_name": r["source_display_name"], "sentiment": r["sentiment"],
            "confidence": r["confidence"], "severity": r["severity"],
            "content_types": json.loads(r.get("content_types_json") or "[]"),
            "entities": ents,
        })
    (out_dir / f"comments_{area}.html").write_text(
        env.get_template("comments.html.j2").render(
            week_id=week_id, generated_at=now, area=area, display=display, items=items,
        ),
        encoding="utf-8",
    )


def _global_top_vendor(week_id: str) -> dict[str, Any] | None:
    rows = storage.query(
        "SELECT em.vendor, COUNT(*) AS n FROM entity_mentions em "
        "JOIN items i ON i.id=em.item_id WHERE i.week_id=? AND i.is_relevant=TRUE "
        "GROUP BY em.vendor ORDER BY n DESC LIMIT 1",
        [week_id],
    )
    return {"vendor": rows[0]["vendor"], "count": rows[0]["n"]} if rows else None
