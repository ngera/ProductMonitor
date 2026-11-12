"""Runs: list, start, cancel, resume, detail, stages, review, schedule.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pathlib import Path
from pipeline.snippets import NEGATIVE, POSITIVE, Snippet, save_snippet, slugify
from typing import Optional

from webui import core

router = APIRouter()


# --- Snippet from run review (POST_V1_PLAN §4.4-C) --------------------------


@router.post("/products/{product_id}/runs/{run_id}/review/to-snippet")
async def review_item_to_snippet(product_id: str, run_id: str, request: Request):
    """Post a single item from the run review page as a new snippet.

    Feature-flagged: features.snippet_from_review_enabled must be on.
    Uses the item's title/body/url as the snippet body; polarity is
    supplied by the form.
    """
    from pipeline import features as _features
    if not _features.enabled("snippet_from_review_enabled", product_id):
        raise HTTPException(status_code=403, detail="snippet_from_review is disabled for this product")

    product = core._product_or_404(product_id)
    form = await request.form()
    polarity = (form.get("polarity") or "").strip()
    if polarity not in (POSITIVE, NEGATIVE):
        raise HTTPException(status_code=400, detail=f"invalid polarity {polarity!r}")

    item_id = (form.get("item_id") or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id is required")

    # Read the item from the run snapshot the review page uses.
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")
    items = _read_review_items(snap)
    item = next((i for i in items if i.get("id") == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id!r} not in this run's review snapshot")

    snippet = Snippet(
        id=slugify((item.get("title") or item.get("body") or "review-snippet")[:60]),
        polarity=polarity,
        source_url=item.get("url") or None,
        title=item.get("title") or None,
        body=item.get("body") or "",
        labels={
            "is_topic_relevant": (polarity == POSITIVE),
            "areas": [item["primary_area"]] if item.get("primary_area") else [],
            "summary": item.get("summary") or "",
        },
        holdout_eval=False,
        notes=f"Added from run {run_id} review page.",
        created_at=datetime.now(timezone.utc),
    )
    existing_ids = {s.id for s in product.snippets}
    if snippet.id in existing_ids:
        base, n = snippet.id, 2
        while f"{base}-{n}" in existing_ids:
            n += 1
        snippet.id = f"{base}-{n}"

    save_snippet(core.PRODUCTS_DIR / product_id, snippet)
    core.clear_cache()
    return RedirectResponse(
        url=f"/products/{product_id}/runs/{run_id}/review?snippet_added={snippet.id}",
        status_code=303,
    )


def _reports_root_for(product_id: str) -> Path:
    # Shim: delegates to the service module. New code should import
    # from webui.services.runs directly (ADR-0026).
    from webui.services.runs import reports_root_for
    return reports_root_for(product_id)


def _out_looks_crashed(out_text: str) -> bool:
    """Heuristic: the subprocess wrote a Python traceback / fatal error."""
    if not out_text:
        return False
    tail = out_text[-1200:]
    return ("Traceback (most recent call last)" in tail
            or "ModuleNotFoundError" in tail
            or tail.rstrip().endswith("Error"))


def _list_runs(product_id: str) -> list[dict]:
    """Combine completed .json run logs + still-running .running markers
    + orphan crashed runs (have .out but no .json and no .running)."""
    logs_dir = core._run_logs_dir(product_id)
    rows: dict[str, dict] = {}
    if not logs_dir.exists():
        return []

    for jf in logs_dir.glob("*.json"):
        try:
            payload = _json.loads(jf.read_text(encoding="utf-8"))
            rid = payload.get("run_id") or jf.stem
            rows[rid] = {
                "run_id": rid,
                "week_id": payload.get("week_id"),
                "status": payload.get("status") or "unknown",
                "stage_durations": payload.get("stage_durations") or {},
                "counters": payload.get("counters") or {},
                "running": False,
                "crashed": False,
            }
        except Exception:
            continue

    for mk in logs_dir.glob("*.running"):
        rid = mk.stem
        rows.setdefault(rid, {
            "run_id": rid, "week_id": None, "status": "running",
            "stage_durations": {}, "counters": {}, "running": True, "crashed": False,
        })

    # Orphan crashed: .out exists but neither .json nor .running.
    for of in logs_dir.glob("*.out"):
        rid = of.stem
        if rid in rows:
            continue
        try:
            tail = of.read_text(encoding="utf-8", errors="replace")
        except Exception:
            tail = ""
        if _out_looks_crashed(tail):
            rows[rid] = {
                "run_id": rid, "week_id": None, "status": "crashed",
                "stage_durations": {}, "counters": {}, "running": False, "crashed": True,
            }

    return sorted(rows.values(), key=lambda r: r["run_id"], reverse=True)


def _read_run(product_id: str, run_id: str) -> Optional[dict]:
    logs = core._run_logs_dir(product_id)
    jf = logs / f"{run_id}.json"
    if jf.exists():
        try:
            return _json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _run_is_running(product_id: str, run_id: str) -> bool:
    return (core._run_logs_dir(product_id) / f"{run_id}.running").exists()


def _run_stdout(product_id: str, run_id: str) -> str:
    out = core._run_logs_dir(product_id) / f"{run_id}.out"
    return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""


def _report_dir_for_run(product_id: str, run_payload: Optional[dict]) -> Optional[Path]:
    if not run_payload or not run_payload.get("week_id"):
        return None
    candidate = _reports_root_for(product_id) / run_payload["week_id"]
    return candidate if (candidate / "index.html").exists() else None


@router.get("/products/{product_id}/runs", response_class=HTMLResponse)
def runs_index(request: Request, product_id: str,
                notice: Optional[str] = None, error: Optional[str] = None):
    product = core._product_or_404(product_id)
    runs = _list_runs(product_id)
    # Use the plugin registry to resolve display names so the UI shows
    # "Media Coverage Sources" instead of raw plugin ids like "rss".
    try:
        from sources.registry import get_registry
        _reg = get_registry()
    except Exception:
        _reg = None

    def _type_display(t: str) -> str:
        if not t:
            return ""
        if _reg is not None:
            plugin = _reg.get(t)
            if plugin is not None:
                return plugin.manifest.display_name
        return t

    def _plugin_taxonomy(t: str) -> tuple[list[str], str]:
        """Return (content_types, source_category) for a plugin id, with safe
        defaults so unknown/synthetic types still slot into User Feedback →
        Custom Sources."""
        if not t or _reg is None:
            return (["user_feedback"], "custom_source")
        plugin = _reg.get(t)
        if plugin is None:
            return (["user_feedback"], "custom_source")
        cts = list(getattr(plugin.manifest, "content_types", []) or ["user_feedback"])
        sc = getattr(plugin.manifest, "source_category", "custom_source") or "custom_source"
        return (cts, sc)

    # Build a lookup of catalog feed_url → publication display name so we can
    # render Media Coverage rows with recognizable publication names (Wired,
    # The Verge, …) instead of the generic "Media Coverage Sources" plugin
    # label. Cheap — just a dict comprehension.
    try:
        from pipeline import media_sources as _media
        _pub_by_url = {m["feed_url"]: m["name"] for m in _media.load()}
    except Exception:
        _pub_by_url = {}

    source_options = []
    for s in product.sources:
        # Skip sources the user has paused at the source level — they wouldn't
        # run anyway and pollute the trigger UI. Matches the wizard's
        # "only selected/active items" listing.
        if s.get("paused"):
            continue
        t = s.get("type") or ""
        cts, sc = _plugin_taxonomy(t)
        primary_ct = "user_feedback" if "user_feedback" in cts else (
            "media_coverage" if "media_coverage" in cts else "user_feedback"
        )
        streams = s.get("streams") or []
        stream_rows: list[dict] = []
        n_offcatalog = 0
        for st in streams:
            if st.get("paused"):
                continue
            fu = st.get("feed_url") or st.get("url") or ""
            offcat = bool(t == "rss" and fu and fu not in _pub_by_url)
            if offcat:
                n_offcatalog += 1
            label = (
                _pub_by_url.get(fu)
                or st.get("display")
                or st.get("name")
                or fu
                or _type_display(t)
            )
            stream_rows.append({
                "label": label,
                "detail": fu,
                "offcatalog": offcat,
            })
        if not stream_rows:
            # Every stream is paused — treat the whole source as inactive too.
            continue
        source_options.append({
            "id": s.get("id"),
            "type": t,
            "type_display": _type_display(t),
            "n_streams": len(streams),
            "n_streams_active": len(stream_rows),
            "n_offcatalog": n_offcatalog,
            "content_types": cts,
            "source_category": sc,
            "primary_content_type": primary_ct,
            "stream_rows": stream_rows,
        })
    tr = product.time_range or {"mode": "incremental"}
    # POST_V1_PLAN §4.2 — pre-run readiness card.
    from webui.source_health import compute_readiness
    readiness = compute_readiness(product.sources)
    # Scheduler card — always renders (grays out when the flag is off so the
    # user sees why the card is inert).
    from pipeline import features as _features, scheduler as _scheduler
    schedule = _scheduler.load(product_id)
    schedule_enabled_flag = _features.enabled("scheduler_enabled", product_id)
    next_due = None
    if schedule.enabled:
        try:
            next_due = _scheduler.next_due_at(schedule).isoformat(timespec="minutes")
        except Exception:
            next_due = None
    return core.templates.TemplateResponse(
        "runs_list.html",
        {
            "request": request,
            "product": product,
            "runs": runs,
            "source_options": source_options,
            "time_range_summary": core._summarize_time_range(tr),
            "source_readiness": readiness,
            "schedule": schedule,
            "schedule_cadences": _scheduler.CADENCES,
            "schedule_time_windows": _scheduler.TIME_WINDOWS,
            "schedule_flag_on": schedule_enabled_flag,
            "schedule_next_due": next_due,
            "notice": notice,
            "error": error,
        },
    )


# --- Schedule save (Runs and Reports tab) -----------------------------------


@router.post("/products/{product_id}/schedule")
async def schedule_save(product_id: str, request: Request):
    core._product_or_404(product_id)
    from pipeline import scheduler as _scheduler
    form = await request.form()
    sched = _scheduler.Schedule(
        enabled=(form.get("enabled") in ("1", "on", "true")),
        cadence=(form.get("cadence") or "weekly").strip(),
        time_window=(form.get("time_window") or "last_week").strip(),
        hour=int(form.get("hour") or 9),
        minute=int(form.get("minute") or 0),
        # Preserve last_fired_at across saves — otherwise editing the form
        # would silently reset the cadence clock.
        last_fired_at=_scheduler.load(product_id).last_fired_at,
    )
    try:
        _scheduler.save(product_id, sched)
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/runs?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/products/{product_id}/runs?notice=schedule+saved",
        status_code=303,
    )


def _effective_stream_count(product, selected_source_ids: list[str]) -> tuple[int, str]:
    """Count how many streams would actually fetch given the current
    pause state + the user's source-id selection.

    Mirrors the fetch stage's pause precedence (pipeline/fetch.py):
      1. Global connection pause via connections.paused_types()
      2. Per-source `paused` flag on the product's source entry
      3. Per-stream `paused` flag on each stream

    Returns (count, error_message). error_message is a URL-safe hint
    explaining WHY nothing would fetch; only meaningful when count == 0.
    """
    from pipeline import connections
    globally_paused = connections.paused_types()

    allow: set[str] | None = (
        {s for s in selected_source_ids if s} if selected_source_ids else None
    )
    live = 0
    considered = 0
    for src in (product.sources or []):
        sid = src.get("id") or ""
        stype = src.get("type") or ""
        if allow is not None and sid not in allow:
            continue
        considered += 1
        if stype in globally_paused:
            continue
        if bool(src.get("paused")):
            continue
        for stream in (src.get("streams") or []):
            if not bool(stream.get("paused")):
                live += 1

    if live > 0:
        return live, ""
    if considered == 0:
        return 0, "no+sources+configured+for+this+product+—+add+one+on+the+Sources+page"
    if allow is None:
        return 0, "all+configured+sources+are+paused+—+un-pause+at+least+one+on+the+Sources+page"
    return 0, (
        "selected+source(s)+have+all+streams+paused+—+un-pause+on+the+Sources+"
        "page,+or+pick+a+source+that+isn%27t+fully+paused"
    )


@router.post("/products/{product_id}/runs")
async def runs_create(product_id: str, request: Request):
    product = core._product_or_404(product_id)
    form = await request.form()
    skip_fetch = form.get("skip_fetch")
    skip_llm = form.get("skip_llm")
    # Multi-select of source ids; empty list = all sources (default).
    selected_sources = [v for v in form.getlist("source_ids") if v]
    # Per-publication toggles for the rss (media coverage) source. Each row
    # on the runs form submits its feed_url; if any are present we pass them
    # through as the keep-list so the fetch stage skips other rss streams.
    # When at least one publication is checked, the parent rss source id is
    # auto-added to selected_sources — the checkbox for the source itself is
    # implicit under the per-stream UI.
    keep_feed_urls = [u for u in form.getlist("stream_urls") if u]
    if keep_feed_urls:
        # Discover which rss source ids the operator has publications
        # checked under so we make sure their source is in selected_sources.
        rss_ids_from_streams: set[str] = set()
        for src in product.sources:
            if src.get("type") != "rss":
                continue
            for stream in src.get("streams") or []:
                fu = stream.get("feed_url") or stream.get("url") or ""
                if fu in keep_feed_urls:
                    rss_ids_from_streams.add(src.get("id"))
                    break
        for sid in rss_ids_from_streams:
            if sid and sid not in selected_sources:
                selected_sources.append(sid)
    # Optional per-run time-mode override; if not set, the persisted product
    # time_range is used by the orchestrator.
    time_mode_override = (form.get("time_mode_override") or "").strip()

    # Pre-flight: refuse to spawn a run when the effective selection has
    # zero un-paused streams. Without this, a user who picks a source
    # whose streams are ALL paused sees a "success" run with 0 items,
    # which is confusing. The pipeline would happily do that same
    # nothing — this just fails fast with a clear message.
    if not skip_fetch:
        n_live, msg = _effective_stream_count(product, selected_sources)
        if n_live == 0:
            return RedirectResponse(
                url=f"/products/{product_id}/runs?error={msg}",
                status_code=303,
            )

    marker_id = core._spawn_pipeline_run(
        product_id,
        skip_fetch=bool(skip_fetch),
        skip_llm=bool(skip_llm),
        source_ids=selected_sources or None,
        keep_feed_urls=keep_feed_urls or None,
        time_mode=time_mode_override or None,
    )
    return RedirectResponse(url=f"/products/{product_id}/runs/{marker_id}", status_code=303)


def _cancel_pipeline_run(product_id: str, run_id: str) -> tuple[bool, str]:
    """Kill an in-flight UI-spawned pipeline subprocess and clear its marker.

    Returns (ok, message). Safe to call when the run already finished —
    reports that and cleans a stale marker if present.
    """
    logs_dir = core._run_logs_dir(product_id)
    marker = logs_dir / f"{run_id}.running"
    pid_path = logs_dir / f"{run_id}.pid"
    json_path = logs_dir / f"{run_id}.json"

    if json_path.exists():
        marker.unlink(missing_ok=True)
        return False, "run already finished"

    if not marker.exists() and not pid_path.exists():
        return False, "no in-flight run found"

    pid: Optional[int] = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (TypeError, ValueError):
            pid = None

    killed = False
    if pid is not None and pid > 0:
        try:
            if sys.platform == "win32":
                # DETACHED_PROCESS children need a tree kill.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                killed = True
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
                killed = True
        except ProcessLookupError:
            killed = False
        except Exception as e:
            marker.unlink(missing_ok=True)
            return False, f"failed to stop process {pid}: {e}"

    marker.unlink(missing_ok=True)

    # Terminal JSON so the runs list shows cancelled rather than an orphan.
    if not json_path.exists():
        from datetime import datetime, timezone
        week_id, _ = _resume_week_and_sources(product_id, run_id, None)
        try:
            json_path.write_text(
                _json.dumps({
                    "run_id": run_id,
                    "product_id": product_id,
                    "week_id": week_id,
                    "status": "cancelled",
                    "stage_durations": {},
                    "counters": {},
                    "errors": [
                        "Cancelled by operator from the product Summary page."
                    ],
                    "cancelled_at": datetime.now(timezone.utc).isoformat(),
                }, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    if killed:
        return True, f"cancelled run {run_id} (pid {pid})"
    return True, f"cleared in-flight marker for {run_id}"


@router.post("/products/{product_id}/runs/{run_id}/cancel")
def run_cancel(product_id: str, run_id: str, next: str = Form("")):
    """Cancel an in-flight run (Summary banner / runs UI)."""
    core._product_or_404(product_id)
    # Only allow cancelling ids that look like our marker ids — never
    # arbitrary paths.
    if not re.fullmatch(r"[A-Za-z0-9._-]{6,80}", run_id or ""):
        raise HTTPException(status_code=400, detail="invalid run_id")
    ok, msg = _cancel_pipeline_run(product_id, run_id)
    dest = next if (
        isinstance(next, str)
        and next.startswith("/")
        and not next.startswith("//")
    ) else f"/products/{product_id}"
    sep = "&" if "?" in dest else "?"
    flag = "notice" if ok else "error"
    # Keep message short for the query string.
    from urllib.parse import quote
    return RedirectResponse(
        url=f"{dest}{sep}{flag}={quote(msg[:120])}",
        status_code=303,
    )


@router.post("/products/{product_id}/runs/{run_id}/resume")
def run_resume(product_id: str, run_id: str):
    """Spawn a new run from the interrupted stage (ADR-0033)."""
    core._product_or_404(product_id)
    if not re.fullmatch(r"[A-Za-z0-9._-]{6,80}", run_id or ""):
        raise HTTPException(status_code=400, detail="invalid run_id")
    if _run_is_running(product_id, run_id):
        from urllib.parse import quote
        return RedirectResponse(
            url=(
                f"/products/{product_id}/runs/{run_id}"
                f"?error={quote('run is still in flight — cancel it first')}"
            ),
            status_code=303,
        )

    payload = _read_run(product_id, run_id)
    stdout = _run_stdout(product_id, run_id)
    crashed = False
    if payload is None and _out_looks_crashed(stdout):
        crashed = True
    status = (payload or {}).get("status") if payload else ("failed" if crashed else None)
    if status not in ("cancelled", "failed", "partial") and not crashed:
        from urllib.parse import quote
        return RedirectResponse(
            url=(
                f"/products/{product_id}/runs/{run_id}"
                f"?error={quote('resume only applies to cancelled, failed, or crashed runs')}"
            ),
            status_code=303,
        )

    stage_states = _parse_stage_states(
        stdout, run_complete=True, payload=payload,
    )
    from_stage = _infer_resume_stage(stage_states)
    if not from_stage:
        from urllib.parse import quote
        return RedirectResponse(
            url=(
                f"/products/{product_id}/runs/{run_id}"
                f"?error={quote('all stages already completed — nothing to resume')}"
            ),
            status_code=303,
        )

    week_id, source_csv = _resume_week_and_sources(product_id, run_id, payload)
    source_list = [s for s in (source_csv or "").split(",") if s] or None
    marker_id = core._spawn_pipeline_run(
        product_id,
        skip_fetch=(from_stage != "fetch"),
        from_stage=from_stage,
        week_id=week_id,
        source_ids=source_list,
        resumed_from=run_id,
    )
    from urllib.parse import quote
    return RedirectResponse(
        url=(
            f"/products/{product_id}/runs/{marker_id}"
            f"?notice={quote(f'resumed from {from_stage} (prior run {run_id})')}"
        ),
        status_code=303,
    )


_STAGE_LINE_RE = re.compile(r"stage=(\w+)")
_STAGE_SECONDS_RE = re.compile(r"seconds=([\d.]+)")


def _infer_resume_stage(stage_states: list[dict]) -> Optional[str]:
    """Pick the pipeline stage a Resume button should start from.

    Rules (ADR-0033):
      - Mid-stage interrupt (``running`` or ``failed`` after stage_start):
        resume that stage.
      - Otherwise resume the stage after the last ``done`` one.
      - If every stage is done, return None (nothing to resume).
      - If nothing has started, resume from ``fetch``.
    """
    if not stage_states:
        return "fetch"
    interrupted = next(
        (
            s["stage"]
            for s in stage_states
            if s.get("status") in ("running", "failed")
        ),
        None,
    )
    if interrupted:
        return interrupted
    last_done_idx = -1
    for i, s in enumerate(stage_states):
        if s.get("status") == "done":
            last_done_idx = i
    if last_done_idx < 0:
        return stage_states[0]["stage"]
    nxt = last_done_idx + 1
    if nxt >= len(stage_states):
        return None
    return stage_states[nxt]["stage"]


def _resume_week_and_sources(
    product_id: str, run_id: str, payload: Optional[dict],
) -> tuple[Optional[str], Optional[str]]:
    """Recover week_id + source_ids CSV for a Resume spawn.

    Prefer the terminal JSON, then temp_runs manifest / runtime snapshot
    (cancel historically wrote cancelled JSON without week_id).
    """
    week_id: Optional[str] = None
    source_ids: Optional[str] = None
    if payload:
        week_id = (payload.get("week_id") or None) or None
        if isinstance(week_id, str):
            week_id = week_id.strip() or None
    temp = core._temp_run_dir(product_id, run_id)
    if not week_id:
        for rel in ("manifest.json", "config_snapshot/runtime.json"):
            path = temp / rel
            if not path.exists():
                continue
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            week_id = (data.get("week_id") or "").strip() or week_id
            if week_id:
                break
    runtime_path = temp / "config_snapshot" / "runtime.json"
    if runtime_path.exists():
        try:
            runtime = _json.loads(runtime_path.read_text(encoding="utf-8"))
            cli = (runtime.get("runtime_context") or {}).get("cli_args") or {}
            raw = cli.get("source_ids")
            if isinstance(raw, str) and raw.strip():
                source_ids = raw.strip()
            if not week_id:
                week_id = (runtime.get("week_id") or "").strip() or None
        except Exception:
            pass
    return week_id, source_ids


def _parse_stage_states(
    stdout: str,
    run_complete: bool,
    payload: Optional[dict] = None,
    *,
    from_stage: Optional[str] = None,
) -> list[dict]:
    """Scan the pipeline .out for stage_start / stage_done markers and produce
    a per-stage status list, in pipeline order.

    Statuses:
      pending  - not seen yet (only used while the run is still active)
      running  - stage_start seen, stage_done not yet
      done     - stage_done seen (or inherited before --from-stage)
      skipped  - run finished but stage never started (e.g. --skip-llm)
      failed   - fatal error before this stage's stage_done

    Duration is filled in for `done` stages from the `seconds=` field.

    Fallback: when `payload` is present (the run wrote its terminal JSON), we
    also merge in payload.stage_durations. This matters because a UI-triggered
    run's .out is written under the marker id, not the pipeline run-id — so
    viewing the completed run by its pipeline run-id gives us an empty stdout
    and stage_durations is the only source of truth we have.

    ``from_stage`` (ADR-0033): stages before the resume gate are marked
    ``done`` so the flow pills / source grid don't look empty on resume.
    """
    states: dict[str, dict] = {
        s: {"stage": s, "status": "pending", "duration_s": None}
        for s in core._PIPELINE_STAGES
    }
    llm_skipped_flag = False
    fatal_seen = False
    detected_from: Optional[str] = from_stage

    for line in (stdout or "").splitlines():
        if "from_stage" in line and "stage=" in line and detected_from is None:
            m = _STAGE_LINE_RE.search(line)
            if m and m.group(1) in states:
                detected_from = m.group(1)
        elif "stage_start" in line:
            m = _STAGE_LINE_RE.search(line)
            if m and m.group(1) in states:
                states[m.group(1)]["status"] = "running"
        elif "stage_done" in line:
            sm = _STAGE_LINE_RE.search(line)
            if sm and sm.group(1) in states:
                states[sm.group(1)]["status"] = "done"
                dm = _STAGE_SECONDS_RE.search(line)
                if dm:
                    try:
                        states[sm.group(1)]["duration_s"] = float(dm.group(1))
                    except ValueError:
                        pass
        elif "llm_skipped" in line:
            llm_skipped_flag = True
        elif "pipeline_failed" in line:
            fatal_seen = True

    # Resume gate: prior stages already finished on the previous run.
    if detected_from and detected_from in states:
        gate = core._PIPELINE_STAGES.index(detected_from)
        for s in core._PIPELINE_STAGES[:gate]:
            if states[s]["status"] == "pending":
                states[s]["status"] = "done"

    # LLM-skipped explicitly turns the six LLM-gated stages into "skipped".
    if llm_skipped_flag:
        for s in ("relevance", "classify", "score", "group", "aggregate", "render"):
            if states[s]["status"] == "pending":
                states[s]["status"] = "skipped"

    if fatal_seen:
        for s in states.values():
            if s["status"] == "running":
                s["status"] = "failed"

    # Merge in payload.stage_durations: anything the payload knows ran must be
    # "done" even if the stdout parse missed it (e.g. .out written under a
    # different id, log rotated, etc.).
    if payload:
        for stage, secs in (payload.get("stage_durations") or {}).items():
            if stage in states and states[stage]["status"] in ("pending", "running"):
                states[stage]["status"] = "done"
                if states[stage]["duration_s"] is None:
                    try:
                        states[stage]["duration_s"] = float(secs)
                    except (TypeError, ValueError):
                        pass

    # Run has ended (payload written or crash detected). Anything still
    # "pending" means the stage never executed — usually --skip-fetch or the
    # run died so early the log has no stage_start entries.
    if run_complete:
        for s in states.values():
            if s["status"] == "pending":
                s["status"] = "skipped"
            elif s["status"] == "running":
                s["status"] = "failed"

    return [states[s] for s in core._PIPELINE_STAGES]


def _resume_meta(product_id: str, run_id: str) -> Optional[dict]:
    """Load ``.resume.json`` sidecar written when the webui spawns a resume."""
    path = core._run_logs_dir(product_id) / f"{run_id}.resume.json"
    if not path.exists():
        return None
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _ensure_inherited_snapshots(product_id: str, run_id: str) -> None:
    """Lazy-copy prior stage snapshots if resume pre-seed was missed."""
    meta = _resume_meta(product_id, run_id)
    if not meta:
        # Fall back to runtime.json once the pipeline has started.
        rt = core._temp_run_dir(product_id, run_id) / "config_snapshot" / "runtime.json"
        if not rt.exists():
            return
        try:
            runtime = _json.loads(rt.read_text(encoding="utf-8"))
            cli = (runtime.get("runtime_context") or {}).get("cli_args") or {}
            prior = cli.get("resumed_from")
            from_stage = cli.get("from_stage")
            if not prior or not from_stage:
                return
            meta = {"prior_run_id": prior, "from_stage": from_stage}
        except Exception:
            return
    prior = meta.get("prior_run_id")
    from_stage = meta.get("from_stage")
    if not prior or not from_stage or from_stage not in core._PIPELINE_STAGES:
        return
    # Already have early-stage captures?
    captured = _captured_stages(product_id, run_id)
    have = {c["stage"] for c in captured}
    need = list(core._PIPELINE_STAGES[: core._PIPELINE_STAGES.index(from_stage)])
    if need and all(s in have for s in need if s in ("fetch", "normalize", "filter", "relevance")):
        return
    try:
        from pipeline import stage_capture as _sc
        _sc.inherit_prior_stages(product_id, run_id, prior, need)
    except Exception:
        pass


@router.get("/products/{product_id}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, product_id: str, run_id: str,
               notice: Optional[str] = None, error: Optional[str] = None):
    product = core._product_or_404(product_id)
    payload = _read_run(product_id, run_id)
    running = _run_is_running(product_id, run_id) and payload is None
    stdout = _run_stdout(product_id, run_id)
    report_dir = _report_dir_for_run(product_id, payload)

    crashed = False
    # Best-effort marker cleanup.
    marker = core._run_logs_dir(product_id) / f"{run_id}.running"
    if payload is not None:
        # JSON exists => run completed normally.
        marker.unlink(missing_ok=True)
    elif running and _out_looks_crashed(stdout):
        # Subprocess wrote a Traceback and stopped — it's not coming back.
        # Clean up the marker so future visits show it as crashed, not stuck.
        marker.unlink(missing_ok=True)
        crashed = True
        running = False

    stage_states = _parse_stage_states(
        stdout, run_complete=(payload is not None or crashed), payload=payload,
    )
    status = (payload or {}).get("status") if payload else None
    resumable = (not running) and (
        crashed or status in ("cancelled", "failed", "partial")
    )
    resume_stage = _infer_resume_stage(stage_states) if resumable else None
    if resume_stage is None:
        resumable = False
    # ADR-0033: pull prior-run snapshots into this run's temp_runs so the
    # source grid isn't empty when we resumed past fetch.
    _ensure_inherited_snapshots(product_id, run_id)
    captured = _captured_stages(product_id, run_id)
    source_flow = _per_source_counts(product_id, run_id, captured)
    # Fold per-stage totals into stage_states so the pill can show "stage N".
    for s in stage_states:
        s["total"] = source_flow["totals"].get(s["stage"])
    # POST_V1_PLAN §4.2 — post-run per-source health card. Only compute when
    # the run has finished (payload exists) since compute_health reads errors[].
    # Scope to --source-ids when the operator unticked sources on the run form
    # (same filter _per_source_counts already applies) so unticked sources
    # don't show as "ok — N streams fetched cleanly".
    source_health_list = []
    if payload is not None:
        from webui.source_health import compute_health
        allowed_sources = _run_source_id_filter(core._temp_run_dir(product_id, run_id))
        source_health_list = compute_health(
            payload, product.sources, source_ids=allowed_sources,
        )

    # POST_V1_PLAN §4.11 — token usage card. Computed for any run with
    # llm_usage rows; gracefully handles empty table.
    #
    # We ALSO skip while a run is still in flight — the token card is only
    # meaningful after the run completes, and this route is polled every
    # 5s by the auto-refresh. Opening the warehouse on every poll invites
    # DuckDB lock contention with the subprocess (see storage.warehouse
    # retry loop). Skipping while `running` removes the contention entirely
    # for the common case.
    from pipeline import features as _features, token_usage as _tu
    token_totals: dict = {}
    if _features.enabled("token_monitor_enabled", product_id) and not running:
        token_totals = _tu.per_run_totals(product_id, run_id)
        # Add cost estimates per model
        if token_totals.get("total_tokens", 0) > 0:
            token_totals["estimated_cost_usd"] = _tu.estimate_cost_usd(
                model=(payload or {}).get("model", "") or "unknown",
                prompt_tokens=token_totals.get("prompt_tokens", 0),
                completion_tokens=token_totals.get("completion_tokens", 0),
                cached_input_tokens=token_totals.get("cached_input_tokens", 0),
            )
    # POST_V1_PLAN §4.10 — eval scorecard. Only rendered when evals_enabled
    # is on for this product AND a summary exists on disk.
    eval_summary = None
    if _features.enabled("evals_enabled", product_id):
        from pipeline import eval as _eval
        eval_summary = _eval.load_summary(product_id, run_id)

    return core.templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "payload": payload,
            "running": running,
            "crashed": crashed,
            "resumable": resumable,
            "resume_stage": resume_stage,
            "stdout_tail": stdout[-4000:] if stdout else "",
            "report_week": (payload or {}).get("week_id") if report_dir else None,
            "captured_stages": captured,
            "stage_states": stage_states,
            "source_flow": source_flow,
            "source_health": source_health_list,
            "token_totals": token_totals,
            "eval_summary": eval_summary,
            "notice": notice,
            "error": error,
        },
    )


# --- Trace viewer (POST_V1_PLAN §4.16) --------------------------------------
#
# Renders the JSONL span file at
# data/<pid>/temp_runs/<run_id>/trace.jsonl as a waterfall.
# Read-only. Trace file is written by pipeline/tracing.py during the run.


@router.get("/products/{product_id}/runs/{run_id}/trace", response_class=HTMLResponse)
def run_trace_view(request: Request, product_id: str, run_id: str):
    product = core._product_or_404(product_id)
    trace_path = core._temp_run_dir(product_id, run_id) / "trace.jsonl"
    spans: list[dict] = []
    if trace_path.exists():
        try:
            with trace_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        spans.append(_json.loads(line))
                    except Exception:
                        continue
        except Exception:
            spans = []

    # Compute a normalized waterfall — bar left offset + width as %
    if spans:
        # Sort by start_ts; earliest first
        spans.sort(key=lambda s: s.get("start_ts", 0))
        t_min = spans[0].get("start_ts", 0)
        t_max = max(s.get("end_ts", 0) for s in spans)
        total = max(t_max - t_min, 0.001)
        for s in spans:
            left = ((s.get("start_ts", t_min) - t_min) / total) * 100
            width = max(((s.get("end_ts", t_min) - s.get("start_ts", t_min)) / total) * 100, 0.5)
            s["_left_pct"] = round(left, 3)
            s["_width_pct"] = round(width, 3)

    return core.templates.TemplateResponse(
        "run_trace.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "spans": spans,
            "trace_path": str(trace_path),
        },
    )


def _per_stream_labels(product_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """Map stream ``name`` → human label and stream ``name`` → source id.

    Used by the run-detail per-source table so Media Coverage streams show
    as Wired / The Verge / … instead of one aggregated "Media Coverage
    Sources (rss)" row. Labels prefer the media catalog (by feed_url),
    then stream display / subreddit / name — same preference order as
    minifetch's Step 4 progress rows.
    """
    labels: dict[str, str] = {}
    source_ids: dict[str, str] = {}
    try:
        from pipeline.product import load_product
        product = load_product(product_id)
    except Exception:
        return labels, source_ids
    try:
        from pipeline.minifetch import _media_catalog_by_url, _stream_display_label
        catalog = _media_catalog_by_url()
    except Exception:
        catalog = {}
        _stream_display_label = None  # type: ignore[assignment]
    for s in product.sources or []:
        sid = (s.get("id") or s.get("type") or "").strip()
        plugin = (s.get("type") or sid).strip()
        for st in s.get("streams") or []:
            name = (st.get("name") or "").strip()
            if not name:
                continue
            if _stream_display_label is not None:
                labels[name] = _stream_display_label(plugin, st, catalog)
            else:
                labels[name] = (st.get("display") or name)
            if sid:
                source_ids[name] = sid
    return labels, source_ids


def _per_source_row_key(stage: str, row: dict) -> tuple[str, str]:
    """Return ``(row_key, plugin_id)`` for the per-source funnel table.

    Fetch snapshots are one row per raw file (stream); later stages carry
    ``source_display_name`` equal to the stream name. Prefer those over
    the plugin id so multiple RSS publications don't collapse into one
    "Media Coverage Sources" row.
    """
    plugin = (row.get("source") or "unknown").strip() or "unknown"
    if stage == "fetch":
        fname = (row.get("file_name") or "").strip()
        if fname:
            stem = Path(fname).stem
            if stem:
                return stem, plugin
        return plugin, plugin
    disp = (row.get("source_display_name") or "").strip()
    if disp:
        return disp, plugin
    return plugin, plugin


def _per_source_counts(product_id: str, run_id: str, captured: list[dict]) -> dict:
    """Walk each captured stage's .jsonl and compute per-source "still in-flight"
    counts. Returns:

        {
          "sources": [(source_id, display_name), ...],   # union across stages
          "by_stage": {stage: {source_id: kept_count}},  # kept per source
          "totals":   {stage: total_kept},               # summed across sources
        }

    "Kept" for warehouse stages = filter_status in (None, 'passed') AND
    is_relevant in (None, True). For the fetch stage snapshot (which lists
    raw JSONL files, not warehouse rows), kept = sum(line_count) per source.

    Row keys are per *stream* (file stem / source_display_name) when
    available, not merely plugin id — so TechCrunch / Verge / Wired each
    get their own funnel row under an rss product source.
    """
    d = core._temp_run_dir(product_id, run_id)
    by_stage: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    displays: dict[str, str] = {}
    plugin_of: dict[str, str] = {}
    stream_labels, stream_source_ids = _per_stream_labels(product_id)

    for meta in captured:
        stage = meta["stage"]
        jsonl = d / f"{stage}.jsonl"
        if not jsonl.exists():
            continue
        counts: dict[str, int] = {}
        total = 0
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = _json.loads(line)
                    except Exception:
                        continue
                    key, plugin = _per_source_row_key(stage, row)
                    plugin_of.setdefault(key, plugin)
                    displays.setdefault(
                        key,
                        stream_labels.get(key)
                        or row.get("source_display_name")
                        or key,
                    )
                    if stage == "fetch":
                        n = int(row.get("line_count") or 0)
                        counts[key] = counts.get(key, 0) + n
                        total += n
                    else:
                        fs = row.get("filter_status")
                        ir = row.get("is_relevant")
                        kept = fs in (None, "passed") and (ir is None or ir is True)
                        if kept:
                            counts[key] = counts.get(key, 0) + 1
                            total += 1
        except Exception:
            continue
        by_stage[stage] = counts
        totals[stage] = total

    # Stable source order: highest ever-seen count first, ties by name.
    max_by_source: dict[str, int] = {}
    for counts in by_stage.values():
        for src, n in counts.items():
            if n > max_by_source.get(src, 0):
                max_by_source[src] = n

    # Hide sources the user un-ticked on the run form. When --source-ids is
    # passed, both fetch (skips other sources) and filter (drops their
    # in-warehouse items as excluded_by_source) already scope the RUN's
    # data. But `_per_source_counts` reads captured snapshots, and the
    # fetch snapshot lists every raw file on disk for the week — including
    # files written by PRIOR runs. Without this filter, those excluded
    # sources still appeared as rows on the run detail page with counts >
    # 0 at normalize and 0 downstream, making it look like the source
    # filter didn't take effect. Read the definitive filter list from the
    # captured runtime.json instead of trying to reverse-engineer it from
    # snapshot counts.
    #
    # ``allowed`` holds product source *ids* (e.g. ``rss``). Row keys are
    # now stream names (``rss-supabase-2``), so match via stream→source_id
    # / plugin_id, not only exact key equality.
    allowed = _run_source_id_filter(d)
    if allowed is not None:
        def _allowed(key: str) -> bool:
            if key in allowed:
                return True
            if stream_source_ids.get(key) in allowed:
                return True
            if plugin_of.get(key) in allowed:
                return True
            return False
        max_by_source = {s: n for s, n in max_by_source.items() if _allowed(s)}
    sources = sorted(max_by_source.keys(), key=lambda s: (-max_by_source[s], s))

    try:
        from sources.registry import get_registry
        _reg = get_registry()
    except Exception:
        _reg = None

    def _row_display(src_id: str) -> str:
        if src_id in stream_labels:
            return stream_labels[src_id]
        # Plugin-level key (single-stream plugins, or legacy snapshots).
        if _reg is not None:
            plugin = _reg.get(src_id)
            if plugin is not None:
                return plugin.manifest.display_name
        return displays.get(src_id, src_id)

    return {
        "sources": [(s, _row_display(s)) for s in sources],
        "by_stage": by_stage,
        "totals": totals,
    }


def _run_source_id_filter(temp_run_dir: Path) -> "set[str] | None":
    """Return the set of source ids the run was scoped to (from
    config_snapshot/runtime.json), or None if the run wasn't scoped
    (i.e. --source-ids wasn't passed and every configured source ran).

    Reads once per request; used by the run-detail per-source table to
    hide sources that were unticked on the runs form.
    """
    rt = temp_run_dir / "config_snapshot" / "runtime.json"
    if not rt.exists():
        return None
    try:
        blob = _json.loads(rt.read_text(encoding="utf-8"))
        raw = ((blob.get("runtime_context") or {}).get("cli_args") or {}).get("source_ids")
    except Exception:
        return None
    if not raw:
        return None
    ids = {s.strip() for s in str(raw).split(",") if s.strip()}
    return ids or None


def _captured_stages(product_id: str, run_id: str) -> list[dict]:
    """Return [{stage, row_count, duration_s, has_error}, ...] in capture order."""
    d = core._temp_run_dir(product_id, run_id)
    idx_path = d / "stages.json"
    if not idx_path.exists():
        return []
    try:
        idx = _json.loads(idx_path.read_text(encoding="utf-8"))
        stages = idx.get("stages") or []
    except Exception:
        return []
    out: list[dict] = []
    for s in stages:
        meta_path = d / f"{s}.meta.json"
        row_count, duration, err = None, None, False
        if meta_path.exists():
            try:
                m = _json.loads(meta_path.read_text(encoding="utf-8"))
                row_count = m.get("row_count")
                duration = m.get("duration_s")
                err = bool(m.get("capture_error"))
            except Exception:
                pass
        out.append({"stage": s, "row_count": row_count, "duration_s": duration, "has_error": err})
    return out


_STAGE_VIEWS = ("in-flight", "dropped", "all")


def _row_is_in_flight(row: dict) -> bool:
    """Same "kept" definition used by the top-of-page source table:
    filter_status hasn't dropped it AND relevance didn't mark it not-relevant."""
    fs = row.get("filter_status")
    ir = row.get("is_relevant")
    return fs in (None, "passed") and (ir is None or ir is True)


def _read_stage_jsonl(
    path: Path, offset: int, limit: int, view: str = "all",
) -> tuple[list[dict], int, int]:
    """Read a slice of the JSONL, with an optional view filter (in-flight /
    dropped / all).

    Returns (rows_on_this_page, total_matching_view, total_in_file).
    Two counters so the sub-page can say "showing X of Y matching (Z total in warehouse)".

    The JSONL is per-run scale (hundreds of rows) so a full linear scan is fine.
    """
    if not path.exists():
        return [], 0, 0
    matches: list[dict] = []
    total_in_file = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            total_in_file += 1
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                row = {"_parse_error": True, "_raw": line[:200]}
            if view == "in-flight" and not _row_is_in_flight(row):
                continue
            if view == "dropped" and _row_is_in_flight(row):
                continue
            matches.append(row)
    total_matching = len(matches)
    return matches[offset:offset + limit], total_matching, total_in_file


@router.get("/products/{product_id}/runs/{run_id}/stages/{stage}", response_class=HTMLResponse)
def stage_snapshot(
    request: Request,
    product_id: str,
    run_id: str,
    stage: str,
    offset: int = 0,
    limit: int = 50,
    view: str = "in-flight",
):
    product = core._product_or_404(product_id)
    d = core._temp_run_dir(product_id, run_id)
    jsonl_path = d / f"{stage}.jsonl"
    meta_path = d / f"{stage}.meta.json"
    if not jsonl_path.exists():
        raise HTTPException(status_code=404, detail=f"no snapshot for stage {stage!r}")

    # Fetch stage has no filter_status concept — force 'all'.
    if stage == "fetch" or view not in _STAGE_VIEWS:
        view = "all"

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows, total, total_in_file = _read_stage_jsonl(jsonl_path, offset, limit, view=view)

    meta: dict = {}
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    # Union of keys seen across the current page — stable-ish column order:
    # base identity first, then classification, then everything else alphabetical.
    preferred = [
        "id", "source", "week_id", "created_at", "author", "title", "body",
        "filter_status", "is_relevant", "relevance_score",
        "primary_area", "sentiment", "content_types_json", "summary",
        "severity", "score",
    ]
    keys_seen = {k for r in rows for k in r.keys()}
    ordered = [k for k in preferred if k in keys_seen] + sorted(
        k for k in keys_seen if k not in preferred
    )

    # Kept-count so the sub-page header can show both "in warehouse" (rows in
    # this JSONL) and "in-flight" (items where filter_status is passed/null
    # and is_relevant isn't False) — matches the parent run detail table.
    # Single _captured_stages() scan reused for both per-stage kept totals
    # and the stage-nav tabs — previously called twice per request.
    all_stages = _captured_stages(product_id, run_id)
    kept_total = None
    per_stage_kept: dict[str, int] = {}
    try:
        source_flow_kept = _per_source_counts(product_id, run_id, all_stages)
        per_stage_kept = dict(source_flow_kept.get("totals") or {})
        kept_total = per_stage_kept.get(stage)
    except Exception:
        pass
    # Replace each nav row's row_count (which came from the joined warehouse
    # view — same number for every non-fetch stage) with the per-stage kept
    # total so each tab reflects what actually survived that stage.
    for row in all_stages:
        if row["stage"] in per_stage_kept:
            row["row_count"] = per_stage_kept[row["stage"]]

    return core.templates.TemplateResponse(
        "stage_snapshot.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "stage": stage,
            "meta": meta,
            "rows": rows,
            "columns": ordered,
            "total": total,
            "total_in_file": total_in_file,
            "offset": offset,
            "limit": limit,
            "view": view,
            "views_available": _STAGE_VIEWS if stage != "fetch" else ("all",),
            "kept_total": kept_total,
            "all_stages": all_stages,
        },
    )


@router.get("/products/{product_id}/runs/{run_id}/stages/{stage}/download")
def stage_snapshot_download(product_id: str, run_id: str, stage: str):
    core._product_or_404(product_id)
    p = core._temp_run_dir(product_id, run_id) / f"{stage}.jsonl"
    if not p.exists():
        raise HTTPException(status_code=404, detail="no snapshot")
    return FileResponse(
        str(p),
        media_type="application/x-ndjson",
        filename=f"{run_id}-{stage}.jsonl",
    )


@router.post("/products/{product_id}/runs/{run_id}/stages/delete")
def stage_snapshots_delete(product_id: str, run_id: str):
    core._product_or_404(product_id)
    d = core._temp_run_dir(product_id, run_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return RedirectResponse(url=f"/products/{product_id}/runs/{run_id}", status_code=303)


# --- Per-run config snapshot browser ----------------------------------------
#
# stage_capture.snapshot_config copies the config that produced each run into
# temp_runs/<run_id>/config_snapshot/. These routes browse it so you can answer
# "what settings did this run use?" long after the live config has changed.


def _config_snapshot_dir(product_id: str, run_id: str) -> Path:
    return core._temp_run_dir(product_id, run_id) / "config_snapshot"


def _list_config_snapshot_files(product_id: str, run_id: str) -> list[dict]:
    """Return a flat list of files in the snapshot, sorted for stable display.
    Each entry: {relpath, name, group, size_bytes}."""
    root = _config_snapshot_dir(product_id, run_id)
    if not root.exists():
        return []
    entries: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        group = "global" if "/" not in rel else rel.split("/", 1)[0]
        entries.append({
            "relpath": rel,
            "name": p.name,
            "group": group,
            "size_bytes": p.stat().st_size,
        })
    return entries


def _safe_snapshot_file(product_id: str, run_id: str, relpath: str) -> Path:
    """Resolve `relpath` inside the snapshot dir, rejecting anything that
    escapes it (path traversal defense)."""
    root = _config_snapshot_dir(product_id, run_id).resolve()
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"file {relpath!r} not in snapshot")
    return candidate


@router.get("/products/{product_id}/runs/{run_id}/config", response_class=HTMLResponse)
def run_config_snapshot(request: Request, product_id: str, run_id: str):
    product = core._product_or_404(product_id)
    entries = _list_config_snapshot_files(product_id, run_id)
    runtime = None
    runtime_path = _config_snapshot_dir(product_id, run_id) / "runtime.json"
    if runtime_path.exists():
        try:
            runtime = _json.loads(runtime_path.read_text(encoding="utf-8"))
        except Exception:
            runtime = None
    return core.templates.TemplateResponse(
        "run_config.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "entries": entries,
            "runtime": runtime,
            "has_snapshot": bool(entries),
        },
    )


@router.get("/products/{product_id}/runs/{run_id}/config/view", response_class=HTMLResponse)
def run_config_snapshot_view(request: Request, product_id: str, run_id: str, relpath: str):
    product = core._product_or_404(product_id)
    path = _safe_snapshot_file(product_id, run_id, relpath)
    body = path.read_text(encoding="utf-8", errors="replace")
    return core.templates.TemplateResponse(
        "run_config_file.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "relpath": relpath,
            "name": path.name,
            "body": body,
            "size_bytes": path.stat().st_size,
        },
    )


@router.get("/products/{product_id}/runs/{run_id}/config/download")
def run_config_snapshot_download(product_id: str, run_id: str, relpath: str):
    core._product_or_404(product_id)
    path = _safe_snapshot_file(product_id, run_id, relpath)
    return FileResponse(
        str(path),
        media_type="text/yaml" if path.suffix in (".yaml", ".yml") else "application/octet-stream",
        filename=f"{run_id}-{path.name}",
    )


# --- Per-run post review ----------------------------------------------------
#
# Every item in the run + the reason it was kept or dropped. Reads the latest
# stage snapshot (classify.jsonl if it exists; else the last one written) so
# we see the item's final filter_status / is_relevant after every stage that
# has run so far.

_FILTER_STATUS_HELP = {
    "passed":                  "Kept by filter — text/engagement/dedup all OK.",
    "dropped:too_short":       "Body under min_body_chars AND title < 20 chars AND no KB/CVE watchlist match.",
    "dropped:duplicate_url":   "Canonical URL was already seen earlier in the batch.",
    "dropped:duplicate_title": "Title matches an earlier item's simhash within grouping.simhash_hamming_threshold.",
    "dropped:low_engagement":  "Upvotes AND comments both below fetching.default_engagement_threshold (raise to 0 to keep everything).",
    "dropped:deleted_or_empty":"Body was [deleted] / [removed] / empty AND no title.",
    "dropped:not_topic_relevant": "Relevance LLM decided this isn't on-topic (score above filter.relevance_drop_confidence).",
    "dropped:not_relevant":       "Relevance LLM decided this isn't relevant (older path).",
    "classification_failed":   "Classify stage failed for this item (LLM error or schema violation).",
    "dropped:conditional_violation": "Classify stage's structured output violated a conditional schema constraint.",
}


def _outcome_class(filter_status: str | None, is_relevant) -> str:
    """CSS class hint for the row: 'kept' / 'dropped-filter' / 'dropped-relevance' / 'dropped-classify' / 'inflight'."""
    if not filter_status:
        return "inflight"
    if filter_status == "passed":
        if is_relevant is True:
            return "kept"
        if is_relevant is False:
            return "dropped-relevance"
        return "inflight"
    if "not_relevant" in filter_status or "not_topic_relevant" in filter_status:
        return "dropped-relevance"
    if "classification" in filter_status:
        return "dropped-classify"
    return "dropped-filter"


def _outcome_label(filter_status: str | None, is_relevant) -> str:
    if not filter_status:
        return "not filtered yet"
    if filter_status == "passed":
        if is_relevant is True:
            return "KEPT (relevant)"
        if is_relevant is False:
            return "dropped by relevance"
        return "kept by filter (pending relevance)"
    if filter_status.startswith("dropped:"):
        return f"dropped: {filter_status.split(':', 1)[1]}"
    return filter_status


def _pick_review_snapshot(product_id: str, run_id: str) -> Optional[Path]:
    """Pick the most complete snapshot for the review list.

    Order of preference (each is a *superset* of the last in terms of state
    populated per item):
      classify > relevance > filter > normalize
    Then fall back to whichever stage was last captured.
    """
    d = core._temp_run_dir(product_id, run_id)
    for stage in ("classify", "relevance", "filter", "normalize"):
        p = d / f"{stage}.jsonl"
        if p.exists():
            return p
    # last-captured fallback
    idx = d / "stages.json"
    if idx.exists():
        try:
            stages = _json.loads(idx.read_text(encoding="utf-8")).get("stages") or []
            for s in reversed(stages):
                p = d / f"{s}.jsonl"
                if p.exists() and s != "fetch":
                    return p
        except Exception:
            pass
    return None


def _read_review_items(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                continue
            fs = row.get("filter_status")
            ir = row.get("is_relevant")
            row["_outcome_class"] = _outcome_class(fs, ir)
            row["_outcome_label"] = _outcome_label(fs, ir)
            row["_reason_help"] = _FILTER_STATUS_HELP.get(fs or "", "")
            rows.append(row)
    return rows


@router.get("/products/{product_id}/runs/{run_id}/review", response_class=HTMLResponse)
def run_review(
    request: Request,
    product_id: str,
    run_id: str,
    outcome: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
    q: Optional[str] = None,
):
    product = core._product_or_404(product_id)
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")

    items = _read_review_items(snap)

    # Facets before filtering, so dropdowns show the full menu.
    reason_counts: dict[str, int] = {}
    for i in items:
        r = i.get("filter_status") or "(none — pre-filter)"
        reason_counts[r] = reason_counts.get(r, 0) + 1
    facets = {
        "outcomes": sorted({i["_outcome_class"] for i in items}),
        "sources":  sorted({i.get("source") for i in items if i.get("source")}),
        # (label, value, count) — sorted by count desc so common reasons come first
        "reasons":  sorted(
            [(r, r, n) for r, n in reason_counts.items()],
            key=lambda t: (-t[2], t[0]),
        ),
    }
    outcome_counts = {}
    for i in items:
        outcome_counts[i["_outcome_class"]] = outcome_counts.get(i["_outcome_class"], 0) + 1

    # Apply filters.
    filtered = items
    if outcome:
        filtered = [i for i in filtered if i["_outcome_class"] == outcome]
    if source:
        filtered = [i for i in filtered if i.get("source") == source]
    if reason:
        if reason == "(none — pre-filter)":
            filtered = [i for i in filtered if not i.get("filter_status")]
        else:
            filtered = [i for i in filtered if i.get("filter_status") == reason]
    if q:
        needle = q.lower()
        filtered = [i for i in filtered
                    if needle in (i.get("title") or "").lower()
                    or needle in (i.get("body") or "").lower()
                    or needle in (i.get("author") or "").lower()]

    from pipeline import features as _features_ref
    return core.templates.TemplateResponse(
        "run_review.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "snapshot_stage": snap.stem,
            "items": filtered,
            "total_all": len(items),
            "total_shown": len(filtered),
            "facets": facets,
            "outcome_counts": outcome_counts,
            "filters": {"outcome": outcome or "", "source": source or "", "reason": reason or "", "q": q or ""},
            "snippet_from_review_enabled": _features_ref.enabled(
                "snippet_from_review_enabled", product_id,
            ),
        },
    )


@router.get("/products/{product_id}/runs/{run_id}/review/detail", response_class=HTMLResponse)
def run_review_detail(request: Request, product_id: str, run_id: str, item_id: str):
    product = core._product_or_404(product_id)
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")

    item = None
    for row in _read_review_items(snap):
        if row.get("id") == item_id:
            item = row
            break
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id!r} not in this run's snapshot")

    # Walk every stage snapshot to build a per-stage state transition list.
    journey: list[dict] = []
    d = core._temp_run_dir(product_id, run_id)
    idx_path = d / "stages.json"
    stages: list[str] = []
    if idx_path.exists():
        try:
            stages = _json.loads(idx_path.read_text(encoding="utf-8")).get("stages") or []
        except Exception:
            pass
    prev_fs, prev_ir = "<absent>", "<absent>"
    for stage in stages:
        p = d / f"{stage}.jsonl"
        if not p.exists() or stage == "fetch":
            journey.append({"stage": stage, "state": None, "changed": False, "note": "fetch snapshot is per-file" if stage == "fetch" else "no snapshot"})
            continue
        row = None
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("id") == item_id:
                    row = r
                    break
        if row is None:
            journey.append({"stage": stage, "state": None, "changed": False, "note": "not present"})
            continue
        fs = row.get("filter_status")
        ir = row.get("is_relevant")
        changed = (fs != prev_fs) or (ir != prev_ir)
        journey.append({
            "stage": stage,
            "state": {"filter_status": fs, "is_relevant": ir, "relevance_score": row.get("relevance_score")},
            "changed": changed,
        })
        prev_fs, prev_ir = fs, ir

    return core.templates.TemplateResponse(
        "run_review_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "item": item,
            "journey": journey,
            "reason_help": _FILTER_STATUS_HELP.get(item.get("filter_status") or "", ""),
        },
    )
