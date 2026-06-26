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
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import structlog
from dotenv import load_dotenv

from pipeline import storage
from pipeline.config import resolve_path, app_config, taxonomy_version, vendors_version
from pipeline.util import current_week_id

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
    parser.add_argument("--week", default=None, help="ISO week id, e.g. 2026-W22")
    parser.add_argument("--skip-fetch", action="store_true", help="re-run from existing raw/warehouse")
    parser.add_argument("--skip-llm", action="store_true", help="skip relevance+classify stages")
    args = parser.parse_args(argv)

    week_id = args.week or current_week_id()
    run_id = f"run_{week_id}_{uuid.uuid4().hex[:8]}"
    versions = {"taxonomy": taxonomy_version(), "vendors": vendors_version(), "code": _code_version()}

    storage.start_run(run_id, week_id, versions)
    durations: dict[str, float] = {}
    results: dict[str, Any] = {}
    errors: list[str] = []
    completeness: dict[str, Any] = {}
    status = "success"

    # import stages lazily so missing optional deps don't break --skip-* paths
    from pipeline import aggregate, classify, fetch, filter as filter_stage
    from pipeline import group, normalize, relevance, render, score

    try:
        if not args.skip_fetch:
            _run_stage("fetch", lambda: fetch.run_fetch(week_id), durations, results)
            completeness.update(results["fetch"].get("completeness", {}))
            errors.extend(results["fetch"].get("errors", []))

        _run_stage("normalize", lambda: normalize.run_normalize(week_id), durations, results)
        _run_stage("filter", lambda: filter_stage.run_filter(week_id), durations, results)

        llm_ok = not args.skip_llm and _llm_reachable()
        if llm_ok:
            _run_stage("relevance", lambda: relevance.run_relevance(week_id), durations, results)
            _run_stage("classify", lambda: classify.run_classify(week_id), durations, results)
            completeness["conditional_violations"] = (
                results["classify"].get("counters", {}).get("conditional_violations", 0)
            )
            _run_stage("score", lambda: score.run_score(week_id), durations, results)
            _run_stage("group", lambda: group.run_group(week_id), durations, results)
            _run_stage("aggregate", lambda: aggregate.run_aggregate(week_id), durations, results)
            _run_stage("render", lambda: render.run_render(week_id), durations, results)
        else:
            msg = "LLM stages skipped (Foundry Local unreachable or --skip-llm)"
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
