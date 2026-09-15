"""Tests for the batched storage writes introduced for perf regression #6.

Prior behavior: relevance and classify called `set_relevance` /
`set_filter_status` once per item, each opening its own DuckDB warehouse
connection under a 5.4-second lock-retry ladder. On a 2,000-item week
that was ~3,000 lock-contended open/close cycles.

These tests assert the batched replacements collapse N updates into ONE
connection open, and no-op cleanly on empty input.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest

# storage.py imports duckdb at module load; skip cleanly on installs that
# haven't run `pip install -r requirements.txt` yet.
pytest.importorskip("duckdb")


class _FakeCursor:
    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def execute(self, sql: str, params: Any = None) -> "_FakeCursor":
        self._log.append(("execute", sql, params))
        return self

    def executemany(self, sql: str, rows: list[Any]) -> "_FakeCursor":
        self._log.append(("executemany", sql, list(rows)))
        return self


class _ConnCounter:
    def __init__(self) -> None:
        self.opened = 0
        self.calls: list[tuple[str, Any]] = []

    @contextmanager
    def warehouse(self) -> Iterator[_FakeCursor]:
        self.opened += 1
        yield _FakeCursor(self.calls)


@pytest.fixture
def stub_warehouse(monkeypatch: pytest.MonkeyPatch) -> _ConnCounter:
    from pipeline import storage
    counter = _ConnCounter()
    monkeypatch.setattr(storage, "warehouse", counter.warehouse)
    return counter


def test_set_relevance_batch_opens_one_connection(stub_warehouse: _ConnCounter) -> None:
    from pipeline import storage
    rows = [(f"hn:{i}", 0.9, True) for i in range(250)]
    storage.set_relevance_batch(rows)
    assert stub_warehouse.opened == 1
    # One executemany call carrying all 250 rows.
    call_kinds = [c[0] for c in stub_warehouse.calls]
    assert call_kinds == ["executemany"]
    assert len(stub_warehouse.calls[0][2]) == 250


def test_set_filter_status_batch_opens_one_connection(
    stub_warehouse: _ConnCounter,
) -> None:
    from pipeline import storage
    rows = [(f"hn:{i}", "dropped:not_topic_relevant") for i in range(50)]
    storage.set_filter_status_batch(rows)
    assert stub_warehouse.opened == 1
    assert len(stub_warehouse.calls[0][2]) == 50


def test_set_relevance_batch_empty_is_noop(stub_warehouse: _ConnCounter) -> None:
    from pipeline import storage
    storage.set_relevance_batch([])
    assert stub_warehouse.opened == 0
    assert stub_warehouse.calls == []


def test_set_filter_status_batch_empty_is_noop(stub_warehouse: _ConnCounter) -> None:
    from pipeline import storage
    storage.set_filter_status_batch([])
    assert stub_warehouse.opened == 0
    assert stub_warehouse.calls == []


def test_set_relevance_batch_row_shape(stub_warehouse: _ConnCounter) -> None:
    """The batched form must produce the same [score, is_relevant, id] row
    shape as the single-row version — the WHERE clause is `WHERE id=?` last."""
    from pipeline import storage
    storage.set_relevance_batch([("hn:1", 0.8, True), ("hn:2", 0.2, False)])
    sql, rows = stub_warehouse.calls[0][1], stub_warehouse.calls[0][2]
    assert "WHERE id=?" in sql
    assert rows == [[0.8, True, "hn:1"], [0.2, False, "hn:2"]]


def test_set_filter_status_batch_row_shape(stub_warehouse: _ConnCounter) -> None:
    from pipeline import storage
    storage.set_filter_status_batch(
        [("hn:1", "passed"), ("hn:2", "classification_failed")]
    )
    sql, rows = stub_warehouse.calls[0][1], stub_warehouse.calls[0][2]
    assert "WHERE id=?" in sql
    assert rows == [["passed", "hn:1"], ["classification_failed", "hn:2"]]
