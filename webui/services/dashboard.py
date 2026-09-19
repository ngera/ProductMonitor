"""Install-wide and product dashboard view models (Obsidian Console, ADR-0032).

Builds the §6.1 / §6.2 context for index.html and product.html Summary.
Best-effort: warehouse locks and missing tables degrade to empty defaults
rather than crashing the page.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from webui.services import runs as _runs


# Pipeline stage names for the live-run strip (digest v2 order).
PIPELINE_STAGES = (
    "fetch", "normalize", "filter", "relevance", "classify",
    "group", "score", "persist", "render",
)


@dataclass
class AttentionItem:
    severity: str  # critical | warning
    tag: str       # SOURCE | RUNNER | COST | LATENCY | QUALITY
    lead: str
    body: str = ""
    at: str = ""
    href: str = ""
    action: str = "Inspect"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ago(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    if dt is None:
        return "—"
    now = now or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    return f"{secs // 86400}d ago"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".rstrip("0").rstrip(".")
    return str(n)


def _fmt_cost(c: float) -> str:
    return f"${c:,.2f}"


def _attention_cfg() -> dict[str, Any]:
    try:
        from pipeline.config import app_config
        return dict(app_config().get("attention") or {})
    except Exception:
        return {}


def _usage_until() -> datetime:
    """Floor 'now' to the minute so cross_product_totals cache keys stay stable
    across the many lookups a single Products page used to make."""
    n = _now()
    return n.replace(second=0, microsecond=0)


def _product_tokens_30d(product_id: str) -> tuple[int, float, int, float]:
    """Return (tokens_30d, cost_30d, tokens_prior_30d, cost_prior_30d).

    One warehouse pass — prior window comes from include_prior on the same
    filtered query (do not call cross_product_totals twice).
    """
    try:
        from pipeline import token_usage as tu
        until = _usage_until()
        since = until - timedelta(days=30)
        cur = tu.cross_product_totals(
            since, until, group_by=("product_id",),
            filters={"product_id": product_id},
        )
        t = cur.get("totals") or {}
        return (
            int(t.get("tokens") or 0),
            float(t.get("cost_usd") or 0.0),
            int(t.get("prior_tokens") or 0),
            float(t.get("prior_cost_usd") or 0.0),
        )
    except Exception:
        return 0, 0.0, 0, 0.0


def _install_usage_maps() -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """One-shot 30d usage for the Products page.

    Returns (spend_by_pid, prior_by_pid, token_rows, token_other) where
    spend/prior maps are {product_id: {tokens, cost_usd}} and token_rows
    is the §7.1 chart series. Three warehouse scans total (current, prior,
    stages) instead of O(products × warehouses).
    """
    from pipeline import token_usage as tu

    until = _usage_until()
    since = until - timedelta(days=30)
    prior_since = since - timedelta(days=30)

    by_prod = tu.cross_product_totals(
        since, until, group_by=("product_id",), include_prior=False,
    )
    by_prior = tu.cross_product_totals(
        prior_since, since, group_by=("product_id",), include_prior=False,
    )
    by_stage = tu.cross_product_totals(
        since, until, group_by=("product_id", "stage"), include_prior=False,
    )

    spend: dict[str, dict[str, Any]] = {}
    for s in by_prod.get("series") or []:
        pid = s.get("product_id") or ""
        if pid:
            spend[pid] = {
                "tokens": int(s.get("tokens") or 0),
                "cost_usd": float(s.get("cost_usd") or 0.0),
            }

    prior: dict[str, dict[str, Any]] = {}
    for s in by_prior.get("series") or []:
        pid = s.get("product_id") or ""
        if pid:
            prior[pid] = {
                "tokens": int(s.get("tokens") or 0),
                "cost_usd": float(s.get("cost_usd") or 0.0),
            }

    stages_by_pid: dict[str, dict[str, int]] = {}
    for s in by_stage.get("series") or []:
        pid = s.get("product_id") or ""
        if not pid:
            continue
        stages_by_pid.setdefault(pid, {})[s.get("stage") or ""] = int(
            s.get("tokens") or 0
        )

    rows: list[dict[str, Any]] = []
    other_total = 0
    other_cost = 0.0
    for pid, sp in spend.items():
        stages = stages_by_pid.get(pid) or {}
        relevance = stages.get("relevance", 0)
        classify = stages.get("classify", 0)
        digest = sum(
            v for k, v in stages.items()
            if k in ("digest", "render", "headline", "persist")
        )
        setup = sum(
            v for k, v in stages.items()
            if k and ("wizard" in k or k in ("assistant_wizard", "assistant"))
        )
        total = int(sp["tokens"])
        cost = float(sp["cost_usd"])
        display = pid
        try:
            from pipeline.product import load_product
            display = load_product(pid).display or pid
        except Exception:
            pass
        rate = (cost / total * 1_000_000) if total else 0.0
        rows.append({
            "product_id": pid,
            "display": display,
            "relevance": relevance,
            "classify": classify,
            "digest": digest,
            "setup": setup,
            "total": total,
            "cost": cost,
            "rate": rate,
        })
        other_total += setup
        if total:
            other_cost += cost * (setup / total)

    rows.sort(key=lambda r: r["total"], reverse=True)
    other = {
        "label": "Wizard setup",
        "total": other_total,
        "cost": round(other_cost, 4),
    }
    return spend, prior, rows, other


def token_by_product_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """§7.1 rows + wizard/other bucket for the Products page chart."""
    try:
        _, _, rows, other = _install_usage_maps()
        return rows, other
    except Exception:
        return [], {"label": "Wizard setup", "total": 0, "cost": 0.0}


def enrich_product_card(
    product: dict[str, Any],
    *,
    spend: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Add §6.1 fields onto an existing index product dict.

    `spend` is an optional {product_id: {tokens, cost_usd}} map from
    `_install_usage_maps()` — when provided we skip a per-product warehouse
    scan (the slow path that made the Products page crawl).
    """
    pid = product.get("id") or ""
    out = dict(product)
    out.setdefault("status", "offline")
    out.setdefault("items_last_week", 0)
    out.setdefault("items_delta_pct", None)
    out.setdefault("sentiment", None)
    out.setdefault("n_sources_ok", 0)
    out.setdefault("n_sources_total", int(product.get("n_sources") or 0))
    out.setdefault("last_run_at", None)
    out.setdefault("last_run_status", None)
    out.setdefault("next_run_at", None)
    out.setdefault("tokens_30d", 0)
    out.setdefault("cost_30d", 0.0)
    out.setdefault("last_run_ago", "—")
    out.setdefault("report_url", None)

    if product.get("error") or not pid:
        out["status"] = "failing"
        return out

    try:
        summary = _runs.product_dashboard_summary(pid)
    except Exception:
        summary = {}

    latest = summary.get("latest_run")
    success = summary.get("latest_success")
    if latest:
        out["last_run_status"] = latest.get("status")
        out["last_run_at"] = latest.get("started_at")
        if latest.get("started_at"):
            try:
                dt = datetime.fromisoformat(latest["started_at"])
                out["last_run_ago"] = _fmt_ago(dt)
            except Exception:
                pass
        st = latest.get("status") or ""
        if st == "running":
            out["status"] = "degraded"
        elif st in ("failed", "crashed"):
            out["status"] = "failing"
        elif st == "success":
            out["status"] = "ok"
        elif st == "partial":
            out["status"] = "degraded"
        else:
            out["status"] = "idle" if not success else "ok"
    else:
        out["status"] = "offline"

    out["report_url"] = summary.get("latest_report_url")

    trend = summary.get("trend") or []
    if trend:
        last = trend[-1]
        out["items_last_week"] = int(last.get("item_count") or 0)
        out["sentiment"] = last.get("avg_sentiment")
        if len(trend) >= 2:
            prev = int(trend[-2].get("item_count") or 0)
            cur = out["items_last_week"]
            if prev > 0:
                out["items_delta_pct"] = round((cur - prev) / prev * 100, 1)

    # Source readiness
    try:
        from pipeline.product import load_product
        from webui import source_health as sh
        p = load_product(pid)
        ready = sh.compute_readiness(p.sources or [])
        out["n_sources_total"] = len(ready) or len(p.sources or [])
        out["n_sources_ok"] = sum(1 for r in ready if r.status == "ready")
        if out["n_sources_total"] and out["n_sources_ok"] < out["n_sources_total"]:
            if out["status"] == "ok":
                out["status"] = "degraded"
    except Exception:
        pass

    if spend is not None:
        sp = spend.get(pid) or {}
        out["tokens_30d"] = int(sp.get("tokens") or 0)
        out["cost_30d"] = float(sp.get("cost_usd") or 0.0)
    else:
        tokens, cost, _, _ = _product_tokens_30d(pid)
        out["tokens_30d"] = tokens
        out["cost_30d"] = cost

    # Stale running marker → failing
    try:
        logs = _runs.run_logs_dir(pid)
        if logs.exists():
            for mk in logs.glob("*.running"):
                age = _now().timestamp() - mk.stat().st_mtime
                # 2× shortest common cadence (daily ≈ 86400) → 48h
                if age > 2 * 86400:
                    out["status"] = "failing"
    except Exception:
        pass

    return out


