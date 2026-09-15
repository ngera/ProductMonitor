"""Tests for the concurrent-fetch executor (ADR-0023).

Focuses on the DISPATCHER — the semaphore + isolation behavior. The
stream-worker body is exercised by the existing per-source tests
running under the serial code path; this test suite proves the
dispatch shape is correct so flipping the flag doesn't change the
per-stream semantics.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

pytest.importorskip("structlog")

from pipeline.fetch import _StreamResult, _dispatch, _host_key


# ---------------------------------------------------------------------------
# Host-key derivation
# ---------------------------------------------------------------------------


class TestHostKey:
    def test_url_stream_uses_host(self) -> None:
        assert _host_key("rss", {"feed_url": "https://ARS.example.com/rss"}) == "ars.example.com"

    def test_url_stream_ignores_port_and_path(self) -> None:
        assert _host_key(
            "rss", {"feed_url": "https://feeds.example.com:443/x/y"},
        ) == "feeds.example.com"

    def test_no_url_falls_back_to_source_type(self) -> None:
        # praw / github_issues streams don't carry URLs — they must all
        # bucket into the same host so per-host cap actually protects them.
        assert _host_key("reddit", {"subreddit": "windows11"}) == "reddit"

    def test_malformed_url_falls_back_to_source_type(self) -> None:
        assert _host_key("rss", {"feed_url": "not-a-url"}) == "rss"


# ---------------------------------------------------------------------------
# Dispatcher semantics
# ---------------------------------------------------------------------------


def _make_work(records: list, name: str, host: str, delay: float = 0.05):
    """Return a (callable, host) tuple that records enter/exit times."""
    def _run() -> _StreamResult:
        with _tracer_lock:
            records.append(("enter", name, time.monotonic()))
        time.sleep(delay)
        with _tracer_lock:
            records.append(("exit", name, time.monotonic()))
        r = _StreamResult()
        r.fetched = 1
        return r
    return (_run, host)


_tracer_lock = threading.Lock()


def _max_in_flight_by_host(records: list) -> dict[str, int]:
    """Compute the peak concurrent in-flight count per host from a
    trace of ('enter'|'exit', name, host, timestamp) records."""
    # In this helper `records` is ordered chronologically because the
    # workers emit both events under one lock — so we can scan linearly.
    in_flight: dict[str, int] = {}
    peak: dict[str, int] = {}
    for kind, name, _t in records:
        host = _host_of.get(name)
        if kind == "enter":
            in_flight[host] = in_flight.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), in_flight[host])
        else:
            in_flight[host] = in_flight[host] - 1
    return peak


_host_of: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _clear_host_map():
    _host_of.clear()
    yield
    _host_of.clear()


def test_per_host_cap_serializes_same_host(_clear_host_map=None) -> None:
    # Three streams on ONE host with per_host_cap=1 must run serially
    # even when max_workers=4 allows more.
    records: list = []
    items = []
    for i in range(3):
        name = f"reddit-{i}"
        _host_of[name] = "reddit"
        items.append(_make_work(records, name, "reddit"))

    results = _dispatch(items, max_workers=4, per_host_cap=1)

    assert len(results) == 3
    assert all(r.fetched == 1 for r in results)
    peaks = _max_in_flight_by_host(records)
    assert peaks["reddit"] == 1, (
        f"per_host_cap=1 must serialize same-host streams; got peak={peaks}"
    )


def test_different_hosts_run_in_parallel() -> None:
    records: list = []
    items = []
    for host, name in [("a.com", "rss-a"), ("b.com", "rss-b"), ("c.com", "rss-c")]:
        _host_of[name] = host
        items.append(_make_work(records, name, host))

    results = _dispatch(items, max_workers=4, per_host_cap=1)

    assert len(results) == 3
    peaks = _max_in_flight_by_host(records)
    total_peak = sum(peaks.values())
    # At least one moment where >=2 different-host streams were in flight
    # simultaneously (probabilistic under scheduler jitter, but this test
    # uses 50ms sleeps and 4 workers — collision is near-certain).
    max_seen = max(_count_concurrent(records))
    assert max_seen >= 2, (
        f"different-host streams should run in parallel; observed peak={max_seen}"
    )


def _count_concurrent(records: list) -> list[int]:
    """Return a list of concurrent-in-flight totals over the trace's
    lifespan. Useful because per-host cap doesn't tell us about
    cross-host parallelism directly."""
    in_flight = 0
    peaks: list[int] = []
    for kind, _name, _t in records:
        if kind == "enter":
            in_flight += 1
        else:
            in_flight -= 1
        peaks.append(in_flight)
    return peaks


def test_global_cap_bounds_total_in_flight() -> None:
    # 6 streams across 6 hosts with max_workers=2 — never more than 2
    # in flight even though per-host would allow more.
    records: list = []
    items = []
    for i in range(6):
        name = f"src-{i}"
        host = f"host-{i}.com"
        _host_of[name] = host
        items.append(_make_work(records, name, host, delay=0.03))

    _dispatch(items, max_workers=2, per_host_cap=1)

    max_seen = max(_count_concurrent(records))
    assert max_seen <= 2, (
        f"max_workers=2 must bound total in-flight; observed peak={max_seen}"
    )


def test_worker_exception_is_isolated() -> None:
    """A crashing worker must NOT stop other workers."""
    records: list = []
    items = []

    def _crashing() -> _StreamResult:
        raise RuntimeError("simulated stream crash")

    def _ok() -> _StreamResult:
        records.append("ok")
        r = _StreamResult()
        r.fetched = 5
        return r

    items = [(_crashing, "a.com"), (_ok, "b.com")]

    results = _dispatch(items, max_workers=2, per_host_cap=1)

    assert len(results) == 2
    # Crashed worker returns an error-populated result rather than
    # propagating the exception.
    assert "simulated stream crash" in " ".join(results[0].errors)
    # Sibling ran successfully.
    assert results[1].fetched == 5
    assert records == ["ok"]


def test_results_preserve_submission_order() -> None:
    """The reducer needs results in submission order so stream_labels
    matches per-stream errors in log output."""
    records: list = []
    items = []
    # Deliberately variable delays so completion order != submission order.
    delays = [0.15, 0.02, 0.08]
    for i, d in enumerate(delays):
        name = f"src-{i}"
        _host_of[name] = f"host-{i}.com"

        def _work(idx=i, delay=d) -> _StreamResult:
            time.sleep(delay)
            r = _StreamResult()
            r.fetched = idx
            return r
        items.append((_work, f"host-{i}.com"))

    results = _dispatch(items, max_workers=4, per_host_cap=1)
    assert [r.fetched for r in results] == [0, 1, 2]


def test_empty_work_list_returns_empty() -> None:
    assert _dispatch([], max_workers=4, per_host_cap=1) == []
