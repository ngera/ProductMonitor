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
from pipeline.config import resolve_path, app_config, set_current_product, taxonomy_version
from pipeline.product import DEFAULT_PRODUCT, available_products, load_product
from pipeline.util import current_week_id


_TIME_MODES = ("incremental", "last_week", "last_month", "last_quarter", "range")


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

    if mode == "last_quarter":
        return {"mode": mode, "since_ts": now - 90 * 86400, "until_ts": now,
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
    # Cross-platform preflight (see pipeline/preflight.py). Idempotent, so
    # invoking via cli.py or directly via `python -m pipeline.run` both
    # get one check apiece with no duplicated output.
    from pipeline import preflight
    preflight.check()

    load_dotenv()
    parser = argparse.ArgumentParser(description="ProductMonitor pipeline")
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
    parser.add_argument(
        "--open-browser", dest="open_browser", action="store_true",
        default=None,
        help="Open the rendered report in the default browser when the run "
             "succeeds. Default: on for terminal runs (stdout is a tty), off "
             "when stdout is piped/redirected. Use --no-open-browser to force off.",
    )
    parser.add_argument(
        "--no-open-browser", dest="open_browser", action="store_false",
        help="Suppress the auto-open browser behavior (useful in CI).",
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
    versions = {"taxonomy": taxonomy_version(), "code": _code_version()}

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

    # POST_V1_PLAN §4.11 — set the top-level token attribution context for
    # the whole run. Individual stages push a stage-specific context on
    # top so LLM calls get run_id + stage + product_id.
    from pipeline.token_usage import TokenContext, set_context as _set_token_context

    def _stage(name: str, fn: Callable[[], dict]) -> None:
        with _set_token_context(TokenContext(stage=name)):
            _run_stage(name, fn, durations, results)
        try:
            stage_capture.snapshot_stage(
                product.id, run_id, week_id, name,
                results.get(name) or {}, durations.get(name, 0.0),
            )
        except Exception as e:  # pragma: no cover - never let capture fail the run
            log.warning("stage_capture_failed", stage=name, error=str(e))

    # import stages lazily so missing optional deps don't break --skip-* paths
    from pipeline import aggregate, classify, digest, eval as eval_stage, fetch, filter as filter_stage
    from pipeline import features as _feat, group, normalize, persistent_issue, relevance, render, score

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

    # POST_V1_PLAN §4.11 — wrap the whole run in the top-level token
    # attribution context. Every LLM call under this scope inherits run_id
    # and product_id automatically.
    with _set_token_context(TokenContext(run_id=run_id, product_id=product.id)):
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
            # Pass the same source_ids the fetch stage used so items from
            # UN-selected sources (fetched by prior runs but still in the
            # warehouse for this week) get dropped as `excluded_by_source`.
            # Without this, downstream stages would still process them and
            # they'd appear in the report.
            _stage(
                "filter",
                lambda: filter_stage.run_filter(
                    week_id, source_ids=selected_source_ids,
                ),
            )

            # Compute the skip reason once so it can flow into `errors` with
            # a specific diagnostic (unconfigured / scaffold-default / probe
            # failed) instead of the historic one-size-fits-all message.
            skip_reason = _llm_skip_reason() if not args.skip_llm else None
            llm_ok = skip_reason is None and not args.skip_llm
            if llm_ok:
                _stage("relevance", lambda: relevance.run_relevance(week_id))
                _stage("classify", lambda: classify.run_classify(week_id))
                completeness["conditional_violations"] = (
                    results["classify"].get("counters", {}).get("conditional_violations", 0)
                )
                _stage("score", lambda: score.run_score(week_id))
                _stage("group", lambda: group.run_group(week_id))
                # Persistent-issue clustering (ADR 0016). Cross-week identity for
                # week_groups per section — powers digest v2's cross-run counts.
                # Loads sentence-transformers lazily so this cost is only paid
                # when the flag is on.
                if _feat.enabled("digest_v2_enabled", product.id):
                    _stage("persistent_issue", lambda: persistent_issue.run(week_id))
                _stage("aggregate", lambda: aggregate.run_aggregate(week_id))
                # §4.10 — score classify against the golden set BEFORE render
                # so the optional acceptance gate (D9) can veto reporting.
                # No-op unless `features.evals_enabled` is on for this product.
                _stage("eval", lambda: eval_stage.run_eval(run_id, week_id))
                _stage("render", lambda: render.run_render(week_id, run_id=run_id))
                # Digest v2 — new report artifact behind the `digest_v2_enabled`
                # flag. Slice 1 scaffold no-ops; real render lands in Slice 3
                # after the persistent-issue stage (Slice 2) is in place.
                if _feat.enabled("digest_v2_enabled", product.id):
                    _stage("digest", lambda: digest.build(run_id, week_id=week_id))
            else:
                if args.skip_llm:
                    msg = "LLM stages skipped (--skip-llm)"
                else:
                    # skip_reason was computed above and diagnoses the
                    # specific failure mode (unconfigured / scaffold /
                    # probe failed) so the operator can act on it.
                    msg = skip_reason or (
                        "LLM stages skipped: reason unknown "
                        "(please file an issue with your llm_routing.yaml)."
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
    report_path: Optional[Path] = None
    if status == "success":
        out = results.get("render", {}).get("out_dir", "")
        if out:
            report_path = Path(out) / "index.html"
            # Plain ASCII marker — Windows terminals default to cp1252 and
            # crash on non-latin1 glyphs when Python's stdout encoding isn't
            # forced to utf-8.
            print(f"\n  [OK] report ready: {report_path}")
    if errors:
        print("\n  notes:")
        for e in errors:
            print(f"   - {e}")

    # Auto-open the report on interactive runs. Default matches the "opened in
    # your browser" one-liner promised in first_run_solution.md §3.3 for demo,
    # and applies to every ordinary run too so users never have to hunt for
    # the file path. Off when stdout is piped so CI logs stay quiet.
    if report_path is not None:
        want_open = args.open_browser
        if want_open is None:
            want_open = sys.stdout.isatty()
        if want_open:
            _open_in_browser(report_path)
    return 0 if status != "failed" else 1


def _open_in_browser(report_path: Path) -> None:
    """Best-effort: never let a browser hiccup fail the run."""
    try:
        import webbrowser
        webbrowser.open(report_path.resolve().as_uri())
    except Exception as e:
        log.warning("browser_open_failed", path=str(report_path), error=str(e))


def _llm_skip_reason() -> Optional[str]:
    """Return a diagnostic string for why LLM stages should be skipped, or
    None when the LLM is reachable and stages should proceed.

    Distinguishes three cases so the operator sees an actionable message:
      1. "Not configured" — endpoint is empty (wizard's Skip choice, or
         the user cleared their llm_routing.yaml on purpose).
      2. "Scaffold default detected" — endpoint still points at the
         Foundry Local default from `scaffold_product`, which most
         installs don't have running.
      3. "Health check failed" — endpoint is set but unreachable /
         key wrong / model unknown. Detail from the exception is logged.
    """
    from pipeline.config import current_product
    try:
        cfg = (current_product().llm_routing or {}).get("relevance") or {}
    except Exception:
        cfg = {}
    endpoint = (cfg.get("endpoint") or "").strip()
    if not endpoint:
        return (
            "LLM stages skipped: no LLM endpoint configured. Set one via "
            "the wizard's LLM chooser, or edit "
            f"products/<id>/llm_routing.yaml directly."
        )
    if endpoint.rstrip("/") == "http://localhost:5273/v1":
        # Scaffold default — Foundry Local. Users who don't run FL see
        # a scary "connection refused" without knowing why.
        return (
            "LLM stages skipped: llm_routing.yaml still points at the "
            "scaffold's Foundry Local placeholder "
            "(http://localhost:5273/v1). Pick a real LLM on the product's "
            "LLM routing page, or start Foundry Local if that was intended."
        )
    try:
        from pipeline.llm import LLMClient
        if LLMClient("relevance").health_check():
            return None
    except Exception as e:
        log.warning("llm_client_init_failed", error=str(e))
    return (
        "LLM stages skipped: configured endpoint failed health check. "
        "Verify the endpoint URL on the product's LLM routing page and "
        "that the API key (if hosted) is set on /connections."
    )


def _llm_reachable() -> bool:
    """Back-compat shim. New code should call `_llm_skip_reason()` for
    the diagnostic message; this returns just the boolean."""
    return _llm_skip_reason() is None


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
