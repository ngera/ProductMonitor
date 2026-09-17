"""Reddit RSS — dedicated per-subreddit RSS plugin.

Wraps the generic `sources.rss.RssSource` fetch/parse code but exposes a
subreddit-name field to users so they type `r/cursor` instead of pasting
the full RSS URL (a common gotcha — hitting the bare subreddit page URL
returns HTML that the RSS parser chokes on).

On fetch, the subreddit name is expanded to `https://www.reddit.com/r/<name>/new.rss`
via `sources.rss.expand_reddit_shorthand`. Legacy streams that still carry
`feed_url` continue to work — if `subreddit` is absent, we extract the
name from the URL so no existing product's sources.yaml breaks.
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, SourceCursor, SourceManifest
from sources.rss import RssSource, expand_reddit_shorthand


MANIFEST = SourceManifest(
    plugin_id="reddit_rss",
    display_name="Reddit RSS",
    version="0.2.0",
    docs_url="https://www.reddit.com",
    help=(
        "Reddit per-subreddit RSS feeds — a keyless alternative to the "
        "OAuth Reddit Data API. Enter subreddits as r/<name> (or just "
        "<name>). We fetch each subreddit's /new.rss feed. Set "
        "REDDIT_USER_AGENT in .env to reduce 429 rate limits; the plugin "
        "auto-uses it. When you have many subreddits, set 'Sleep before "
        "fetch' to 3-5 seconds."
    ),
    connection_fields=[],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="cursor",
                  help="Internal label for cursor / dedup. Also the default display name."),
        FieldSpec(name="subreddit", label="Subreddit", type="text", required=True,
                  placeholder="r/cursor",
                  help="Enter as r/<name> (or just <name>). Feed URL is built automatically at fetch time."),
        FieldSpec(name="display", label="Display label", type="text", default="",
                  placeholder="r/cursor",
                  help="Human-readable name shown in reports. Defaults to r/<subreddit>."),
        FieldSpec(name="sleep_before_fetch_seconds", label="Sleep before fetch (seconds)", type="number", default=3,
                  help="Reddit throttles aggressively; 3-5 seconds between fetches is recommended when you have many subreddits."),
    ],
    identifier_field="subreddit",
    supports_bulk_add=True,
    source_category="rss_feed",
    content_types=["user_feedback"],
)


# Matches the subreddit name inside any Reddit URL we've ever seen a user
# paste: bare page (/r/foo/), listing (/r/foo/new/), Old Reddit
# (old.reddit.com), .rss variants, http/https, with or without trailing
# slash. Captures the subreddit name in group 1.
_REDDIT_URL_RE = re.compile(
    r"^https?://(?:www\.|old\.)?reddit\.com/r/([A-Za-z0-9_]{2,21})",
    re.IGNORECASE,
)
# Bare shorthand (with or without leading slash / r/ prefix).
_SHORTHAND_RE = re.compile(
    r"^/?r?/?([A-Za-z0-9_]{2,21})/?$", re.IGNORECASE,
)


def normalize_subreddit(raw: str) -> str:
    """Normalize any user input into a bare subreddit name (no r/, no URL).

    Accepts:
      cursor, r/cursor, /r/cursor, R/Cursor,
      https://www.reddit.com/r/cursor/,
      https://www.reddit.com/r/cursor/new.rss,
      https://old.reddit.com/r/cursor/top/

    Returns the bare name, empty string on invalid input. Case is preserved
    (Reddit URLs are case-insensitive but the display looks nicer with the
    user's chosen casing).
    """
    s = (raw or "").strip()
    if not s:
        return ""
    m = _REDDIT_URL_RE.match(s)
    if m:
        return m.group(1)
    m = _SHORTHAND_RE.match(s)
    if m:
        return m.group(1)
    return ""


def _feed_url_for(subreddit: str) -> str:
    """Bare subreddit name → the RSS URL to fetch."""
    return f"https://www.reddit.com/r/{subreddit}/new.rss"


class RedditRssSource(RssSource):
    """Same fetch behavior as `RssSource`; injects `feed_url` from
    `subreddit` at fetch time so users only ever see r/<name> in the UI.

    Backward-compat: if a legacy stream still has `feed_url` set but no
    `subreddit`, we extract the subreddit from the URL and log a note.
    That path also fixes pre-migration configs where the URL was the bare
    subreddit page (returns HTML, parser fails) — extraction pulls out the
    name and we rebuild the correct /new.rss URL.
    """

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats,
    ) -> Iterator[RawItem]:
        subreddit = normalize_subreddit(config.get("subreddit") or "")
        if not subreddit:
            # Legacy path: derive from feed_url if present.
            legacy_url = (config.get("feed_url") or "").strip()
            if legacy_url:
                subreddit = normalize_subreddit(legacy_url)
                # `expand_reddit_shorthand` also accepts r/<name>; keeps parity
                # with the generic RssSource path for anyone bypassing this
                # override with type: rss.
                if not subreddit:
                    resolved = expand_reddit_shorthand(legacy_url)
                    subreddit = normalize_subreddit(resolved)
        if not subreddit:
            raise ValueError(
                "reddit_rss stream needs `subreddit: r/<name>` (was "
                f"{config.get('subreddit')!r} / {config.get('feed_url')!r})"
            )
        # Inject the real URL and delegate to the RSS base. Mutating a copy
        # so we don't smear state across streams. content_type override
        # (ADR-0028) tells the RSS base to yield user_feedback instead
        # of the media_coverage default — subreddit posts are user
        # commentary, not editorial content.
        effective = dict(config)
        effective["feed_url"] = _feed_url_for(subreddit)
        effective["content_type"] = "user_feedback"
        yield from super().fetch_since(cursor, effective, stats)
