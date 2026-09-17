"""Run-log inspection service (ADR-0026).

Every route that reads `data/<product>/run_logs/*.json` — the runs UI,
`/healthz`, the runs-detail page, the wizard's post-run smoke test —
goes through this module. Previously each of those hit the filesystem
directly with its own copy of the traversal; this is where those copies
consolidate.

Pure Python: no FastAPI, no template rendering. Raises `FileNotFoundError`
when a run doesn't exist. Callers translate to HTTP.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pipeline.config import app_config, resolve_path


def product_data_root(product_id: str) -> Path:
    """Per-product data directory: `data/<product_id>/`."""
    return resolve_path(app_config()["paths"]["data_root"]) / product_id


def run_logs_dir(product_id: str) -> Path:
    """Per-product run-log directory: `data/<product_id>/run_logs/`."""
    return product_data_root(product_id) / "run_logs"


def reports_root_for(product_id: str) -> Path:
    """Per-product rendered-report root: `reports/<product_id>/`."""
    return resolve_path(app_config()["paths"]["reports_root"]) / product_id


def run_id_started_at(run_id: str) -> Optional[datetime]:
    """Parse a UTC datetime out of a run_id like `ui-20260915T120000-abc123`
    or `sched-20260915T120000-abc123`. Returns None when the id doesn't
    match the timestamp convention (legacy or manually-crafted ids)."""
    m = re.search(r"(\d{8}T\d{6})", run_id)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def product_dashboard_summary(product_id: str) -> dict:
    """Rich per-product dashboard summary for the product page's Summary tab.

    Combines a filesystem scan of run_logs (status/timing per run) with
    read-only queries against the product's warehouse (items counts,
    weekly_rollup trend, area breakdown). Returns a plain dict — no
    FastAPI or template concerns.

    Fields:
      totals               overall counters across every completed run
      latest_run           the most recent run (any status) as a dict
      latest_success       the most recent status=success run (or None)
      latest_report_url    "/products/<pid>/reports/<week>/" if a report
                           exists on disk for latest_success's week
      run_status_counts    {success: N, partial: N, failed: N, crashed: N,
                            scheduled_skipped: N, running: N}
      by_area              [{area, item_count, avg_sentiment}, ...]
                           from the most recent weekly_rollup week
      trend                [{week_id, item_count, avg_sentiment}, ...]
                           trailing 8 weeks, oldest first
      warehouse_available  True when the warehouse file exists; False
                           for a fresh product with no data yet

    Callers should treat empty/missing sections gracefully (empty lists,
    None fields) — a fresh product has zero data and every field except
    warehouse_available may be defaulted.
    """
    logs_dir = run_logs_dir(product_id)
    warehouse = product_data_root(product_id) / "warehouse.duckdb"

    # ---- Runs from the log dir --------------------------------------------
    runs: list[dict] = []
    if logs_dir.exists():
        for jf in sorted(logs_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                continue
            run_id = payload.get("run_id") or jf.stem
            started = run_id_started_at(run_id)
            runs.append({
                "run_id": run_id,
                "week_id": payload.get("week_id"),
                "status": payload.get("status") or "unknown",
                "started_at": started.isoformat() if started else None,
                "stage_durations": payload.get("stage_durations") or {},
                "counters": payload.get("counters") or {},
                "errors": payload.get("errors") or [],
            })

    # Sort newest-first by embedded run_id timestamp when available.
    runs.sort(
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )

    status_counts: dict[str, int] = {}
    for r in runs:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    latest_run = runs[0] if runs else None
    latest_success = next(
        (r for r in runs if r["status"] == "success"),
        None,
    )

    # Report URL for the latest successful run when the file exists.
    latest_report_url = None
    if latest_success and latest_success.get("week_id"):
        candidate = (
            reports_root_for(product_id) / latest_success["week_id"] / "index.html"
        )
        if candidate.exists():
            latest_report_url = (
                f"/products/{product_id}/reports/{latest_success['week_id']}/"
            )

    # Aggregate totals across every completed run's counters. counters is
    # {stage: {key: int}}; sum only the leaf ints per key, per stage.
    totals = {"runs": len(runs), "fetched": 0, "classified": 0, "kept": 0}
    for r in runs:
        for _stage, cvals in (r.get("counters") or {}).items():
            if not isinstance(cvals, dict):
                continue
            for k in ("fetched", "classified", "kept"):
                if isinstance(cvals.get(k), (int, float)):
                    totals[k] += int(cvals[k])

    # ---- Warehouse-derived facets (guarded — fresh product has no db) ----
    by_area: list[dict] = []
    trend: list[dict] = []
    warehouse_available = warehouse.exists()
    if warehouse_available:
        try:
            import duckdb
            con = duckdb.connect(str(warehouse), read_only=True)
            try:
                # Most recent weekly_rollup week for the area breakdown.
                latest_week = con.execute(
                    "SELECT MAX(week_id) FROM weekly_rollup"
                ).fetchone()
                if latest_week and latest_week[0]:
                    lw = latest_week[0]
                    rows = con.execute(
                        "SELECT area, item_count, avg_sentiment "
                        "FROM weekly_rollup WHERE week_id=? "
                        "ORDER BY item_count DESC",
                        [lw],
                    ).fetchall()
                    by_area = [
                        {
                            "area": r[0],
                            "item_count": int(r[1] or 0),
                            "avg_sentiment": (
                                float(r[2]) if r[2] is not None else None
                            ),
                        }
                        for r in rows
                    ]

                # Trailing 8-week trend from weekly_rollup (aggregated
                # across areas). Oldest-first for chart-friendliness.
                trend_rows = con.execute(
                    "SELECT week_id, SUM(item_count) AS n, "
                    "AVG(weighted_sentiment) AS s "
                    "FROM weekly_rollup GROUP BY week_id "
                    "ORDER BY week_id DESC LIMIT 8"
                ).fetchall()
                trend = list(reversed([
                    {
                        "week_id": r[0],
                        "item_count": int(r[1] or 0),
                        "avg_sentiment": (
                            float(r[2]) if r[2] is not None else None
                        ),
                    }
                    for r in trend_rows
                ]))
            finally:
                con.close()
        except Exception:
            # DuckDB missing or schema mismatch — degrade to empty facets
            # rather than crashing the summary page.
            by_area = []
            trend = []

    return {
        "totals": totals,
        "latest_run": latest_run,
        "latest_success": latest_success,
        "latest_report_url": latest_report_url,
        "run_status_counts": status_counts,
        "by_area": by_area,
        "trend": trend,
        "warehouse_available": warehouse_available,
    }


def last_successful_run_age_seconds(
    product_id: str, now: datetime,
) -> Optional[float]:
    """Age of the most recent status=success run for one product, in
    seconds. None when the product has never produced a successful run."""
    logs_dir = run_logs_dir(product_id)
    if not logs_dir.exists():
        return None
    latest: Optional[datetime] = None
    for jf in logs_dir.glob("*.json"):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (payload.get("status") or "") != "success":
            continue
        # Prefer the run_id timestamp (embedded, monotonic); fall back to
        # the file's mtime for legacy ids that don't encode a timestamp.
        started = run_id_started_at(payload.get("run_id") or jf.stem)
        if started is None:
            try:
                started = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if latest is None or started > latest:
            latest = started
    if latest is None:
        return None
    return (now - latest).total_seconds()
