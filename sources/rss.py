"""Generic RSS / Atom feed connector (SOURCE_RSS.md).

No auth. Reads any public RSS or Atom feed via `feedparser`. Intended as a
context layer — news / blog coverage that corroborates primary-source
reports. Credibility weight defaults low (~0.5); it's not primary signal.

Stream config:

    streams:
      - name: windows-central
        feed_url: https://www.windowscentral.com/rss.xml
      - name: bleepingcomputer-microsoft
        feed_url: https://www.bleepingcomputer.com/feed/category/microsoft/
        credibility_weight_override: 0.8   # more reliable than most
      - name: substack-someone
        feed_url: https://someone.substack.com/feed
      - name: newsletter-x
        feed_url: https://example.beehiiv.com/feed
        display: "X (Substack)"           # human-readable name in reports

One feed per stream keeps cursor handling clean. The plugin gracefully
handles broken feeds (404/410) and malformed XML (bozo flag) — it logs and
moves on rather than crashing the whole run.

Gotcha: most RSS feeds keep only ~10-30 latest items. If a feed publishes
faster than your fetch cadence, you'll silently miss middle items. If that
matters, either run more frequently or accept the loss (it's still a
low-primary-signal source).
"""

from __future__ import annotations

import html
import logging
import os
import re
from calendar import timegm
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

import feedparser
import httpx

from pipeline import http as _retry_http
from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="rss",
    # Renamed from "Reddit RSS" — the plugin is generic and primarily used
    # for media coverage feeds (Wired, The Verge, etc.); for subreddit RSS
    # feeds specifically, use the dedicated `reddit_rss` plugin which shares
    # this same fetch code but presents Reddit-oriented placeholders + help.
    display_name="Media Coverage Sources",
    version="0.1.0",
    docs_url="https://www.wired.com/feed/rss",
    help=(
        "Generic RSS/Atom fetcher — one feed URL per stream. Best fit for "
        "tech + business news publications (Wired, The Verge, Ars Technica, "
        "TechCrunch, Bloomberg, etc.). See Admin > Sources for a curated "
        "catalog of feed URLs you can copy in, or click 'Enable all missing "
        "feeds' on this product's Sources page to opt in with one click. "
        "Also accepts blog RSS, Substack, Beehiiv, or any public feed. "
        "For Reddit RSS specifically, use the `reddit_rss` plugin — it uses "
        "the same fetch code but ships Reddit-oriented placeholders."
    ),
    connection_fields=[],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="wired", help="Internal label for cursor / dedup. Also the default display name."),
        FieldSpec(name="feed_url", label="Feed URL", type="text", required=True,
                  placeholder="https://www.wired.com/feed/rss",
                  help="Public RSS or Atom feed URL."),
        FieldSpec(name="display", label="Display label", type="text", default="",
                  placeholder="Wired",
                  help="Human-readable name shown in reports. Defaults to the stream name."),
        FieldSpec(name="sleep_before_fetch_seconds", label="Sleep before fetch (seconds)", type="number", default=0,
                  help="Pause before this stream fetches. Useful when multiple streams target the same rate-limited host."),
    ],
    identifier_field="feed_url",
    supports_bulk_add=True,
    source_category="rss_feed",
    content_types=["media_coverage"],
)

log = logging.getLogger(__name__)

_USER_AGENT = "product-monitor/0.1"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# feedparser accepts a bytes payload; we fetch via httpx so we can set a
# proper User-Agent (some publishers 403 the default feedparser UA).
_TIMEOUT = httpx.Timeout(20.0)

# Reddit's RSS is a legitimate fallback when the OAuth Data API isn't set up,
# but Reddit is stricter about rate limits + wants an identified User-Agent.
# We detect Reddit URLs and adapt the request accordingly. See
# https://github.com/reddit-archive/reddit/wiki/API for the UA guidance.
_REDDIT_HOSTS = frozenset({
    "www.reddit.com", "reddit.com", "old.reddit.com", "np.reddit.com",
})


def _is_reddit_host(url: str) -> bool:
    try:
        return urlsplit(url).netloc.lower() in _REDDIT_HOSTS
    except Exception:
        return False


_reddit_ua_warned = False


def _ua_for_url(url: str) -> str:
    """Return a User-Agent tuned to the target host.

    Reddit: prefers `<platform>:<app>:<version> (by /u/<username>)`. If the
    user has set REDDIT_USER_AGENT in .env (for the PRAW-based reddit source)
    we reuse it here so RSS gets the same identity. Falls back to a generic
    identifier — Reddit will accept it but throttles unidentified UAs hard
    (observed: 429 after the first successful fetch, even with 4s delays).
    We warn once per process when using the fallback.

    Other hosts: default project UA.
    """
    if _is_reddit_host(url):
        reddit_ua = (os.environ.get("REDDIT_USER_AGENT") or "").strip()
        if reddit_ua:
            return reddit_ua
        global _reddit_ua_warned
        if not _reddit_ua_warned:
            log.warning(
                "rss reddit fallback UA in use — Reddit throttles unidentified "
                "UAs hard. Set REDDIT_USER_AGENT in .env to "
                "'product-monitor:0.1 (by /u/<your_reddit_handle>)' "
                "to dramatically reduce 429s.",
            )
            _reddit_ua_warned = True
        return "product-monitor:rss:0.1 (unauthenticated fallback)"
    return _USER_AGENT


