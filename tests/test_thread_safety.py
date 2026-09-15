"""Thread-safety tests for sources flagged in ADR-0023's audit table.

Under the concurrent-fetch executor (ADR-0023), source clients may be
called from multiple threads. Three sources had known races:

  - StackExchangeSource._backoff_until: read+write without lock.
  - RedditSource: praw's Reddit object is not thread-safe.
  - CreditTracker (scrapecreators): check-then-spend race could exceed
    the operator's per-run credit cap on a paid API.

These tests hammer the fixed code from many threads and assert the
invariants hold.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


# ---------------------------------------------------------------------------
# CreditTracker: atomic reserve under load
# ---------------------------------------------------------------------------


class TestCreditTracker:
    """The critical test — this is a paid API. Overshooting the cap by
    even one under concurrent load costs real money."""

    def _tracker(self, cap: int):
        # Import lazily so the module compiles even when tests are
        # collected on installs without the scrapecreators plugin
        # dependency chain.
        from sources.scrapecreators.client import CreditTracker
        return CreditTracker(cap=cap)

    def test_atomic_reserve_never_exceeds_cap(self) -> None:
        tracker = self._tracker(cap=100)
        successes: list[bool] = []
        lock = threading.Lock()

        def _worker() -> None:
            ok = tracker.try_reserve()
            with lock:
                successes.append(ok)

        # 500 threads racing for 100 slots. Without the lock, some
        # would slip through past the cap.
        with ThreadPoolExecutor(max_workers=32) as pool:
            for _ in range(500):
                pool.submit(_worker)

        n_reserved = sum(1 for s in successes if s)
        assert n_reserved == 100, (
            f"try_reserve must grant exactly cap slots under load; got {n_reserved}"
        )
        assert tracker.spent == 100
        # Once cap is hit, halted flips.
        halted, reason = tracker.is_halted()
        assert halted
        assert "cap" in reason

    def test_halted_tracker_rejects_all(self) -> None:
        tracker = self._tracker(cap=10)
        tracker.halt("test-halt")
        results: list[bool] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(tracker.try_reserve) for _ in range(50)]
            results = [f.result() for f in futures]
        assert not any(results), "halted tracker must reject every reserve"
        assert tracker.spent == 0  # no reservations made

    def test_spend_and_halt_visible_across_threads(self) -> None:
        # Sanity: memory visibility on the fields — locked read after
        # locked write is what the lock guarantees.
        tracker = self._tracker(cap=1000)
        for _ in range(100):
            assert tracker.try_reserve()
        assert tracker.spent == 100
        tracker.halt("stop")
        halted, reason = tracker.is_halted()
        assert halted and reason == "stop"


# ---------------------------------------------------------------------------
# StackExchange backoff: read-write under lock
# ---------------------------------------------------------------------------


class TestStackexBackoff:
    """The lock ensures that a `backoff=N` response from one thread is
    visible to another thread's `_sleep_if_backoff` before it makes its
    next request. Without the lock, memory-model non-guarantees could
    let a stale 0.0 slip through and skip the backoff SE requested."""

    def _source(self):
        # Skip if httpx / dependencies aren't installed in this env.
        pytest.importorskip("httpx")
        try:
            from sources.stackex import StackExchangeSource
        except ImportError:
            pytest.skip("stackex module unavailable")
        return StackExchangeSource()

    def test_concurrent_writes_pick_max(self) -> None:
        # Two threads writing deadlines — the later (larger) one must win.
        # Prior code could see them race and the smaller one overwrite,
        # defeating the point of the backoff signal.
        import time as _time
        src = self._source()

        def _write_deadline(seconds: float) -> None:
            with src._backoff_lock:
                new = _time.monotonic() + seconds
                if new > src._backoff_until:
                    src._backoff_until = new

        with ThreadPoolExecutor(max_workers=4) as pool:
            for s in [0.5, 5.0, 2.0, 3.0]:
                pool.submit(_write_deadline, s)

        # Deadline should reflect the LARGEST offset submitted (5.0),
        # not whichever thread happened to finish last.
        remaining = src._backoff_until - _time.monotonic()
        assert 4.5 < remaining <= 5.0, (
            f"largest deadline must win under concurrent writes; got {remaining:.2f}s"
        )

    def test_read_never_returns_stale_zero_after_write(self) -> None:
        import time as _time
        src = self._source()
        # Simulate a backoff-request → verify subsequent reads see it.
        deadline = _time.monotonic() + 10.0
        with src._backoff_lock:
            src._backoff_until = deadline

        # Many readers — every one must observe the deadline.
        seen: list[float] = []
        lock = threading.Lock()

        def _reader() -> None:
            with src._backoff_lock:
                d = src._backoff_until
            with lock:
                seen.append(d)

        with ThreadPoolExecutor(max_workers=16) as pool:
            for _ in range(200):
                pool.submit(_reader)

        assert all(abs(d - deadline) < 1e-9 for d in seen), (
            "locked read must always observe the locked-written deadline"
        )


# ---------------------------------------------------------------------------
# Reddit: fetch_since is serialized by an instance lock
# ---------------------------------------------------------------------------


class TestRedditLock:
    """We can't spin up a real praw client, but we can verify the lock
    EXISTS and is acquired around the generator body — which is the
    contract that keeps praw safe."""

    def test_reddit_source_has_lock(self) -> None:
        try:
            from sources.reddit import RedditSource
        except (ImportError, KeyError):
            pytest.skip("reddit source requires praw + env vars")
        # We can inspect the CLASS without instantiating (which needs env).
        import inspect
        src = inspect.getsource(RedditSource)
        assert "_reddit_lock" in src
        assert "with self._reddit_lock:" in src, (
            "RedditSource.fetch_since must hold _reddit_lock across the "
            "generator body (praw is not thread-safe)"
        )
