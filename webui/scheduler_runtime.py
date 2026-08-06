"""Scheduler daemon — background thread that fires per-product runs on cadence.

Started once at webui startup (see webui/app.py `startup_event`). Wakes every
`_TICK_SECONDS` (default 60s) and, for each product whose
`products/<id>/schedule.yaml` is enabled:

  1. Loads the schedule via `pipeline.scheduler.load`.
  2. Calls `pipeline.scheduler.should_fire`. If false → next product.
  3. Checks for an in-flight `.running` marker in the product's run_logs.
     - If present → `scheduler.write_skipped_placeholder(...)` documents the
       skip in the runs list so the operator can see WHY nothing ran.
       Marks the schedule as fired so we don't spin on the same tick.
     - If absent → spawns the same `python -m pipeline.run` subprocess the
       "Trigger run" button uses, with `--time-mode` mapped from the
       schedule's `time_window`. Marks the schedule as fired.

Feature-flag gated: `scheduler_enabled` (per-product override supported via
`products/<pid>/features.yaml`). When the global flag is off but a specific
product has it on, that product's schedule still runs.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_TICK_SECONDS = 60          # daemon wake interval
_MIN_SLEEP_ON_ERROR = 5     # backoff after an unexpected exception


# ---------------------------------------------------------------------------
# Public entry point — called from webui/app.py startup
# ---------------------------------------------------------------------------


def start(*, tick_seconds: int = _TICK_SECONDS) -> threading.Thread:
    """Launch the scheduler daemon. Returns the thread handle. Idempotent-ish
    — calling twice just spawns two threads, which is harmless (they both
    read schedule.yaml before firing) but wasteful; the app.py caller
    guards against double-start via a module-level flag."""
    t = threading.Thread(
        target=_loop, args=(tick_seconds,),
        daemon=True, name="feedback-monitor-scheduler",
    )
    t.start()
    log.info("scheduler.started tick_seconds=%s", tick_seconds)
    return t


# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------


def _loop(tick_seconds: int) -> None:
    while True:
        try:
            _tick()
        except Exception as e:  # never let the daemon die
            log.exception("scheduler.tick_crashed error=%s", e)
            time.sleep(_MIN_SLEEP_ON_ERROR)
            continue
        time.sleep(tick_seconds)


def _tick(now: Optional[datetime] = None) -> None:
    """One iteration: enumerate products, fire what's due."""
    from pipeline import features, scheduler
    from pipeline.product import PRODUCTS_DIR, available_products

    if not PRODUCTS_DIR.exists():
        return

    now = now or datetime.now(timezone.utc)

    for pid in available_products():
        # Product-level override respects the same precedence as every other
        # feature (per-product features.yaml → global features.yaml → False).
        if not features.enabled("scheduler_enabled", pid):
            continue
        try:
            sched = scheduler.load(pid)
        except Exception as e:
            log.warning("scheduler.load_failed product=%s error=%s", pid, e)
            continue
        if not scheduler.should_fire(sched, now=now):
            continue
        try:
            _fire_or_skip(pid, sched, now)
        except Exception as e:
            log.exception("scheduler.fire_failed product=%s error=%s", pid, e)


def _fire_or_skip(product_id: str, sched, now: datetime) -> None:
    """Fire the pipeline (or document a skip if a run is already in flight)
    for one due product. Marks the schedule fired either way so a subsequent
    tick doesn't fire again immediately."""
    from pipeline import scheduler
    logs_dir = _run_logs_dir(product_id)
    in_flight = scheduler.in_flight_run_id(logs_dir)
    if in_flight:
        scheduler.write_skipped_placeholder(
            logs_dir, product_id,
            reason=f"another run is still in progress ({in_flight})",
            now=now,
        )
        log.info(
            "scheduler.skipped product=%s reason=in_flight running_id=%s",
            product_id, in_flight,
        )
        scheduler.mark_fired(product_id, now)
        return

    marker_id = _spawn_pipeline_run(product_id, sched, logs_dir, now)
    log.info(
        "scheduler.fired product=%s marker_id=%s cadence=%s time_window=%s",
        product_id, marker_id, sched.cadence, sched.time_window,
    )
    scheduler.mark_fired(product_id, now)


# ---------------------------------------------------------------------------
# Subprocess launcher — mirrors webui.app.runs_create so scheduled runs look
# identical to UI-triggered runs in the runs list / stage-capture files.
# ---------------------------------------------------------------------------


def _spawn_pipeline_run(product_id: str, sched, logs_dir: Path,
                        now: datetime) -> str:
    from pipeline.scheduler import TIME_WINDOW_TO_TIME_MODE

    marker_id = "sched-" + now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    logs_dir.mkdir(parents=True, exist_ok=True)
    marker = logs_dir / f"{marker_id}.running"
    marker.write_text(
        f"started by scheduler at {now.isoformat()}\n", encoding="utf-8",
    )

    out_path = logs_dir / f"{marker_id}.out"
    time_mode = TIME_WINDOW_TO_TIME_MODE.get(sched.time_window, "incremental")

    cmd = [
        _project_python(), "-m", "pipeline.run",
        "--product", product_id,
        "--run-id", marker_id,
        "--time-mode", time_mode,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=open(out_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                if sys.platform == "win32" else 0
            ),
        )
    except Exception:
        marker.unlink(missing_ok=True)
        raise

    (logs_dir / f"{marker_id}.pid").write_text(str(proc.pid), encoding="utf-8")
    return marker_id


def _project_python() -> str:
    """Prefer the project venv's python (matches webui.app._project_python).
    Falls back to sys.executable so container / global installs still work."""
    root = Path(__file__).resolve().parent.parent
    for candidate in (
        root / ".venv" / "Scripts" / "python.exe",  # Windows venv
        root / ".venv" / "bin" / "python",           # POSIX venv
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _run_logs_dir(product_id: str) -> Path:
    from pipeline.config import app_config, resolve_path
    return (
        resolve_path(app_config()["paths"]["data_root"])
        / product_id / "run_logs"
    )