# Reddit's RSS entry HTML has the shape:
#   <table><tr>
#     <td><a href=post><img alt=title></a></td>          <-- title/thumbnail block
#     <td>
#       <!-- SC_OFF --><div class="md">POST BODY</div><!-- SC_ON -->
#       submitted by <a href="/u/user">/u/user</a><br />
#       <span><a>[link]</a></span> <span><a>[comments]</a></span>
#     </td>
#   </tr></table>
#
# The actual post body is inside <div class="md">...</div>. Everything outside
# is repeated title, thumbnail, or trailer noise. So we extract just the md
# div when present; if the entry is a link post with no body, the md div is
# empty and we fall back to the empty string (link posts have no body signal).
_REDDIT_MD_BODY_RE = re.compile(
    r'<div class="md">(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)


# Shorthand for Reddit-RSS-as-a-feed. Users can type `r/Windows11` in the
# feed_url field instead of the full RSS URL — we expand it here. Valid sorts
# from Reddit's listing endpoints: new, hot, top, rising, controversial. If no
# sort is given, we default to 'new' (matches the fallback path's goal:
# monitor recent activity, not what's trending). Accepts leading '/' and
# case variations of 'r'.
_REDDIT_SHORTHAND_RE = re.compile(
    r"^/?r/([A-Za-z0-9_]{2,21})(?:/(new|hot|top|rising|controversial))?/?$",
    re.IGNORECASE,
)


def expand_reddit_shorthand(feed_url: str) -> str:
    """Expand `r/SUBREDDIT` (with optional `/sort`) to the full RSS URL.
    Returns the input unchanged if it doesn't match the shorthand pattern
    (so full https URLs pass through untouched)."""
    if not feed_url:
        return feed_url
    m = _REDDIT_SHORTHAND_RE.match(feed_url.strip())
    if not m:
        return feed_url
    subreddit = m.group(1)
    sort = (m.group(2) or "new").lower()
    return f"https://www.reddit.com/r/{subreddit}/{sort}.rss"


def _reddit_clean_body(html_body: str) -> str:
    """Extract the actual post body from Reddit's RSS entry HTML, dropping
    the surrounding <table> title/thumbnail wrapper and the trailing
    submitted-by / [link] / [comments] boilerplate."""
    if not html_body:
        return html_body
    m = _REDDIT_MD_BODY_RE.search(html_body)
    if m:
        return m.group(1)
    # Malformed / different structure — fall through to generic strip.
    return html_body


def _strip_html(s: Optional[str]) -> str:
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return html.unescape(s)


def _entry_datetime(entry: Any) -> Optional[datetime]:
    """Prefer published_parsed; fall back to updated_parsed."""
    parsed = (
        getattr(entry, "published_parsed", None)
        or getattr(entry, "updated_parsed", None)
    )
    if not parsed:
        return None
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)


def _entry_external_id(entry: Any, feed_url: str) -> str:
    """Prefer feed-provided id (Atom), fall back to link, fall back to a
    synthesized deterministic id so the same entry isn't re-emitted."""
    return (
        getattr(entry, "id", None)
        or getattr(entry, "link", None)
        or f"{feed_url}#{getattr(entry, 'title', '?')}"
    )


def _entry_body(entry: Any, feed_url: str = "") -> str:
    """Extract the entry body. RSS calls it description; Atom calls it summary
    or content. Take the longest available, strip HTML.

    When feed_url is Reddit, first strip Reddit's RSS boilerplate wrapper
    (repeated title/link table + submitted-by trailer) so the classifier
    sees actual post text.
    """
    candidates: list[str] = []
    for attr in ("summary", "description"):
        v = getattr(entry, attr, None)
        if v:
            candidates.append(v)
    content = getattr(entry, "content", None)
    if content:
        for c in content:
            if isinstance(c, dict) and c.get("value"):
                candidates.append(c["value"])
    if not candidates:
        return ""
    body = max(candidates, key=len)
    if _is_reddit_host(feed_url):
        body = _reddit_clean_body(body)
    return _strip_html(body)


