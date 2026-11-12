"""Live network checks for the keyless sources — opt-in.

Every other test in this suite runs offline against recorded responses. These
call the real endpoints, so they catch what offline tests can't: an API that
changed shape, a feed that moved, Reddit's rate limiting. They are skipped
unless you ask for them:

    PowerShell:  $env:PM_LIVE_TESTS = "1"; pytest -m live -v
    bash:        PM_LIVE_TESTS=1 pytest -m live -v

Set REDDIT_USER_AGENT first (see .env.example) — Reddit throttles the
fallback agent hard. A rate-limited Reddit answer passes as long as it is
reported (health signal + cooldown); a silent empty result fails.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("PM_LIVE_TESTS") != "1",
                       reason="live network test; set PM_LIVE_TESTS=1 to run"),
]

httpx = pytest.importorskip("httpx")
pytest.importorskip("feedparser")

from sources.base import FetchStats, SourceCursor  # noqa: E402

# Last 30 days, so the check doesn't depend on a quiet week.
_SINCE = time.time() - 30 * 86400


def _fetch(src, config):
    stats = FetchStats()
    items = list(src.fetch_since(SourceCursor(cursor_ts=_SINCE), config, stats))
    return items, stats


def test_hacker_news_search_returns_stories_with_engagement():
    from sources.hn import HackerNewsSource

    items, _ = _fetch(HackerNewsSource(), {
        "name": "live-hn", "search_queries": ["postgres"],
        "include_tags": ["story"], "max_pages_per_query": 1, "hits_per_page": 20,
    })
    assert items, "HN Algolia returned no stories for 'postgres' in 30 days"
    assert all(i.url and i.title for i in items)
    assert any((i.engagement or {}).get("points") is not None for i in items), \
        "HN items lost their points — ranking would go flat"


def test_media_rss_feed_parses_as_media_coverage():
    from sources.rss import RssSource

    items, stats = _fetch(RssSource(), {
        "name": "live-verge", "feed_url": "https://www.theverge.com/rss/index.xml",
    })
    assert not stats.ceiling_hits, f"feed health problem: {stats.ceiling_hits}"
    assert items, "The Verge feed parsed to zero entries"
    assert {i.content_type for i in items} == {"media_coverage"}


def test_reddit_rss_returns_posts_or_reports_the_rate_limit():
    from sources import rss
    from sources.reddit_rss import RedditRssSource

    # conftest isolates the cooldown file; start from no cooldown.
    items, stats = _fetch(RedditRssSource(), {"name": "live-python", "subreddit": "python"})

    if items:
        assert {i.content_type for i in items} == {"user_feedback"}
        assert all("reddit.com" in i.url for i in items)
        return

    # Empty is only acceptable when it is explained.
    assert ("rss:live-python:rate-limited", 0.0) in stats.ceiling_hits, (
        f"Reddit returned nothing and no health signal: {stats.ceiling_hits}"
    )
    assert rss.host_cooldown_remaining("reddit.com") > 0, \
        "429 seen but no cooldown recorded — the next stream would hit Reddit again"


def test_apple_app_store_reviews_feed_still_answers():
    from sources.apple_appstore import AppleAppStoreSource

    # 284882215 = Facebook: always has recent US reviews.
    items, _ = _fetch(AppleAppStoreSource(), {
        "name": "live-appstore", "app_id": "284882215", "countries": ["us"], "max_pages": 1,
    })
    assert items, "App Store review feed returned nothing for a high-volume app"
