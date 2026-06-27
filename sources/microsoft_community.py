"""Microsoft Tech Community + Microsoft Q&A RSS connector
(SOURCE_MICROSOFT_TECH_COMMUNITY.md).

No auth. Stream config:

    streams:
      - name: tech-community-windows
        feed_url: https://techcommunity.microsoft.com/category/windows/rss
      - name: qa-windows-11
        feed_url: https://learn.microsoft.com/answers/tags/windows-11/feed

One feed per stream keeps cursor handling clean (one feed = one stream
name = one cursor row).
"""

from __future__ import annotations

import html
import re
import time as _time
from calendar import timegm
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import feedparser

from pipeline.models import RawItem
from sources.base import FetchStats, Source, SourceCursor

_USER_AGENT = "customer-feedback-monitor/0.1"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Heuristic: an author string with "Microsoft" or "MVP" or "MSFT" is treated
# as an official voice. False positives possible; the classifier treats it as
# a hint, not ground truth.
_OFFICIAL_RE = re.compile(r"\b(microsoft|MSFT|MVP)\b", re.IGNORECASE)


def _strip_html(s: Optional[str]) -> str:
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return html.unescape(s)


def _entry_datetime(entry: Any) -> Optional[datetime]:
    """Prefer published_parsed; fall back to updated_parsed."""
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    # feedparser returns a time.struct_time in UTC.
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)


def _entry_external_id(entry: Any, feed_url: str) -> str:
    return getattr(entry, "id", None) or getattr(entry, "link", None) or f"{feed_url}#{getattr(entry, 'title', '?')}"


def _entry_body(entry: Any) -> str:
    # Prefer content[].value (richer), fall back to summary/description.
    content_list = getattr(entry, "content", None)
    if content_list:
        try:
            return _strip_html(content_list[0].value)
        except Exception:
            pass
    return _strip_html(getattr(entry, "summary", None) or getattr(entry, "description", None))


def _entry_author(entry: Any) -> Optional[str]:
    return getattr(entry, "author", None) or getattr(entry, "creator", None)


class MicrosoftCommunitySource(Source):
    name = "microsoft_community"

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        feed_url: Optional[str] = config.get("feed_url")
        if not feed_url:
            raise ValueError("microsoft_community stream requires a feed_url")
        stream_name: str = config.get("name") or feed_url
        display_name: str = config.get("display") or stream_name

        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        # feedparser pulls the feed itself; pass a UA via request_headers.
        feed = feedparser.parse(feed_url, agent=_USER_AGENT)
        if getattr(feed, "bozo", 0) and not feed.entries:
            # Hard parse failure with no entries — surface but don't crash the run.
            stats.ceiling_hits.append(
                (f"microsoft_community:{stream_name}:bozo", 0.0)
            )
            return

        for entry in feed.entries:
            dt = _entry_datetime(entry)
            if not dt:
                continue
            ts = dt.timestamp()
            if ts <= floor:
                continue
            body = _entry_body(entry)
            author = _entry_author(entry)
            title = getattr(entry, "title", None)
            url = getattr(entry, "link", None) or _entry_external_id(entry, feed_url)
            item = RawItem(
                source="microsoft_community",
                source_display_name=display_name,
                external_id=_entry_external_id(entry, feed_url),
                url=url,
                parent_external_id=None,
                author=author,
                created_at=dt,
                title=title,
                body=body,
                engagement={},  # RSS doesn't expose likes/replies counts
                raw={
                    "feed_url": feed_url,
                    "is_official_voice": bool(_OFFICIAL_RE.search(author or "")),
                },
            )
            yield item
            if ts > newest_seen:
                newest_seen = ts

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

        # Politeness: small delay so a run with several MS feeds doesn't hammer.
        _time.sleep(float(config.get("sleep_after_feed_seconds", 0.5)))
