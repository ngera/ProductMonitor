"""Tests for normalize-stage derived fields (ADR-0028 collapsed
author_intent into per-item content_type + existing is_reply).

The old `_author_intent()` helper was removed. Content_type now travels
on RawItem / the raw JSONL — normalize just copies it into the items row
(with a defensive fallback for legacy JSONL). The reply-vs-top-level
distinction stays on the `is_reply` field driven by parent_external_id.
"""

from __future__ import annotations

import pytest

pytest.importorskip("structlog")

from pipeline.normalize import _to_item_row


def _rec(**over):
    """Minimal raw JSONL dict."""
    from datetime import datetime, timezone
    base = {
        "source": "hn",
        "source_display_name": "Hacker News",
        "external_id": "42",
        "url": "https://example.com/42",
        "parent_external_id": None,
        "author": "u",
        "created_at": datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).isoformat(),
        "title": "t",
        "body": "b",
        "content_type": "user_feedback",
        "engagement": {},
        "raw": {},
    }
    base.update(over)
    return base


def _row(rec):
    from datetime import datetime, timezone
    from pathlib import Path
    return _to_item_row(rec, Path("dummy.jsonl"),
                        datetime.now(timezone.utc))


def test_is_reply_true_when_parent_id_present():
    r = _row(_rec(parent_external_id="abc"))
    assert r["is_reply"] is True


def test_is_reply_false_when_no_parent():
    r = _row(_rec(parent_external_id=None))
    assert r["is_reply"] is False


def test_content_type_flows_through_from_raw():
    r = _row(_rec(content_type="media_coverage"))
    assert r["content_type"] == "media_coverage"


def test_content_type_defaults_to_user_feedback_on_legacy_rec():
    # Legacy JSONL from before ADR-0028 has no content_type field.
    # Normalize backfills defensively.
    r = _row(_rec(content_type=None))
    assert r["content_type"] == "user_feedback"


def test_row_no_longer_has_author_intent_key():
    r = _row(_rec())
    assert "author_intent" not in r, (
        "author_intent was removed by ADR-0028 — the normalize row must "
        "not carry it or the DB write will fail."
    )
