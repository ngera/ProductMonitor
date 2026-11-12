"""429 handling for RSS feeds, Reddit especially (sources/rss.py).

In the Supabase 2026-W39 run, 5 of 6 subreddits came back empty: the first
429 didn't stop the others from requesting (and retrying), and nothing told
the next run to wait. These tests drive the real fetch path through an
httpx MockTransport — no network.
"""

from __future__ import annotations

import httpx
import pytest

from pipeline import http as retry_http
from sources import rss
from sources.base import FetchStats, SourceCursor
from sources.reddit_rss import RedditRssSource

_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_abc</id><title>Supabase auth broke after upgrade</title>
    <link href="https://www.reddit.com/r/supabase/comments/abc/x/"/>
    <updated>2026-09-20T10:00:00+00:00</updated>
    <author><name>/u/someone</name></author>
    <content type="html">&lt;div class="md"&gt;&lt;p&gt;Login loops since the upgrade, any ideas about the cause?&lt;/p&gt;&lt;/div&gt;</content>
  </entry>
</feed>"""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(retry_http.time, "sleep", lambda _s: None)


def _source(handler):
    src = RedditRssSource()
    src._client = httpx.Client(transport=httpx.MockTransport(handler))
    return src


def _fetch(src, sub):
    stats = FetchStats()
    items = list(src.fetch_since(SourceCursor(), {"name": f"s-{sub}", "subreddit": sub}, stats))
    return items, stats


def test_429_starts_cooldown_and_later_subreddits_skip_the_request():
    requested: list[str] = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "120"})

    src = _source(handler)
    items1, stats1 = _fetch(src, "webdev")
    n_after_first = len(requested)
    items2, stats2 = _fetch(src, "startups")

    assert items1 == [] and items2 == []
    assert ("rss:s-webdev:rate-limited", 0.0) in stats1.ceiling_hits
    assert ("rss:s-startups:rate-limited", 0.0) in stats2.ceiling_hits
    # The second subreddit never hit Reddit.
    assert len(requested) == n_after_first
    assert 110 < rss.host_cooldown_remaining("reddit.com") <= 120


def test_cooldown_survives_into_a_new_run():
    rss.record_rate_limit("reddit.com", "600")
    calls = []
    src = _source(lambda r: calls.append(1) or httpx.Response(200, content=_FEED))
    items, stats = _fetch(src, "supabase")
    assert items == [] and calls == []
    assert stats.ceiling_hits == [("rss:s-supabase:rate-limited", 0.0)]


def test_cooldown_expires():
    rss.record_rate_limit("reddit.com", "60", now=1_000.0)
    assert rss.host_cooldown_remaining("reddit.com", now=1_030.0) == pytest.approx(30.0)
    assert rss.host_cooldown_remaining("reddit.com", now=1_061.0) == 0.0


def test_missing_retry_after_uses_default_and_huge_value_is_capped():
    assert rss.record_rate_limit("example.org", None, now=0.0) == rss._COOLDOWN_DEFAULT_SECONDS
    assert rss.record_rate_limit("example.net", "999999", now=0.0) == rss._COOLDOWN_MAX_SECONDS


def test_other_hosts_are_not_blocked_by_reddit_cooldown():
    rss.record_rate_limit("reddit.com", "600")
    src = rss.RssSource()
    src._client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=_FEED)))
    stats = FetchStats()
    items = list(src.fetch_since(SourceCursor(),
                                 {"name": "verge", "feed_url": "https://www.theverge.com/rss/index.xml"},
                                 stats))
    assert len(items) == 1
    assert stats.ceiling_hits == []


def test_successful_feed_still_parses():
    src = _source(lambda r: httpx.Response(200, content=_FEED))
    items, stats = _fetch(src, "supabase")
    assert [i.title for i in items] == ["Supabase auth broke after upgrade"]
    assert items[0].content_type == "user_feedback"
    assert stats.ceiling_hits == []
