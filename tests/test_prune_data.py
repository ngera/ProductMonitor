"""Tests for scripts/prune_data.py (#12c retention pruning).

Verifies:
- Duration parser accepts d/w/m/y suffixes and rejects garbage.
- Raw JSONL from old ISO weeks is selected; recent weeks are not.
- Run-log sidecars are grouped by stem and deleted together.
- `.running` markers are NEVER touched (scheduler's job).
- Dry-run (default) leaves the filesystem untouched.
"""

from __future__ import annotations

import importlib.util
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "prune_data.py"
_spec = importlib.util.spec_from_file_location("_prune_data", _SCRIPT)
prune_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune_data)  # type: ignore[union-attr]


class TestDurationParser:
    @pytest.mark.parametrize("spec,days", [
        ("30d", 30), ("4w", 28), ("6m", 180), ("1y", 365), ("52W", 364),
    ])
    def test_valid(self, spec: str, days: int) -> None:
        assert prune_data._parse_duration(spec) == timedelta(days=days)

    @pytest.mark.parametrize("spec", ["", "30", "d30", "5x", "abc"])
    def test_invalid(self, spec: str) -> None:
        with pytest.raises(Exception):
            prune_data._parse_duration(spec)


class TestWeekIdToDate:
    def test_valid(self) -> None:
        d = prune_data._week_id_to_date("2026-W37")
        assert d is not None
        # ISO week 37 of 2026 starts Monday 2026-09-07.
        assert d.year == 2026 and d.month == 9 and d.day == 7

    @pytest.mark.parametrize("bad", ["2026", "W37", "2026-37", "not-a-week"])
    def test_invalid(self, bad: str) -> None:
        assert prune_data._week_id_to_date(bad) is None


def _make_raw(product_dir: Path, source: str, week_id: str) -> Path:
    d = product_dir / "raw" / source / week_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / "stream.jsonl"
    f.write_text('{"external_id":"a"}\n', encoding="utf-8")
    return f


def _make_run_log(
    product_dir: Path, run_id: str, mtime_offset_days: float,
) -> list[Path]:
    d = product_dir / "run_logs"
    d.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for suffix in (".json", ".out", ".pid"):
        f = d / f"{run_id}{suffix}"
        f.write_text("x", encoding="utf-8")
        ts = time.time() - (mtime_offset_days * 86400)
        os.utime(f, (ts, ts))
        files.append(f)
    return files


def test_prune_raw_deletes_old_weeks(tmp_path: Path) -> None:
    product = tmp_path / "windows"
    now = datetime.now(timezone.utc)
    old_week = (now - timedelta(days=400)).strftime("%G-W%V")
    fresh_week = (now - timedelta(days=3)).strftime("%G-W%V")
    old_file = _make_raw(product, "rss", old_week)
    fresh_file = _make_raw(product, "rss", fresh_week)

    files_removed, _ = prune_data._prune_raw_jsonl(
        product, timedelta(days=365), now, commit=True,
    )
    assert files_removed == 1
    assert not old_file.exists()
    assert fresh_file.exists()


def test_prune_raw_dry_run_leaves_files(tmp_path: Path) -> None:
    product = tmp_path / "windows"
    now = datetime.now(timezone.utc)
    old_week = (now - timedelta(days=400)).strftime("%G-W%V")
    old_file = _make_raw(product, "rss", old_week)

    files_removed, _ = prune_data._prune_raw_jsonl(
        product, timedelta(days=365), now, commit=False,
    )
    assert files_removed == 1        # counted…
    assert old_file.exists()          # …but not deleted.


def test_prune_run_logs_deletes_full_group(tmp_path: Path) -> None:
    product = tmp_path / "windows"
    old_files = _make_run_log(product, "ui-old-run", mtime_offset_days=60)
    fresh_files = _make_run_log(product, "ui-fresh-run", mtime_offset_days=1)

    files_removed, _ = prune_data._prune_run_logs(
        product, timedelta(days=30), datetime.now(timezone.utc), commit=True,
    )
    assert files_removed == 3
    for f in old_files:
        assert not f.exists()
    for f in fresh_files:
        assert f.exists()


def test_prune_run_logs_leaves_running_markers(tmp_path: Path) -> None:
    product = tmp_path / "windows"
    d = product / "run_logs"
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "ui-old-run.running"
    marker.write_text("x", encoding="utf-8")
    # Backdate the marker so it WOULD be pruned by mtime.
    old_ts = time.time() - (60 * 86400)
    os.utime(marker, (old_ts, old_ts))

    prune_data._prune_run_logs(
        product, timedelta(days=30), datetime.now(timezone.utc), commit=True,
    )
    assert marker.exists(), (
        ".running markers must be left to the scheduler's stale-marker "
        "detector; pruning them would create a race with in-flight runs"
    )
