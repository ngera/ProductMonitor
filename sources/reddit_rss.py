"""Reddit RSS — dedicated per-subreddit RSS plugin.

Wraps the generic `sources.rss.RssSource` (which already carries Reddit-
specific rate-limit + User-Agent handling in `_is_reddit_host` /
`_ua_for_url`). The only real difference from the generic `rss` plugin is
the manifest: Reddit-oriented placeholders, help text, and a display name
so the wizard's sub-step 2 shows "Reddit RSS" as a distinct card next to
"Hacker News", not lumped under "Media Coverage Sources".

Under the hood the fetch code is identical — a `type: reddit_rss` entry
in a product's `sources.yaml` behaves exactly like a `type: rss` entry
pointing at a Reddit URL. The separation is purely a UX affordance.
"""

from __future__ import annotations

from sources.base import FieldSpec, SourceManifest
from sources.rss import RssSource


MANIFEST = SourceManifest(
    plugin_id="reddit_rss",
    display_name="Reddit RSS",
    version="0.1.0",
    docs_url="https://www.reddit.com",
    help=(
        "Reddit per-subreddit RSS feeds — a keyless alternative to the "
        "OAuth Reddit Data API. Paste one subreddit's RSS URL per stream, "
        "e.g. https://www.reddit.com/r/Windows11/new.rss. Also accepts "
        "/top.rss and /rising.rss variants. Set REDDIT_USER_AGENT in .env "
        "to reduce 429 rate limits; the plugin auto-uses it. When you need "
        "many subreddits, set 'Sleep before fetch' to 3-5 seconds."
    ),
    connection_fields=[],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="windows11",
                  help="Internal label for cursor / dedup. Also the default display name."),
        FieldSpec(name="feed_url", label="Subreddit RSS URL", type="text", required=True,
                  placeholder="https://www.reddit.com/r/Windows11/new.rss",
                  help="Reddit URL: https://www.reddit.com/r/SUBREDDIT/new.rss. Also accepts /top.rss and /rising.rss."),
        FieldSpec(name="display", label="Display label", type="text", default="",
                  placeholder="r/Windows11",
                  help="Human-readable name shown in reports. Defaults to the stream name."),
        FieldSpec(name="sleep_before_fetch_seconds", label="Sleep before fetch (seconds)", type="number", default=3,
                  help="Reddit throttles aggressively; 3-5 seconds between fetches is recommended when using multiple subreddit streams."),
    ],
    identifier_field="feed_url",
    supports_bulk_add=True,
    source_category="rss_feed",
    content_types=["user_feedback"],
)


class RedditRssSource(RssSource):
    """Same fetch behavior as `RssSource`; distinct manifest for wizard UX.

    Kept as a subclass rather than a re-registration of `RssSource` under a
    new key so anything checking `isinstance(source, RssSource)` (e.g. in
    the fetch pipeline's error handling) works uniformly.
    """
    pass