def collect_attention(
    product_ids: Optional[list[str]] = None,
    *,
    spend: Optional[dict[str, dict[str, Any]]] = None,
    prior: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """SOURCE / RUNNER / COST attention rows. Scoped when product_ids given.

    Optional `spend` / `prior` maps from `_install_usage_maps()` avoid a
    per-product warehouse scan for the COST rule.
    """
    from pipeline.product import available_products, load_product

    cfg = _attention_cfg()
    cost_pct = float(cfg.get("cost_increase_pct") or 50)
    items: list[AttentionItem] = []
    pids = product_ids if product_ids is not None else available_products()

    for pid in pids:
        try:
            product = load_product(pid)
        except Exception:
            continue

        # RUNNER — stale .running marker
        try:
            logs = _runs.run_logs_dir(pid)
            if logs.exists():
                for mk in logs.glob("*.running"):
                    mtime = datetime.fromtimestamp(
                        mk.stat().st_mtime, tz=timezone.utc,
                    )
                    age_h = (_now() - mtime).total_seconds() / 3600
                    if age_h > 48:
                        items.append(AttentionItem(
                            severity="critical",
                            tag="RUNNER",
                            lead=f"{pid} — run marker older than 48h.",
                            body="A pipeline subprocess may be stuck; digests will not update until it is cleared.",
                            at=mtime.strftime("%Y-%m-%d %H:%M"),
                            href=f"/products/{pid}/runs",
                            action="Inspect",
                        ))
        except Exception:
            pass

        # SOURCE — readiness missing / last run health
        try:
            from webui import source_health as sh
            ready = sh.compute_readiness(product.sources or [])
            missing = [r for r in ready if r.status in ("missing", "partial")]
            if missing:
                names = ", ".join(r.display_name for r in missing[:3])
                items.append(AttentionItem(
                    severity="warning",
                    tag="SOURCE",
                    lead=f"{names} — credentials incomplete.",
                    body=f"Affects {product.display}; streams will fetch 0 items until keys are set.",
                    href=missing[0].fix_url or f"/products/{pid}/sources",
                    action="Fix",
                ))
            summary = _runs.product_dashboard_summary(pid)
            latest = summary.get("latest_run")
            if latest and latest.get("status") in ("failed", "crashed", "partial"):
                errs = latest.get("errors") or []
                # Look for source-ish errors
                for e in errs[:2]:
                    msg = e if isinstance(e, str) else (
                        e.get("message") or e.get("error") or str(e)
                    )
                    if any(k in msg.lower() for k in (
                        "403", "401", "rate", "source", "fetch", "reddit",
                        "http",
                    )):
                        items.append(AttentionItem(
                            severity="critical" if "403" in msg or "401" in msg else "warning",
                            tag="SOURCE",
                            lead=f"{pid} — fetch problem on last run.",
                            body=f"{msg[:160]} Items may be missing from this week's digest.",
                            at=(latest.get("started_at") or "")[:16].replace("T", " "),
                            href=f"/products/{pid}/runs/{latest.get('run_id')}",
                            action="Inspect",
                        ))
                        break
        except Exception:
            pass

        # COST — 30d spend up > threshold vs prior 30d
        try:
            if spend is not None and prior is not None:
                cost = float((spend.get(pid) or {}).get("cost_usd") or 0.0)
                prior_cost = float((prior.get(pid) or {}).get("cost_usd") or 0.0)
            else:
                _, cost, _, prior_cost = _product_tokens_30d(pid)
            if prior_cost > 0 and cost > prior_cost * (1 + cost_pct / 100):
                pct = round((cost - prior_cost) / prior_cost * 100)
                items.append(AttentionItem(
                    severity="warning",
                    tag="COST",
                    lead=f"{product.display} — 30-day spend up {pct}%.",
                    body=f"{_fmt_cost(cost)} vs {_fmt_cost(prior_cost)} prior period.",
                    href=f"/admin/tokens?product_id={pid}",
                    action="Review",
                ))
        except Exception:
            pass

    return [i.as_dict() for i in items]


def install_source_health() -> dict[str, Any]:
    """Aggregate stream readiness across all products for §7.2."""
    from pipeline.product import available_products, load_product
    from webui import source_health as sh

    by_type: dict[str, dict[str, int]] = {}
    for pid in available_products():
        try:
            p = load_product(pid)
            for r in sh.compute_readiness(p.sources or []):
                bucket = by_type.setdefault(
                    r.plugin_id,
                    {"type": r.plugin_id, "ok": 0, "slow": 0, "failing": 0, "paused": 0},
                )
                if r.n_streams_paused:
                    bucket["paused"] += r.n_streams_paused
                if r.status == "ready":
                    bucket["ok"] += max(r.n_streams - r.n_streams_paused, 0) or 1
                elif r.status in ("missing", "partial", "unknown_plugin"):
                    bucket["failing"] += max(r.n_streams, 1)
                else:
                    bucket["slow"] += max(r.n_streams, 1)
        except Exception:
            continue

    # fetch_success_14d — approximate from run log status when no finer data
    fetch_success_14d: list[dict[str, Any]] = []
    try:
        day_ok: dict[str, list[int]] = {}
        for pid in available_products():
            logs = _runs.run_logs_dir(pid)
            if not logs.exists():
                continue
            for jf in logs.glob("*.json"):
                try:
                    import json
                    payload = json.loads(jf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                started = _runs.run_id_started_at(
                    payload.get("run_id") or jf.stem,
                )
                if started is None or (_now() - started).days > 14:
                    continue
                day = started.strftime("%Y-%m-%d")
                ok = 1 if payload.get("status") == "success" else 0
                day_ok.setdefault(day, []).append(ok)
        for i in range(13, -1, -1):
            d = (_now() - timedelta(days=i)).strftime("%Y-%m-%d")
            vals = day_ok.get(d) or []
            pct = (sum(vals) / len(vals) * 100) if vals else None
            fetch_success_14d.append({"date": d, "pct": pct})
    except Exception:
        fetch_success_14d = []

    return {
        "by_type": list(by_type.values()),
        "fetch_success_14d": fetch_success_14d,
    }


def product_source_health(product_id: str) -> list[dict[str, Any]]:
    """Per-stream readiness for one product (§6.3)."""
    from pipeline.product import load_product
    from webui import source_health as sh

    try:
        p = load_product(product_id)
    except Exception:
        return []
    out = []
    for r in sh.compute_readiness(p.sources or []):
        status = "ok" if r.status == "ready" else (
            "failing" if r.status in ("missing", "unknown_plugin") else "slow"
        )
        out.append({
            "source_type": r.plugin_id,
            "label": r.display_name,
            "status": status,
            "last_fetch_at": None,
            "last_http_status": None,
            "items_last_run": 0,
            "p95_ms": None,
            "paused": r.n_streams_paused > 0,
        })
    return out


def build_index_context(
    products: list[dict[str, Any]],
    drafts_v2: list[dict[str, Any]],
) -> dict[str, Any]:
    """Full §6.1 context for the Products page."""
    try:
        spend, prior, token_rows, token_other = _install_usage_maps()
    except Exception:
        spend, prior, token_rows, token_other = {}, {}, [], {
            "label": "Wizard setup", "total": 0, "cost": 0.0,
        }

    enriched = [enrich_product_card(p, spend=spend) for p in products]
    n_scheduled = sum(
        1 for p in enriched
        if p.get("status") not in ("offline", "failing") and not p.get("error")
    )
    last_run_at = None
    for p in enriched:
        ts = p.get("last_run_at")
        if ts and (last_run_at is None or ts > last_run_at):
            last_run_at = ts

    streams_ok = sum(int(p.get("n_sources_ok") or 0) for p in enriched)
    streams_total = sum(int(p.get("n_sources_total") or 0) for p in enriched)
    # Header spend/tokens = sum of the same per-product 30d figures shown in
    # the table — never a second aggregation path that can drift. Round each
    # product's cost to cents first so Spend matches Σ(Cost 30d) column.
    tokens_30d = sum(int(p.get("tokens_30d") or 0) for p in enriched)
    cost_30d = sum(
        round(float(p.get("cost_30d") or 0.0), 2) for p in enriched
    )

    last_run_dt = None
    if last_run_at:
        try:
            last_run_dt = datetime.fromisoformat(last_run_at)
        except Exception:
            pass

    attention = collect_attention(
        [p["id"] for p in enriched if p.get("id")],
        spend=spend,
        prior=prior,
    )
    source_health = install_source_health()

    return {
        "products": enriched,
        "install_stats": {
            "n_products": len(enriched),
            "n_drafts": len(drafts_v2),
            "n_scheduled": n_scheduled,
            "last_run_at": last_run_at,
            "last_run_ago": _fmt_ago(last_run_dt),
            "streams_ok": streams_ok,
            "streams_total": streams_total,
            "tokens_30d": tokens_30d,
            "tokens_30d_fmt": _fmt_tokens(tokens_30d),
            "cost_30d": cost_30d,
            "cost_30d_fmt": _fmt_cost(cost_30d),
            "streams_warn": streams_total > 0 and streams_ok < streams_total,
        },
        "attention": attention,
        "token_by_product": token_rows,
        "token_other": token_other,
        "source_health": source_health,
        "fmt_tokens": _fmt_tokens,
        "fmt_cost": _fmt_cost,
    }


def _live_run(product_id: str) -> Optional[dict[str, Any]]:
    logs = _runs.run_logs_dir(product_id)
    if not logs.exists():
        return None
    markers = sorted(logs.glob("*.running"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not markers:
        return None
    mk = markers[0]
    run_id = mk.stem
    # Try to infer stage from sibling .out tail — best-effort.
    stage = "fetch"
    stage_index = 0
    out_path = logs / f"{run_id}.out"
    if out_path.exists():
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            for i, name in enumerate(PIPELINE_STAGES):
                if f"stage_start stage={name}" in text or f'"stage": "{name}"' in text:
                    stage = name
                    stage_index = i
        except Exception:
            pass
    elapsed = max(0, int(_now().timestamp() - mk.stat().st_mtime))
    return {
        "run_id": run_id,
        "stage": stage,
        "stage_index": stage_index,
        "stage_total": len(PIPELINE_STAGES),
        "stages": list(PIPELINE_STAGES),
        "items_done": 0,
        "items_total": 0,
        "elapsed_s": elapsed,
        "eta_s": None,
        "tokens_so_far": 0,
        "cost_so_far": 0.0,
    }


def _items_kept_by_source(
    product_id: str, week_id: Optional[str] = None,
) -> dict[str, int]:
    """Count kept items per `items.source` (plugin / type key).

    Kept ≈ filter passed and not marked irrelevant. Opens the product
    warehouse read-only so Summary never depends on process-global
    `current_product()`.
    """
    import duckdb

    wpath = (
        Path(_runs.product_data_root(product_id)) / "warehouse.duckdb"
    )
    if not wpath.exists():
        return {}
    try:
        con = duckdb.connect(str(wpath), read_only=True)
    except Exception:
        return {}
    try:
        has = con.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = 'items'"
        ).fetchone()
        if not has or not has[0]:
            return {}
        if week_id:
            rows = con.execute(
                """SELECT source, COUNT(*) FROM items
                   WHERE week_id = ?
                     AND COALESCE(filter_status, 'passed') = 'passed'
                     AND (is_relevant IS NULL OR is_relevant = TRUE)
                     AND source IS NOT NULL AND source <> ''
                   GROUP BY source""",
                [week_id],
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT source, COUNT(*) FROM items
                   WHERE COALESCE(filter_status, 'passed') = 'passed'
                     AND (is_relevant IS NULL OR is_relevant = TRUE)
                     AND source IS NOT NULL AND source <> ''
                   GROUP BY source""",
            ).fetchall()
        return {str(r[0]): int(r[1] or 0) for r in rows if r[0]}
    except Exception:
        return {}
    finally:
        con.close()


def extend_dashboard_summary(product_id: str, base: dict[str, Any]) -> dict[str, Any]:
    """Add §6.2 fields onto product_dashboard_summary output."""
    out = dict(base)
    now_iso = _now().isocalendar()
    current_week = f"{now_iso[0]}-W{now_iso[1]:02d}"

    trend = []
    for t in base.get("trend") or []:
        row = dict(t)
        row["partial"] = (row.get("week_id") == current_week)
        trend.append(row)
    out["trend"] = trend

    out["live_run"] = _live_run(product_id)

    # KPIs from latest success / trend
    fetched = int((base.get("totals") or {}).get("fetched") or 0)
    kept = int((base.get("totals") or {}).get("kept") or 0)
    trend_list = out["trend"]
    items_cur = int(trend_list[-1]["item_count"]) if trend_list else 0
    items_prev = int(trend_list[-2]["item_count"]) if len(trend_list) >= 2 else 0
    fetched_delta = None
    if items_prev > 0:
        fetched_delta = round((items_cur - items_prev) / items_prev * 100, 1)
    sentiment = trend_list[-1].get("avg_sentiment") if trend_list else None
    sentiment_prev = trend_list[-2].get("avg_sentiment") if len(trend_list) >= 2 else None
    sentiment_delta = None
    if sentiment is not None and sentiment_prev is not None:
        sentiment_delta = round(sentiment - sentiment_prev, 3)

    streams = product_source_health(product_id)
    sources_ok = sum(1 for s in streams if s["status"] == "ok")
    sources_failing = sum(1 for s in streams if s["status"] == "failing")
    sources_slow = sum(1 for s in streams if s["status"] == "slow")

    out["kpis"] = {
        "fetched": items_cur or fetched,
        "fetched_delta_pct": fetched_delta,
        "fetched_prev": items_prev,
        "kept": kept,
        "kept_pct": round(kept / fetched * 100, 1) if fetched else None,
        "holdout_precision": None,
        "sentiment": sentiment,
        "sentiment_delta": sentiment_delta,
        "sentiment_rank_note": "",
        "sources_ok": sources_ok,
        "sources_total": len(streams),
        "sources_failing": sources_failing,
        "sources_slow": sources_slow,
    }

    # by_source: volume from warehouse items; tokens from llm_usage
    # (source_id = items.source / plugin type, set per-call in relevance/classify).
    by_source: list[dict[str, Any]] = []
    latest = base.get("latest_success") or base.get("latest_run")
    week_for_volume = (latest or {}).get("week_id")
    items_by_source = _items_kept_by_source(product_id, week_for_volume)
    # Fall back to all weeks when the latest run's week has no kept rows yet.
    if not items_by_source and week_for_volume:
        items_by_source = _items_kept_by_source(product_id, None)

    tokens_by_source: dict[str, int] = {}
    if latest and latest.get("run_id"):
        try:
            from pipeline import token_usage as tu
            pr = tu.per_run_totals(product_id, latest["run_id"])
            for sid, info in (pr.get("by_source") or {}).items():
                tokens_by_source[sid] = int(info.get("tokens") or 0)
        except Exception:
            pass

    for s in streams:
        stype = s["source_type"]
        label = s["label"]
        tokens = (
            tokens_by_source.get(stype, 0)
            or tokens_by_source.get(label, 0)
        )
        by_source.append({
            "source_type": stype,
            "label": label,
            "items": int(items_by_source.get(stype, 0)),
            "tokens": tokens,
            "cost": 0.0,
            "status": s["status"],
        })
    out["by_source"] = by_source

    # recent_runs — five newest
    recent: list[dict[str, Any]] = []
    logs = _runs.run_logs_dir(product_id)
    if logs.exists():
        import json
        for jf in sorted(logs.glob("*.json"), reverse=True)[:5]:
            try:
                payload = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            run_id = payload.get("run_id") or jf.stem
            started = _runs.run_id_started_at(run_id)
            durations = payload.get("stage_durations") or {}
            duration_s = int(sum(
                float(v) for v in durations.values()
                if isinstance(v, (int, float))
            )) if durations else None
            counters = payload.get("counters") or {}
            items = 0
            for cvals in counters.values():
                if isinstance(cvals, dict) and isinstance(cvals.get("kept"), (int, float)):
                    items += int(cvals["kept"])
            tokens = 0
            cost = 0.0
            try:
                from pipeline import token_usage as tu
                pr = tu.per_run_totals(product_id, run_id)
                tokens = int(pr.get("total_tokens") or 0)
                cost = float(tu.estimate_cost_usd(
                    "unknown", pr.get("prompt_tokens") or 0,
                    pr.get("completion_tokens") or 0,
                ) or 0.0)
            except Exception:
                pass
            week_id = payload.get("week_id")
            report_url = None
            if week_id:
                cand = _runs.reports_root_for(product_id) / week_id / "index.html"
                if cand.exists():
                    report_url = f"/products/{product_id}/reports/{week_id}/"
            status = payload.get("status") or "unknown"
            failed_stage = None
            if status in ("failed", "crashed"):
                errs = payload.get("errors") or []
                if errs:
                    e0 = errs[0]
                    failed_stage = e0.get("stage") if isinstance(e0, dict) else None
            recent.append({
                "week_id": week_id,
                "run_id": run_id,
                "report_url": report_url,
                "started_at": started.strftime("%b %d, %H:%M") if started else "—",
                "duration_s": duration_s,
                "items": items,
                "tokens": tokens,
                "cost": cost,
                "status": status,
                "failed_stage": failed_stage,
            })
    out["recent_runs"] = recent
    out["attention"] = collect_attention([product_id])
    return out
