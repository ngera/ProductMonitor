"""Bluesky (AT Protocol) post search connector.

Default path: public AppView searchPosts (no key). Public search is
sometimes rate-limited or returns 403 — optional BLUESKY_HANDLE +
BLUESKY_APP_PASSWORD enable authenticated search as fallback.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
import httpx

from pipeline import http as _retry_http
from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

log = logging.getLogger(__name__)

_PUBLIC_SEARCH = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_AUTH_SEARCH = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_SESSION_URL = "https://bsky.social/xrpc/com.atproto.server.createSession"
_USER_AGENT = "product-monitor/0.1"


MANIFEST = SourceManifest(
    plugin_id="bluesky",
    display_name="Bluesky",
    version="0.1.0",
    docs_url="https://docs.bsky.app/",
    help=(
        "Search Bluesky posts mentioning your product via the public AppView. "
        "No key required for the happy path. Public search is occasionally "
        "403'd under load — set BLUESKY_HANDLE + BLUESKY_APP_PASSWORD for "
        "authenticated fallback. Unauthenticated pagination via cursor is "
        "often blocked; expect first-page results when keyless."
    ),
    connection_fields=[
        FieldSpec(name="BLUESKY_HANDLE", label="Handle (optional)", type="text",
                  help="e.g. you.bsky.social — only for authenticated fallback."),
        FieldSpec(name="BLUESKY_APP_PASSWORD", label="App password (optional)", type="secret",
                  help="Bluesky app password (not account password). For search fallback."),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="bluesky-cursor", help="Internal label for cursor / dedup."),
        FieldSpec(name="search_queries", label="Search queries (one per line)",
                  type="textarea_list", required=True,
                  placeholder="Cursor IDE\n@cursor.com",
                  help="Terms passed to app.bsky.feed.searchPosts."),
        FieldSpec(name="limit", label="Results per query", type="number", default=25,
                  help="Max posts per query per run (API default cap ~100)."),
    ],
    identifier_field="search_queries",
    source_category="custom_source",
    content_types=["user_feedback"],
)


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


def _record_to_item(post: dict[str, Any], query: str) -> Optional[RawItem]:
    record = post.get("record") or {}
    text = (record.get("text") or "").strip()
    if not text and not post.get("uri"):
        return None
    author = post.get("author") or {}
    handle = author.get("handle") or author.get("did") or "unknown"
    uri = post.get("uri") or ""
    rkey = uri.split("/")[-1] if uri else post.get("cid", "")[:16]
    created = _parse_iso(record.get("createdAt") or post.get("indexedAt") or "")
    url = f"https://bsky.app/profile/{handle}/post/{rkey}"

    return RawItem(
        source="bluesky",
        source_display_name="Bluesky",
        external_id=f"{handle}:{rkey}",
        url=url,
        parent_external_id=None,
        author=handle,
        created_at=created,
        title=None,
        body=text,
        content_type="user_feedback",
        engagement={
            "likes": int(post.get("likeCount") or 0),
            "replies": int(post.get("replyCount") or 0),
            "reposts": int(post.get("repostCount") or 0),
        },
        raw={"uri": uri, "query": query},
    )


class BlueskySource(Source):
    name = "bluesky"

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(30.0),
        )
        self._auth_token: Optional[str] = None

    def _ensure_session(self) -> None:
        if self._auth_token:
            return
        handle = (os.environ.get("BLUESKY_HANDLE") or "").strip()
        password = (os.environ.get("BLUESKY_APP_PASSWORD") or "").strip()
        if not handle or not password:
            return
        resp = _retry_http.request_with_retry(
            lambda: self._client.post(
                _SESSION_URL,
                json={"identifier": handle, "password": password},
            ),
            source_id="bluesky",
        )
        resp.raise_for_status()
        self._auth_token = resp.json().get("accessJwt")

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        params = {"q": query, "limit": min(limit, 100)}
        headers: dict[str, str] = {}
        url = _PUBLIC_SEARCH

        def _do(url_=url, hdrs=headers):
            return self._client.get(url_, params=params, headers=hdrs)

        try:
            resp = _retry_http.request_with_retry(lambda: _do(), source_id="bluesky")
            if resp.status_code == 403:
                raise httpx.HTTPStatusError(
                    "public search forbidden", request=resp.request, response=resp,
                )
            resp.raise_for_status()
            return resp.json().get("posts") or []
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 403:
                raise
            self._ensure_session()
            if not self._auth_token:
                raise RuntimeError(
                    "Bluesky public search returned 403. Set BLUESKY_HANDLE and "
                    "BLUESKY_APP_PASSWORD in .env for authenticated fallback."
                ) from e
            url = _AUTH_SEARCH
            headers = {"Authorization": f"Bearer {self._auth_token}"}
            resp = _retry_http.request_with_retry(
                lambda: self._client.get(url, params=params, headers=headers),
                source_id="bluesky",
            )
            resp.raise_for_status()
            return resp.json().get("posts") or []

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        queries = config.get("search_queries") or []
        if isinstance(queries, str):
            queries = [q.strip() for q in queries.split("\n") if q.strip()]
        if not queries:
            raise ValueError("bluesky stream config missing search_queries")

        limit = max(1, int(config.get("limit") or 25))
        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        for query in queries:
            posts = self._search(query, limit)
            for post in posts:
                item = _record_to_item(post, query)
                if item is None:
                    continue
                ts = item.created_at.timestamp()
                if ts <= floor:
                    continue
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
