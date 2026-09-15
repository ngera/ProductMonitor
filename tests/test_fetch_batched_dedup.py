"""Regression guard for perf fix #6 in pipeline/fetch.py.

Previously the fetch loop called `storage.filter_unseen(source_type,
[raw_item.external_id])` INSIDE the per-item loop — one SQLite
connection per RSS entry. The fix collects the stream's items into a
list, then calls filter_unseen once per stream with all ids.

Rather than spinning up the whole fetch stage (imports pull in half the
codebase), assert the loop shape at the source level: filter_unseen is
called with the full id list, not a single-element list. A future
refactor that reverts to per-item calls would fail this test.
"""

from __future__ import annotations

from pathlib import Path

_FETCH_SRC = (Path(__file__).resolve().parent.parent
              / "pipeline" / "fetch.py").read_text(encoding="utf-8")


def test_filter_unseen_called_with_full_id_list() -> None:
    # The batched form iterates raw_items to build the list; the old form
    # passed [raw_item.external_id] directly inside the loop.
    assert "[raw_item.external_id]" not in _FETCH_SRC, (
        "fetch.py appears to have reverted to per-item filter_unseen — "
        "each call opens a SQLite connection and competes with the webui "
        "for file locks."
    )
    # New form: comprehension across the collected raw_items.
    assert "[ri.external_id for ri in raw_items]" in _FETCH_SRC


def test_raw_items_buffer_precedes_dedup() -> None:
    # The collect-then-dedup pattern requires materializing the stream into
    # a list before the filter_unseen call.
    idx_buffer = _FETCH_SRC.find("raw_items.append(raw_item)")
    idx_filter = _FETCH_SRC.find("storage.filter_unseen(")
    assert idx_buffer > 0 and idx_filter > 0
    assert idx_buffer < idx_filter, (
        "raw_items must be collected before storage.filter_unseen is called"
    )
