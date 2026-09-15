"""Regression tests for stale `.running` marker cleanup.

The scheduler used to trust every `.running` marker unconditionally, so a
crashed subprocess (OOM, `docker compose down` mid-run, power loss) left a
marker that permanently blocked the schedule for that product — no error,
no log, weekly digest just stopped running.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import scheduler


def _write_marker(logs_dir: Path, marker_id: str, *, pid: int | None) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    marker = logs_dir / f"{marker_id}.running"
    marker.write_text("test marker\n", encoding="utf-8")
    if pid is not None:
        (logs_dir / f"{marker_id}.pid").write_text(str(pid), encoding="utf-8")
    return marker


def test_marker_with_live_pid_is_in_flight(tmp_path: Path) -> None:
    _write_marker(tmp_path, "run-1", pid=os.getpid())
    assert scheduler.in_flight_run_id(tmp_path) == "run-1"
    # Not cleaned up.
    assert (tmp_path / "run-1.running").exists()


def test_marker_with_dead_pid_is_cleaned(tmp_path: Path) -> None:
    # PID 2**31 - 1 is not going to be live on any reasonable system.
    _write_marker(tmp_path, "run-crashed", pid=2**31 - 1)
    assert scheduler.in_flight_run_id(tmp_path) is None
    assert not (tmp_path / "run-crashed.running").exists()
    assert not (tmp_path / "run-crashed.pid").exists()


def test_ancient_marker_without_pid_is_cleaned(tmp_path: Path) -> None:
    marker = _write_marker(tmp_path, "run-legacy", pid=None)
    # Backdate mtime past the staleness threshold.
    old = time.time() - (scheduler._STALE_MARKER_AFTER_SECONDS + 60)
    os.utime(marker, (old, old))
    assert scheduler.in_flight_run_id(tmp_path) is None
    assert not marker.exists()


def test_fresh_marker_without_pid_is_still_in_flight(tmp_path: Path) -> None:
    # A marker under the age threshold with no `.pid` sidecar (race between
    # write and pid write) should NOT be declared stale.
    _write_marker(tmp_path, "run-young", pid=None)
    assert scheduler.in_flight_run_id(tmp_path) == "run-young"


def test_terminal_json_takes_precedence(tmp_path: Path) -> None:
    # If the run finished (`.json` exists), the marker is not in-flight
    # regardless of pid/mtime.
    _write_marker(tmp_path, "run-done", pid=os.getpid())
    (tmp_path / "run-done.json").write_text("{}", encoding="utf-8")
    assert scheduler.in_flight_run_id(tmp_path) is None
