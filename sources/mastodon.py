"""Mastodon hashtag timeline connector (keyless public API).

Uses /api/v1/timelines/tag/{tag} on a configured instance. Does NOT use
/api/v2/search (usually requires auth).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Iterator

import httpx

from pipeline import http as _retry_http
from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

log = logging.getLogger(__name__)

_USER_AGENT = "product-monitor/0.1"
_TAG_RE = re.compile(r"<[^>]+>")


MANIFEST = SourceManifest(
    plugin_id="mastodon",
    display_name="Mastodon",
    version="0.1.0",
    docs_url="https://docs.joinmastodon.org/api/timelines/",
    help=(
        "Public hashtag timelines on any Mastodon instance "
        "(e.g. mastodon.social). No API key — uses "
        "/api/v1/timelines/tag/{tag}. Does not search full-text; "
        "configure hashtags your audience actually uses."
    ),
    connection_fields=[],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="mastodon-cursor", help="Internal label for cursor / dedup."),
        FieldSpec(name="instance", label="Instance host", type="text", required=True,
                  default="mastodon.social",
                  placeholder="mastodon.social",
                  help="Hostname only — no https:// prefix."),
        FieldSpec(name="tags", label="Hashtags (comma list)", type="csv", required=True,
                  placeholder="cursor,cursorIDE",
                  help="Tags without # — one timeline fetch per tag."),
        FieldSpec(name="limit", label="Posts per tag", type="number", default=40,
                  help="Max statuses per tag per run (API max 40)."),
    ],
    identifier_field="tags",
    source_category="custom_source",
    content_types=["user_feedback"],
)


def _normalize_host(host: str) -> str:
    h = (host or "mastodon.social").strip().lower()
    h = re.sub(r"^https?://", "", h)
    return h.rstrip("/")


def _parse_iso(s: str) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _status_to_item(status: dict[str, Any], instance: str, tag: str) -> RawItem:
    acct = (status.get("account") or {}).get("acct") or "unknown"
    created = _parse_iso(status.get("created_at") or "")
    body = _strip_html(status.get("content") or "")
    url = status.get("url") or f"https://{instance}/@{acct}/{status.get('id')}"
    return RawItem(
        source="mastodon",
        source_display_name=f"Mastodon ({instance})",
        external_id=f"{instance}:{status['id']}",
        url=url,
        parent_external_id=None,
        author=acct,
        created_at=created,
        title=None,
        body=body,
        content_type="user_feedback",
        engagement={
            "favourites": int(status.get("favourites_count") or 0),
            "reblogs": int(status.get("reblogs_count") or 0),
            "replies": int(status.get("replies_count") or 0),
        },
        raw={"instance": instance, "tag": tag, "language": status.get("language")},
    )


class MastodonSource(Source):
    name = "mastodon"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(30.0),
        )

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        instance = _normalize_host(str(config.get("instance") or "mastodon.social"))
        tags = config.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
        tags = [t.lstrip("#") for t in tags if t]
        if not tags:
            raise ValueError("mastodon stream config missing required 'tags'")

        limit = min(40, max(1, int(config.get("limit") or 40)))
        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        for tag in tags:
            url = f"https://{instance}/api/v1/timelines/tag/{tag}"
            resp = _retry_http.request_with_retry(
                lambda u=url: self._client.get(u, params={"limit": limit}),
                source_id="mastodon",
            )
            resp.raise_for_status()
            for status in resp.json() or []:
                ts = _parse_iso(status.get("created_at") or "").timestamp()
                if ts <= floor:
                    continue
                item = _status_to_item(status, instance, tag)
                if ts > newest_seen:
                    newest_seen = ts
                yield item

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
