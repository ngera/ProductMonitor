"""Pipeline orchestrator (DESIGN.md §4.1).

Stage order:
  Fetch -> Normalize -> Filter -> Relevance(LLM) -> Classify+Extract(LLM)
        -> Score -> Group -> Aggregate -> Render

Score runs before Group because canonical selection (§4.8.3) reads item scores.
LLM stages are skipped (with a clear warning) if Foundry Local is unreachable,
so Fetch/Normalize/Filter still produce raw data.

Usage:
  python -m pipeline.run --week 2026-W22
  python -m pipeline.run                  # current ISO week
  python -m pipeline.run --week 2026-W22 --skip-fetch   # re-run from raw
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import structlog
from dotenv import load_dotenv

from datetime import datetime, timezone

from pipeline import stage_capture, storage
from pipeline.config import resolve_path, app_config, set_current_product, taxonomy_version, vendors_version
from pipeline.product import DEFAULT_PRODUCT, available_products, load_product
from pipeline.util import current_week_id


_TIME_MODES = ("incremental", "last_week", "last_month", "range")


def _parse_iso_date(s: str) -> datetime:
    """Parse 'YYYY-MM-DD' as a UTC date. Anchors at start-of-day."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def compute_effective_window(
    product, *, time_mode: Optional[str] = None,
    since: Optional[str] = None, until: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve the effective (since_ts, until_ts, mode) for this run.

    Precedence: CLI flag > product.time_range > default 'incremental'.

    Returns a dict:
        mode               'incremental' | 'last_week' | 'last_month' | 'range'
        since_ts           epoch seconds (None means "use whatever the
                           cursor already holds")
        until_ts           epoch seconds (None means "no upper bound")
        advance_cursor     bool — False for 'range' so a historical backfill
                           doesn't poison future incremental runs

    Raises ValueError on bad input (unknown mode, range without dates,
    since > until).
    """
    saved = product.time_range or {"mode": "incremental"}
    mode = time_mode or saved.get("mode") or "incremental"
    if mode not in _TIME_MODES:
        raise ValueError(
            f"unknown time mode {mode!r}; expected one of {_TIME_MODES}"
        )

    now = datetime.now(timezone.utc).timestamp()

    if mode == "incremental":
        return {"mode": mode, "since_ts": None, "until_ts": None, "advance_cursor": True}

    if mode == "last_week":
        return {"mode": mode, "since_ts": now - 7 * 86400, "until_ts": now,
                "advance_cursor": True}

    if mode == "last_month":
        return {"mode": mode, "since_ts": now - 30 * 86400, "until_ts": now,
                "advance_cursor": True}

    # mode == "range"
    since_str = since or saved.get("range_from")
    until_str = until or saved.get("range_to")
    if not since_str or not until_str:
        raise ValueError(
            "time-mode 'range' needs both --since and --until "
            "(or persisted range_from + range_to on the product)."
        )
    since_dt = _parse_iso_date(since_str)
    # Until is end-of-day inclusive — add ~1 day so items dated `until` itself
    # are kept. (since_dt = midnight UTC; until_dt = next-midnight UTC.)
    until_dt = _parse_iso_date(until_str).replace(hour=23, minute=59, second=59)
    if since_dt > until_dt:
        raise ValueError(f"since ({since_str}) is after until ({until_str})")
    return {"mode": mode, "since_ts": since_dt.timestamp(),
            "until_ts": until_dt.timestamp(), "advance_cursor": False}

log = structlog.get_logger()


def _code_version() -> str:
    return "v1"


def _run_stage(name: str, fn: Callable[[], dict], durations: dict, results: dict) -> None:
    t0 = time.time()
    log.info("stage_start", stage=name)
    results[name] = fn() or {}
    durations[name] = round(time.time() - t0, 2)
    log.info("stage_done", stage=name, seconds=durations[name])


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Customer Feedback Monitor pipeline")
    parser.add_argument(
        "--product",
        default=DEFAULT_PRODUCT,
        help=f"Product id under products/. Available: {', '.join(available_products()) or '(none)'}",
    )
    parser.add_argument("--week", default=None, help="ISO week id, e.g. 2026-W22")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Use this exact id for the run (webui passes its marker id here so "
             "the marker sidecar and the terminal .json share one filename).",
    )
    parser.add_argument("--skip-fetch", action="store_true", help="re-run from existing raw/warehouse")
    parser.add_argument("--skip-llm", action="store_true", help="skip relevance+classify stages")
    parser.add_argument(
        "--time-mode",
        choices=list(_TIME_MODES),
        default=None,
        help="Time window for fetch: incremental (cursor->now), last_week, "
             "last_month, range. Overrides the product's saved setting.",
    )
    parser.add_argument(
        "--since", default=None,
        help="When --time-mode=range, fetch from this date (YYYY-MM-DD, UTC).",
    )
    parser.add_argument(
        "--until", default=None,
        help="When --time-mode=range, fetch through this date inclusive (YYYY-MM-DD, UTC).",
    )
    parser.add_argument(
        "--source-ids",
        default=None,
        help="Comma-separated source instance ids to include. "
             "Defaults to all sources configured for the product.",
    )
    args = parser.parse_args(argv)

    # Load + activate the product before anything that reads config (storage paths,
    # prompts, schema, sources) is initialized.
    product = load_product(args.product)
    set_current_product(product)
    log.info("product_loaded", product=product.id, display=product.display)

    # Ensure per-product warehouse + state schemas exist (idempotent). Without
    # this, running a brand-new product created via the UI fails on the first
    # start_run() call with "Table runs does not exist". scripts/init_db.py
    # does the same thing for the CLI path.
    storage.ensure_schema()

    try:
        window = compute_effective_window(
            product, time_mode=args.time_mode, since=args.since, until=args.until,
        )
    except ValueError as e:
        print(f"[run] invalid time window: {e}", file=sys.stderr)
        return 2
    log.info("time_window", **window)

    week_id = args.week or current_week_id()
    run_id = args.run_id or f"run_{product.id}_{week_id}_{uuid.uuid4().hex[:8]}"
    versions = {"taxonomy": taxonomy_version(), "vendors": vendors_version(), "code": _code_version()}

    storage.start_run(run_id, week_id, versions)
    runtime_context = {
        "cli_args": {
            "product": args.product,
            "run_id": args.run_id,
            "week": args.week,
            "skip_fetch": args.skip_fetch,
            "skip_llm": args.skip_llm,
            "time_mode": args.time_mode,
            "since": args.since,
            "until": args.until,
            "source_ids": args.source_ids,
        },
        "python_argv": list(sys.argv),
        "python_version": sys.version.split()[0],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    stage_capture.init_run(product.id, run_id, week_id, versions, window, runtime_context)
    durations: dict[str, float] = {}
    results: dict[str, Any] = {}
    errors: list[str] = []
    completeness: dict[str, Any] = {}
    status = "success"

    def _stage(name: str, fn: Callable[[], dict]) -> None:
        _run_stage(name, fn, durations, results)
        try:
            stage_capture.snapshot_stage(
                product.id, run_id, week_id, name,
                results.get(name) or {}, durations.get(name, 0.0),
            )
        except Exception as e:  # pragma: no cover - never let capture fail the run
            log.warning("stage_capture_failed", stage=name, error=str(e))

    # import stages lazily so missing optional deps don't break --skip-* paths
    from pipeline import aggregate, classify, fetch, filter as filter_stage
    from pipeline import group, normalize, relevance, render, score

    selected_source_ids: Optional[list[str]] = None
    if args.source_ids:
        configured = {s.get("id") for s in product.sources}
        requested = [s.strip() for s in args.source_ids.split(",") if s.strip()]
        unknown = [s for s in requested if s not in configured]
        if unknown:
            print(
                f"[run] unknown source ids: {unknown}. Configured: {sorted(configured)}",
                file=sys.stderr,
            )
            return 2
        selected_source_ids = requested
        log.info("source_filter", source_ids=selected_source_ids)

    try:
        if not args.skip_fetch:
            _stage(
                "fetch",
                lambda: fetch.run_fetch(
                    week_id,
                    effective_since=window["since_ts"],
                    effective_until=window["until_ts"],
                    advance_cursor=window["advance_cursor"],
                    source_ids=selected_source_ids,
                ),
            )
            completeness.update(results["fetch"].get("completeness", {}))
            errors.extend(results["fetch"].get("errors", []))

        _stage("normalize", lambda: normalize.run_normalize(week_id))
        _stage("filter", lambda: filter_stage.run_filter(week_id))

        llm_ok = not args.skip_llm and _llm_reachable()
        if llm_ok:
            _stage("relevance", lambda: relevance.run_relevance(week_id))
            _stage("classify", lambda: classify.run_classify(week_id))
            completeness["conditional_violations"] = (
                results["classify"].get("counters", {}).get("conditional_violations", 0)
            )
            _stage("score", lambda: score.run_score(week_id))
            _stage("group", lambda: group.run_group(week_id))
            _stage("aggregate", lambda: aggregate.run_aggregate(week_id))
            _stage("render", lambda: render.run_render(week_id))
        else:
            if args.skip_llm:
                msg = "LLM stages skipped (--skip-llm)"
            else:
                msg = (
                    "LLM stages skipped: configured endpoint failed health check. "
                    "Verify the endpoint URL on the product's LLM routing page and "
                    "that the API key (if hosted) is set on /connections."
                )
            log.warning("llm_skipped", reason=msg)
            errors.append(msg)
            status = "partial"
    except Exception as e:  # pragma: no cover - top-level safety net
        log.error("pipeline_failed", error=str(e))
        errors.append(f"fatal: {e}")
        status = "failed"

    counters = {k: v.get("counters", v) for k, v in results.items()}
    storage.finish_run(run_id, status, durations, counters, completeness, errors)
    _write_run_log(run_id, week_id, status, durations, counters, completeness, errors)

    print(f"\n[run] {run_id} status={status}")
    for stage, secs in durations.items():
        print(f"  {stage:<10} {secs:>6.2f}s")
    if status == "success":
        out = results.get("render", {}).get("out_dir", "")
        print(f"\n  report: {out}\\index.html")
    if errors:
        print("\n  notes:")
        for e in errors:
            print(f"   - {e}")
    return 0 if status != "failed" else 1


def _llm_reachable() -> bool:
    try:
        from pipeline.llm import LLMClient

        return LLMClient("relevance").health_check()
    except Exception as e:
        log.warning("llm_client_init_failed", error=str(e))
        return False


def _write_run_log(run_id, week_id, status, durations, counters, completeness, errors) -> None:
    # Per-product run-log directory: data/<product_id>/run_logs/. Falls back to
    # the legacy `run_logs_root` if no product is loaded.
    try:
        from pipeline.config import current_product
        product_id = current_product().id
        root = resolve_path(app_config()["paths"]["data_root"]) / product_id / "run_logs"
    except Exception:
        root = resolve_path(app_config()["paths"]["run_logs_root"])
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id, "week_id": week_id, "status": status,
        "stage_durations": durations, "counters": counters,
        "completeness": completeness, "errors": errors,
    }
    (root / f"{run_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
