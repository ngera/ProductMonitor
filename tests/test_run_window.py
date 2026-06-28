"""Tests for the time-range window computation in pipeline.run."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pipeline.run import compute_effective_window


@dataclass
class _FakeProduct:
    time_range: dict


def test_default_is_incremental():
    p = _FakeProduct(time_range={"mode": "incremental"})
    w = compute_effective_window(p)
    assert w["mode"] == "incremental"
    assert w["since_ts"] is None
    assert w["until_ts"] is None
    assert w["advance_cursor"] is True


def test_no_time_range_block_still_defaults_to_incremental():
    p = _FakeProduct(time_range={})
    w = compute_effective_window(p)
    assert w["mode"] == "incremental"


def test_last_week_sets_window():
    p = _FakeProduct(time_range={"mode": "incremental"})
    w = compute_effective_window(p, time_mode="last_week")
    assert w["mode"] == "last_week"
    assert w["since_ts"] is not None and w["until_ts"] is not None
    assert w["until_ts"] - w["since_ts"] == 7 * 86400
    assert w["advance_cursor"] is True


def test_last_month_sets_window():
    p = _FakeProduct(time_range={"mode": "incremental"})
    w = compute_effective_window(p, time_mode="last_month")
    assert w["mode"] == "last_month"
    assert w["until_ts"] - w["since_ts"] == 30 * 86400
    assert w["advance_cursor"] is True


def test_range_with_explicit_dates():
    p = _FakeProduct(time_range={"mode": "incremental"})
    w = compute_effective_window(p, time_mode="range",
                                 since="2026-05-01", until="2026-05-15")
    assert w["mode"] == "range"
    assert w["since_ts"] < w["until_ts"]
    # Range is treated as historical backfill — must not poison cursors.
    assert w["advance_cursor"] is False


def test_range_uses_persisted_dates_when_cli_omits():
    p = _FakeProduct(time_range={
        "mode": "range", "range_from": "2026-05-01", "range_to": "2026-05-15",
    })
    w = compute_effective_window(p)
    assert w["mode"] == "range"
    assert w["since_ts"] is not None and w["until_ts"] is not None


def test_range_without_dates_raises():
    p = _FakeProduct(time_range={"mode": "range"})
    with pytest.raises(ValueError) as e:
        compute_effective_window(p)
    assert "needs both" in str(e.value)


def test_range_inverted_dates_raises():
    p = _FakeProduct(time_range={"mode": "incremental"})
    with pytest.raises(ValueError) as e:
        compute_effective_window(p, time_mode="range",
                                 since="2026-06-15", until="2026-06-01")
    assert "after" in str(e.value)


def test_unknown_mode_raises():
    p = _FakeProduct(time_range={"mode": "incremental"})
    with pytest.raises(ValueError) as e:
        compute_effective_window(p, time_mode="next_century")
    assert "unknown time mode" in str(e.value)


def test_cli_overrides_saved_mode():
    # Saved mode is range, CLI says last_week → CLI wins.
    p = _FakeProduct(time_range={
        "mode": "range", "range_from": "2026-05-01", "range_to": "2026-05-15",
    })
    w = compute_effective_window(p, time_mode="last_week")
    assert w["mode"] == "last_week"
    assert w["advance_cursor"] is True
