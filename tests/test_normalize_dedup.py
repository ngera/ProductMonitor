"""Cross-source canonical-URL dedup at normalize (ADR-0024).

Tests the pure `_dedup_by_canonical_url` helper without spinning up the
full normalize stage (which requires a DuckDB warehouse).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# normalize.py imports structlog + config; skip cleanly on installs that
# haven't run `pip install -r requirements.txt` yet.
pytest.importorskip("structlog")

from pipeline.normalize import _dedup_by_canonical_url


def _row(
    *,
    id: str,
    source: str,
    canonical_url: str | None,
    created_at: datetime,
    is_reply: bool = False,
    external_id: str | None = None,
) -> dict:
    return {
        "id": id,
        "source": source,
        "external_id": external_id or id.split(":", 1)[-1],
        "canonical_url": canonical_url,
        "created_at": created_at,
        "is_reply": is_reply,
    }


BASE = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_no_duplicates_passes_through() -> None:
    rows = [
        _row(id="rss:a", source="rss", canonical_url="https://a.com/x", created_at=BASE),
        _row(id="hn:b",  source="hn",  canonical_url="https://b.com/y", created_at=BASE),
    ]
    kept, dropped = _dedup_by_canonical_url(rows)
    assert dropped == 0
    assert {r["id"] for r in kept} == {"rss:a", "hn:b"}


def test_two_sources_same_canonical_earliest_wins() -> None:
    # RSS picks up the article first; HN link post appears later.
    rows = [
        _row(id="hn:123", source="hn", canonical_url="https://blog.com/post",
             created_at=BASE + timedelta(hours=2)),
        _row(id="rss:abc", source="rss", canonical_url="https://blog.com/post",
             created_at=BASE),
    ]
    kept, dropped = _dedup_by_canonical_url(rows)
    assert dropped == 1
    assert {r["id"] for r in kept} == {"rss:abc"}


def test_null_canonical_never_dedupes() -> None:
    # Two items with NULL canonical_url (e.g. reddit self-posts) must
    # both survive — pre-ADR-0024 behavior preserved.
    rows = [
        _row(id="reddit:1", source="reddit", canonical_url=None, created_at=BASE),
        _row(id="reddit:2", source="reddit", canonical_url=None, created_at=BASE),
    ]
    kept, dropped = _dedup_by_canonical_url(rows)
    assert dropped == 0
    assert len(kept) == 2


def test_replies_bypass_dedup() -> None:
    # Two comments happen to share a URL (e.g. same permalink shape) —
    # they must NOT be collapsed, because comment identity is per-thread.
    rows = [
        _row(id="reddit:c1", source="reddit",
             canonical_url="https://reddit.com/r/x/comments/abc",
             created_at=BASE, is_reply=True),
        _row(id="reddit:c2", source="reddit",
             canonical_url="https://reddit.com/r/x/comments/abc",
             created_at=BASE, is_reply=True),
    ]
    kept, dropped = _dedup_by_canonical_url(rows)
    assert dropped == 0
    assert len(kept) == 2


def test_alphabetical_tiebreak_on_equal_timestamp() -> None:
    rows = [
        _row(id="rss:x", source="rss", canonical_url="https://c.com/x", created_at=BASE),
        _row(id="hn:y",  source="hn",  canonical_url="https://c.com/x", created_at=BASE),
    ]
    kept, dropped = _dedup_by_canonical_url(rows)
    assert dropped == 1
    # "hn" < "rss" alphabetically, so hn:y wins.
    assert kept[0]["id"] == "hn:y"


def test_three_way_dedup() -> None:
    rows = [
        _row(id="hn:1", source="hn", canonical_url="https://z.com/p",
             created_at=BASE + timedelta(hours=3)),
        _row(id="rss:1", source="rss", canonical_url="https://z.com/p",
             created_at=BASE + timedelta(hours=1)),
        _row(id="reddit:1", source="reddit", canonical_url="https://z.com/p",
             created_at=BASE + timedelta(hours=2)),
    ]
    kept, dropped = _dedup_by_canonical_url(rows)
    assert dropped == 2
    assert kept[0]["id"] == "rss:1"


def test_top_level_dedupes_reply_stays() -> None:
    # A parent post AND a reply that happen to canonicalize the same
    # (permalink weirdness). Parent participates in dedup; reply does not.
    top = _row(id="rss:parent", source="rss",
               canonical_url="https://s.com/thread",
               created_at=BASE)
    top_dupe = _row(id="hn:parent-repost", source="hn",
                    canonical_url="https://s.com/thread",
                    created_at=BASE + timedelta(hours=1))
    reply = _row(id="reddit:reply", source="reddit",
                 canonical_url="https://s.com/thread",
                 created_at=BASE + timedelta(hours=2),
                 is_reply=True)
    kept, dropped = _dedup_by_canonical_url([top_dupe, top, reply])
    assert dropped == 1
    ids = {r["id"] for r in kept}
    # top wins over top_dupe; reply passes through untouched.
    assert ids == {"rss:parent", "reddit:reply"}
