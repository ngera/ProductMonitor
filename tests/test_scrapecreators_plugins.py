"""End-to-end fixture tests for the three ScrapeCreators plugins.

These tests verify each platform plugin's `fetch_since()` produces the
correct `RawItem` emit shape given a known fixture. If the SC API's
response shape changes, update the fixture and this test will surface
the drift.
"""

from __future__ import annotations

import pytest

from pipeline.models import RawItem
from sources.base import FetchStats, SourceCursor
from sources.scrapecreators import client as sc_client


@pytest.fixture(autouse=True)
def _mock_and_reset(monkeypatch):
    monkeypatch.setenv("SCRAPECREATORS_MOCK", "1")
    monkeypatch.delenv("SCRAPECREATORS_API_KEY", raising=False)
    sc_client.reset_shared_client(cap=100)
    yield
    sc_client.reset_shared_client()


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------


def test_reddit_plugin_emits_posts_and_comments():
    from sources.scrapecreators.reddit import ScrapeCreatorsRedditSource

    source = ScrapeCreatorsRedditSource()
    cursor = SourceCursor(cursor_ts=None)
    stats = FetchStats()
    items = list(source.fetch_since(cursor, {"subreddit": "Windows11"}, stats))

    posts = [i for i in items if i.parent_external_id is None]
    comments = [i for i in items if i.parent_external_id is not None]

    assert len(posts) == 2, f"expected 2 posts, got {len(posts)}"
    # Only sc_post_001 has a fixture for comments; sc_post_002 does not,
    # so its comments call returns the empty-shape fallback.
    assert len(comments) >= 1

    # Every RawItem MUST have a real URL (DESIGN.md §13).
    for it in items:
        assert it.url and it.url.startswith("https://reddit.com/"), it.url

    # Posts carry title + body; comments carry a parent_context blob for
    # the classifier to read.
    post = posts[0]
    assert post.title
    assert post.source == "scrapecreators_reddit"
    assert post.engagement.get("upvotes", 0) >= 0

    if comments:
        c = comments[0]
        assert c.raw.get("parent_context")
        assert c.raw["parent_context"]["title"]

    # Cursor advances to the newest post's created_at.
    assert cursor.cursor_ts is not None
    assert cursor.cursor_ts > 0


def test_reddit_plugin_filters_deleted_comments():
    from sources.scrapecreators.reddit import ScrapeCreatorsRedditSource

    source = ScrapeCreatorsRedditSource()
    stats = FetchStats()
    items = list(source.fetch_since(
        SourceCursor(cursor_ts=None),
        {"subreddit": "Windows11"},
        stats,
    ))
    for c in items:
        if c.parent_external_id is not None:
            assert c.body not in ("[deleted]", "[removed]")
            assert c.body


def test_reddit_plugin_respects_floor_cursor():
    """Items older than the cursor should not be yielded."""
    from sources.scrapecreators.reddit import ScrapeCreatorsRedditSource

    source = ScrapeCreatorsRedditSource()
    stats = FetchStats()
    # Newer than the newest fixture item (1720100000)
    cursor = SourceCursor(cursor_ts=9_999_999_999.0)
    items = list(source.fetch_since(cursor, {"subreddit": "Windows11"}, stats))
    assert items == []


def test_reddit_plugin_skip_comments_when_disabled():
    from sources.scrapecreators.reddit import ScrapeCreatorsRedditSource

    source = ScrapeCreatorsRedditSource()
    stats = FetchStats()
    items = list(source.fetch_since(
        SourceCursor(cursor_ts=None),
        {"subreddit": "Windows11", "fetch_comments": False},
        stats,
    ))
    # Only posts; no comment fetch
    assert all(i.parent_external_id is None for i in items)


# ---------------------------------------------------------------------------
# X / Twitter
# ---------------------------------------------------------------------------


def test_x_plugin_emits_tweets_and_replies():
    from sources.scrapecreators.x import ScrapeCreatorsXSource

    source = ScrapeCreatorsXSource()
    stats = FetchStats()
    items = list(source.fetch_since(
        SourceCursor(cursor_ts=None),
        {"handle": "MSFTWindows"},
        stats,
    ))

    tweets = [i for i in items if i.parent_external_id is None]
    replies = [i for i in items if i.parent_external_id is not None]

    assert len(tweets) == 2
    assert len(replies) >= 1

    for i in items:
        assert i.url.startswith("https://x.com/"), i.url
        assert i.source == "scrapecreators_x"


def test_x_plugin_strips_leading_at_from_handle():
    from sources.scrapecreators.x import ScrapeCreatorsXSource

    source = ScrapeCreatorsXSource()
    stats = FetchStats()
    # Handle passed with leading @
    items = list(source.fetch_since(
        SourceCursor(cursor_ts=None),
        {"handle": "@MSFTWindows"},
        stats,
    ))
    assert len(items) >= 1


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------


def test_tiktok_plugin_emits_videos_and_comments():
    from sources.scrapecreators.tiktok import ScrapeCreatorsTikTokSource

    source = ScrapeCreatorsTikTokSource()
    stats = FetchStats()
    items = list(source.fetch_since(
        SourceCursor(cursor_ts=None),
        {"username": "microsoft"},
        stats,
    ))

    videos = [i for i in items if i.parent_external_id is None]
    comments = [i for i in items if i.parent_external_id is not None]

    assert len(videos) == 2
    assert len(comments) >= 1

    for i in items:
        assert i.source == "scrapecreators_tiktok"
        assert i.url.startswith("https://www.tiktok.com/"), i.url


# ---------------------------------------------------------------------------
# Credit accounting across plugins
# ---------------------------------------------------------------------------


def test_shared_credit_budget_across_plugins():
    """Reddit + X + TikTok share ONE credit tracker per run."""
    from sources.scrapecreators.reddit import ScrapeCreatorsRedditSource
    from sources.scrapecreators.tiktok import ScrapeCreatorsTikTokSource
    from sources.scrapecreators.x import ScrapeCreatorsXSource

    # Fresh client with a small cap
    sc_client.reset_shared_client(cap=100)

    r = ScrapeCreatorsRedditSource()
    x = ScrapeCreatorsXSource()
    t = ScrapeCreatorsTikTokSource()

    # Same client threaded into all three
    assert r._client is x._client is t._client

    list(r.fetch_since(SourceCursor(cursor_ts=None), {"subreddit": "Windows11"}, FetchStats()))
    reddit_spent = r._client.tracker.spent
    assert reddit_spent > 0

    list(x.fetch_since(SourceCursor(cursor_ts=None), {"handle": "MSFTWindows"}, FetchStats()))
    x_spent = x._client.tracker.spent
    # Shared tracker — spent increased
    assert x_spent > reddit_spent


def test_reset_between_runs_zeros_credits():
    from sources.scrapecreators.reddit import ScrapeCreatorsRedditSource

    r = ScrapeCreatorsRedditSource()
    list(r.fetch_since(SourceCursor(cursor_ts=None), {"subreddit": "Windows11"}, FetchStats()))
    assert r._client.tracker.spent > 0

    sc_client.reset_shared_client(cap=100)
    r2 = ScrapeCreatorsRedditSource()
    assert r2._client is not r._client
    assert r2._client.tracker.spent == 0
