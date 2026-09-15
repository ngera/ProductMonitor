"""Hacker News connector via the Algolia HN search index (SOURCE_HN.md).

No auth required. Stream config:

    streams:
      - name: hn-windows
        search_queries: ["windows 11", "KB5036980"]
        include_tags: [story]            # ["story", "comment"] also supported
        max_pages_per_query: 5
        hits_per_page: 100

Cursor is the epoch-second `created_at_i` of the newest item seen across
all queries in the stream. We advance to the max so dedup via seen_ids
catches the small overlap on the next run.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote_plus

import httpx

from pipeline import http as _retry_http
from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

# ============================================================================
# Plugin manifest (POST_V1_PLAN §4.1, ADR-0001)
# ============================================================================
# Canonical reference implementation for plugin authors — copy this shape
# in your own plugin. Metadata that used to live in webui/app.py's
# CONNECTION_META + SOURCE_TYPE_META dicts now lives here, next to the
# runtime code that consumes it.

MANIFEST = SourceManifest(
    plugin_id="hn",
    display_name="Hacker News",
    version="0.1.0",
    docs_url="https://hn.algolia.com/api",
    help=(
        "Algolia-backed HN search. No auth. Each stream is a list of "
        "search queries the connector iterates."
    ),
    connection_fields=[],  # HN has no auth
    stream_fields=[
        FieldSpec(
            name="name", label="Stream name", type="text", required=True,
            placeholder="hn-windows",
            help="Internal label for cursor / dedup; doesn't have to be unique across products.",
        ),
        FieldSpec(
            name="search_queries", label="Search queries (one per line)",
            type="textarea_list", required=True,
            placeholder="windows 11\nKB5036980\nmicrosoft copilot",
            help="One Lucene-style query per line. Each is paginated independently.",
        ),
        FieldSpec(
            name="include_tags", label="Include tags (comma list)",
            type="csv", required=False, default="story",
            help="story | comment | story,comment. story-only avoids comment-without-parent-context noise.",
        ),
        FieldSpec(
            name="max_pages_per_query", label="Max pages per query",
            type="number", required=False, default=5,
            help="Algolia caps at 1000 results per query; a page is `hits_per_page` items.",
        ),
        FieldSpec(
            name="hits_per_page", label="Hits per page",
            type="number", required=False, default=100,
            help="1-200. 100 is the recommended sweet spot.",
        ),
    ],
    identifier_field="search_queries",
    credibility_weight_default=1.0,
    supports_bulk_add=False,   # search_queries is already textarea_list; no bulk expansion needed
    source_category="custom_source",
    content_types=["user_feedback", "media_coverage"],
)


_BASE_URL = "https://hn.algolia.com/api/v1"
_USER_AGENT = "product-monitor/0.1"
_DEFAULT_HITS_PER_PAGE = 100
_DEFAULT_MAX_PAGES = 5
_ALGOLIA_CEILING = 1000  # nbHits cap per Algolia query


def _hit_to_item(hit: dict[str, Any]) -> RawItem:
    """Map one Algolia HN hit to a RawItem.

    Stories: have title, points, num_comments, story_text (for Show/Ask HN).
    Comments: have comment_text, parent_id, story_id; no title/points.
    """
    obj_id = str(hit["objectID"])
    is_comment = "comment" in (hit.get("_tags") or [])
    body = hit.get("story_text") or hit.get("comment_text") or ""
    return RawItem(
        source="hn",
        source_display_name="Hacker News",
        external_id=obj_id,
        url=f"https://news.ycombinator.com/item?id={obj_id}",
        parent_external_id=str(hit["parent_id"]) if hit.get("parent_id") else None,
        author=hit.get("author"),
        created_at=datetime.fromtimestamp(int(hit["created_at_i"]), tz=timezone.utc),
        title=hit.get("title"),  # None for comments
        body=body,
        engagement={
            "points": hit.get("points"),
            "comment_count": hit.get("num_comments"),
        },
        raw={
            "tags": hit.get("_tags") or [],
            "story_id": hit.get("story_id"),
            "is_comment": is_comment,
        },
    )


class HackerNewsSource(Source):
    name = "hn"

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(20.0),
        )

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        queries: list[str] = config.get("search_queries") or [""]
        include_tags: list[str] = config.get("include_tags") or ["story"]
        max_pages: int = int(config.get("max_pages_per_query", _DEFAULT_MAX_PAGES))
        hits_per_page: int = int(config.get("hits_per_page", _DEFAULT_HITS_PER_PAGE))
        sleep_between_pages = float(config.get("sleep_between_pages_seconds", 0.0))

        floor: float = float(cursor.cursor_ts or 0)
        # Algolia accepts tag expressions like "story,author_xyz" (AND) or "(story,comment)" (OR).
        tags_param = ",".join(include_tags) if len(include_tags) == 1 else f"({','.join(include_tags)})"

        newest_seen = floor
        display_name = config.get("name") or "hn-default"

        for query in queries:
            params_base = {
                "tags": tags_param,
                "hitsPerPage": hits_per_page,
            }
            if query:
                params_base["query"] = query
            # Algolia's numericFilters expects a strict greater-than for incremental fetch.
            if floor > 0:
                params_base["numericFilters"] = f"created_at_i>{int(floor)}"

            for page in range(max_pages):
                params = {**params_base, "page": page}
                resp = _retry_http.request_with_retry(
                    lambda p=params: self._client.get("/search_by_date", params=p),
                    source_id="hn",
                )
                resp.raise_for_status()
                payload = resp.json()
                hits = payload.get("hits") or []
                nb_hits = int(payload.get("nbHits", 0))

                if not hits:
                    break

                for hit in hits:
                    item = _hit_to_item(hit)
                    created = item.created_at.timestamp()
                    if created > newest_seen:
                        newest_seen = created
                    yield item

                # Ceiling-hit detection: Algolia caps result count at 1000 per query.
                # If we hit it AND there's still data above our floor, surface it.
                if nb_hits >= _ALGOLIA_CEILING:
                    oldest_on_page = min(int(h["created_at_i"]) for h in hits)
                    if oldest_on_page > floor:
                        stats.ceiling_hits.append(
                            (f"hn:{display_name}:{query or '*'}",
                             float(oldest_on_page - floor))
                        )

                if len(hits) < hits_per_page:
                    break  # last page
                if sleep_between_pages:
                    time.sleep(sleep_between_pages)

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