def _entry_author(entry: Any, fallback: str) -> str:
    """Prefer <author> or <dc:creator>; fall back to the feed's stream name."""
    author = getattr(entry, "author", None)
    if isinstance(author, str) and author.strip():
        return author.strip()
    # feedparser sometimes normalizes to `authors` (list of {name})
    authors = getattr(entry, "authors", None)
    if authors:
        first = authors[0] if isinstance(authors, list) and authors else None
        if isinstance(first, dict) and first.get("name"):
            return str(first["name"])
    return fallback


class RssSource(Source):
    name = "rss"

    def __init__(self) -> None:
        # Client without a fixed User-Agent — we set it per-request based on
        # the feed URL so Reddit gets an identified UA that respects its
        # fair-use conventions.
        self._client = httpx.Client(
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    def _fetch_feed_bytes(self, feed_url: str) -> tuple[Optional[bytes], Optional[str]]:
        """Fetch a feed. Returns (bytes, health_signal) where health_signal
        is None on success and a short string ('dead'/'rate-limited'/'http')
        on failure. Caller surfaces the signal in FetchStats.ceiling_hits."""
        try:
            resp = _retry_http.request_with_retry(
                lambda: self._client.get(
                    feed_url, headers={"User-Agent": _ua_for_url(feed_url)},
                ),
                source_id="rss",
            )
        except httpx.HTTPError as e:
            log.warning("rss network error feed=%s error=%s", feed_url, e)
            return None, "network"
        if resp.status_code in (404, 410):
            log.warning("rss feed dead status=%d feed=%s", resp.status_code, feed_url)
            return None, "dead"
        if resp.status_code == 429:
            # Reddit specifically rate-limits identical IP+UA. Report loudly
            # so the operator knows to slow down (fewer streams, add sleep,
            # or configure REDDIT_USER_AGENT).
            retry_after = resp.headers.get("retry-after", "")
            log.warning(
                "rss feed rate-limited (429) feed=%s retry_after=%s",
                feed_url, retry_after or "unset",
            )
            return None, "rate-limited"
        if resp.status_code >= 400:
            log.warning("rss feed HTTP error status=%d feed=%s", resp.status_code, feed_url)
            return None, "http"
        return resp.content, None

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        feed_url = (config.get("feed_url") or "").strip()
        if not feed_url:
            raise ValueError("rss stream config missing required 'feed_url'")
        stream_name = config.get("name") or feed_url
        display_name = (config.get("display") or stream_name).strip()

        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        # Optional per-stream throttle. Useful when several streams in a row
        # target the same rate-limited host (Reddit especially). Sleep BEFORE
        # the fetch so the throttle isn't skipped if the fetch fails fast.
        sleep_before = float(config.get("sleep_before_fetch_seconds", 0.0))
        if sleep_before > 0:
            import time as _time
            _time.sleep(sleep_before)

        raw_bytes, health = self._fetch_feed_bytes(feed_url)
        if raw_bytes is None:
            # Surface the health signal via ceiling_hits (repurposing the
            # completeness channel — see sources/base.py). Downstream can
            # treat it as a "stream health" warning. Distinguish rate-limit
            # from dead so the operator knows whether to retry or fix config.
            stats.ceiling_hits.append((f"rss:{stream_name}:{health or 'unknown'}", 0.0))
            return

        feed = feedparser.parse(raw_bytes)

        # feedparser's bozo flag == 1 means "we parsed it but XML wasn't
        # strictly valid". Most bozo flags are harmless (missing DTD, weird
        # encoding declaration). We continue but note it.
        if getattr(feed, "bozo", 0) and not feed.entries:
            log.warning(
                "rss feed unparseable feed=%s err=%s",
                feed_url, getattr(feed, "bozo_exception", "unknown"),
            )
            stats.ceiling_hits.append((f"rss:{stream_name}:unparseable", 0.0))
            return

        entries = feed.entries or []
        if not entries:
            log.info("rss feed empty feed=%s", feed_url)
            return

        for entry in entries:
            dt = _entry_datetime(entry)
            if dt is None:
                # Skip entries with no usable timestamp — they'll dedup on
                # future runs but never advance the cursor.
                continue
            ts = dt.timestamp()
            if ts <= floor:
                continue  # already seen; next entry might still be newer

            body = _entry_body(entry, feed_url)
            title = getattr(entry, "title", None) or ""
            link = getattr(entry, "link", None) or feed_url

            if ts > newest_seen:
                newest_seen = ts

            yield RawItem(
                source="rss",
                source_display_name=display_name,
                external_id=_entry_external_id(entry, feed_url),
                url=link,
                parent_external_id=None,
                author=_entry_author(entry, display_name),
                created_at=dt,
                title=title,
                body=body,
                engagement={},
                raw={
                    "feed_url": feed_url,
                    "feed_title": getattr(feed.feed, "title", None) if getattr(feed, "feed", None) else None,
                    "categories": [t.term for t in getattr(entry, "tags", []) if hasattr(t, "term")],
                },
            )

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
