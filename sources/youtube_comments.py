"""YouTube Comments connector — search-first flow (SOURCE_YOUTUBE_COMMENTS.md).

Auth: YOUTUBE_API_KEY in .env (Google Cloud, YouTube Data API v3 enabled).

Flow: per stream, per run —
  1. `search.list` per query in `search_queries` (100 quota units per call).
     Returns up to 50 video ids.
  2. `videos.list` batch-enriches all discovered videos with view_count,
     description snippet, channel title (1 unit per 50 ids).
  3. For each kept video:
       `commentThreads.list` for top-level comments + inline replies
         (1 unit per 100-comment page).
       `comments.list?parentId=` for the tail of any reply thread with >5
         replies (1 unit per 100-reply page).
  4. Cursor = max `publishedAt` of discovered videos.

Quota model (rough):
  search: N_queries * 100
  videos: ~1 unit (single batch)
  threads: sum_over_videos( ceil(comments/100) )
  replies: sum_over_hot_threads( ceil(extra_replies/100) )
  ~150-500 units per stream per run for typical topics.

Config shape:
  streams:
    - name: windows-audio
      search_queries:
        - "windows 11 audio problems"
        - "bluetooth headphones windows"
      max_videos_per_query: 25         # cap on search results kept
      max_comments_per_video: 200      # safety cap on hot threads
      max_replies_per_thread: 100      # cap on reply hydration
      search_order: relevance          # relevance | date
      comment_order: relevance         # relevance | time
      min_video_views: 1000            # skip small videos
      published_within_days: 90        # only videos < N days old

Signal notes: the classifier's `parent_context` gets video title + description
snippet so it can tell "this is broken" apart from "this is broken but great."
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import httpx

from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="youtube_comments",
    display_name="YouTube Comments",
    version="0.1.0",
    docs_url="https://developers.google.com/youtube/v3",
    help=(
        "YouTube Data API v3, search-first flow. Each stream runs one or "
        "more keyword searches, then fetches comments (and full replies) "
        "on the returned videos. Quota-heavy — one search = 100 units, "
        "one comment page = 1 unit. Requires YOUTUBE_API_KEY in .env."
    ),
    connection_fields=[
        FieldSpec(name="YOUTUBE_API_KEY", label="API key", type="secret",
                  help="Google Cloud API key with YouTube Data API v3 enabled. Restrict the key to YouTube Data API v3 for hygiene."),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="windows-audio-search", help="Internal label for cursor / dedup."),
        FieldSpec(name="search_queries", label="Search queries (one per line)", type="textarea_list", required=True,
                  placeholder="windows 11 audio problems\nbluetooth headphones windows\nrealtek driver",
                  help="One search per line. Each burns 100 units of your daily YouTube quota."),
        FieldSpec(name="max_videos_per_query", label="Max videos per query", type="number", default=25,
                  help="Cap on videos discovered per query. YouTube search returns up to 50 per call; lower cap saves comment-fetch quota."),
        FieldSpec(name="max_comments_per_video", label="Max comments per video", type="number", default=200,
                  help="Safety cap on hot threads (flagship reviews can have 50K+ comments). Higher = more signal but more quota."),
        FieldSpec(name="max_replies_per_thread", label="Max replies per thread", type="number", default=100,
                  help="commentThreads inlines 5 replies for free; this caps how many more we fetch via comments.list (1 unit per page)."),
        FieldSpec(name="search_order", label="Search order", type="text", default="relevance",
                  help="relevance | date. 'relevance' surfaces higher-quality videos; 'date' gets the newest."),
        FieldSpec(name="comment_order", label="Comment order", type="text", default="relevance",
                  help="relevance | time. 'relevance' surfaces highest-quality comments (YouTube's own ranking)."),
        FieldSpec(name="min_video_views", label="Min video views", type="number", default=1000,
                  help="Skip videos below this view count. Filters out obscure/low-engagement content."),
        FieldSpec(name="published_within_days", label="Only videos from last N days", type="number", default=90,
                  help="0 = no filter. Recommended: 90-180 for recency; longer wastes quota on stale videos."),
    ],
    identifier_field="search_queries",
    source_category="custom_source",
    content_types=["user_feedback"],
)

log = logging.getLogger(__name__)

_BASE_URL = "https://www.googleapis.com/youtube/v3"
_USER_AGENT = "product-monitor/0.1"
_SEARCH_PAGE_SIZE = 50   # YouTube max
_THREAD_PAGE_SIZE = 100  # YouTube max
_REPLY_PAGE_SIZE = 100   # YouTube max


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """YouTube ISO-8601 with Z: '2026-07-08T14:22:31Z'. UTC-aware."""
    if not s:
        return None
    try:
        s = s.rstrip("Z")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _thread_to_item(
    thread: dict[str, Any],
    video: dict[str, Any],
    matched_query: str,
) -> Optional[RawItem]:
    """Map the top-level comment of a commentThreads entry to a RawItem."""
    top = ((thread.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
    comment_id = ((thread.get("snippet") or {}).get("topLevelComment") or {}).get("id") or thread.get("id")
    if not comment_id or not top.get("textOriginal"):
        return None
    dt = _parse_iso(top.get("publishedAt")) or datetime.now(timezone.utc)
    video_id = video["id"]
    v_snippet = video.get("snippet") or {}
    v_stats = video.get("statistics") or {}
    channel_title = v_snippet.get("channelTitle") or ""
    return RawItem(
        source="youtube_comments",
        source_display_name=f"YouTube ({channel_title})" if channel_title else "YouTube",
        external_id=comment_id,
        url=f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
        parent_external_id=video_id,
        author=top.get("authorDisplayName"),
        created_at=dt,
        title=None,
        body=top["textOriginal"],
        engagement={
            "likes": int(top.get("likeCount") or 0),
            "reply_count": int((thread.get("snippet") or {}).get("totalReplyCount") or 0),
            "video_views": int(v_stats.get("viewCount") or 0),
        },
        raw={
            "video_id": video_id,
            "video_title": v_snippet.get("title"),
            "channel_id": v_snippet.get("channelId"),
            "channel_title": channel_title,
            "matched_query": matched_query,
            "is_comment": True,
            "is_reply": False,
            "parent_context": {
                "title": v_snippet.get("title"),
                "snippet": (v_snippet.get("description") or "")[:500],
            },
        },
    )


def _reply_to_item(
    reply: dict[str, Any],
    video: dict[str, Any],
    parent_comment_id: str,
    matched_query: str,
) -> Optional[RawItem]:
    """Map an individual reply comment (from either inline thread.replies
    or a comments.list call) to a RawItem."""
    snippet = reply.get("snippet") or {}
    reply_id = reply.get("id")
    body = snippet.get("textOriginal")
    if not reply_id or not body:
        return None
    dt = _parse_iso(snippet.get("publishedAt")) or datetime.now(timezone.utc)
    video_id = video["id"]
    v_snippet = video.get("snippet") or {}
    channel_title = v_snippet.get("channelTitle") or ""
    return RawItem(
        source="youtube_comments",
        source_display_name=f"YouTube ({channel_title})" if channel_title else "YouTube",
        external_id=reply_id,
        # Replies still deep-link via `lc` — YT resolves the highlight
        url=f"https://www.youtube.com/watch?v={video_id}&lc={reply_id}",
        parent_external_id=parent_comment_id,
        author=snippet.get("authorDisplayName"),
        created_at=dt,
        title=None,
        body=body,
        engagement={
            "likes": int(snippet.get("likeCount") or 0),
        },
        raw={
            "video_id": video_id,
            "video_title": v_snippet.get("title"),
            "channel_id": v_snippet.get("channelId"),
            "channel_title": channel_title,
            "matched_query": matched_query,
            "is_comment": True,
            "is_reply": True,
            "parent_comment_id": parent_comment_id,
        },
    )


class YouTubeCommentsSource(Source):
    name = "youtube_comments"

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(30.0),
        )

    def _key(self) -> str:
        key = os.environ.get("YOUTUBE_API_KEY")
        if not key:
            raise RuntimeError(
                "YOUTUBE_API_KEY not set. Create at console.cloud.google.com "
                "(enable 'YouTube Data API v3', then Credentials -> API Key) "
                "and add YOUTUBE_API_KEY=... to .env."
            )
        return key

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "key": self._key()}
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise RuntimeError(f"youtube_comments network error: {e}") from e
        if resp.status_code == 403:
            # Quota exhausted or key restricted — raise loudly so the run
            # halts rather than silently returning nothing.
            body = resp.text[:500]
            raise RuntimeError(
                f"youtube_comments 403 (likely quotaExceeded or key restriction): {body}"
            )
        resp.raise_for_status()
        return resp.json()

    def _search_videos(
        self,
        query: str,
        max_results: int,
        order: str,
        published_after: Optional[str],
    ) -> list[str]:
        """One search.list call. Returns video ids. Costs 100 quota units."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": order,
            "maxResults": min(max_results, _SEARCH_PAGE_SIZE),
        }
        if published_after:
            params["publishedAfter"] = published_after
        payload = self._get("/search", params)
        return [
            item["id"]["videoId"]
            for item in (payload.get("items") or [])
            if (item.get("id") or {}).get("videoId")
        ]

    def _fetch_video_metadata(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """videos.list for statistics + snippet. Batches up to 50 ids per call."""
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            payload = self._get("/videos", {
                "part": "snippet,statistics",
                "id": ",".join(batch),
                "maxResults": 50,
            })
            for v in payload.get("items") or []:
                out[v["id"]] = v
        return out

    def _fetch_comment_threads(
        self,
        video_id: str,
        max_comments: int,
        order: str,
    ) -> Iterator[dict[str, Any]]:
        """Page through commentThreads. Yields thread objects; each contains
        the top-level comment and (up to 5) inline replies."""
        emitted = 0
        page_token: Optional[str] = None
        while emitted < max_comments:
            params: dict[str, Any] = {
                "part": "snippet,replies",
                "videoId": video_id,
                "order": order,
                "maxResults": min(_THREAD_PAGE_SIZE, max_comments - emitted),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = self._get("/commentThreads", params)
            except RuntimeError as e:
                # Common: 403 commentsDisabled on the video. Skip that video.
                if "commentsDisabled" in str(e) or "disabled" in str(e).lower():
                    log.info("youtube video %s has comments disabled", video_id)
                    return
                raise
            for thread in payload.get("items") or []:
                yield thread
                emitted += 1
                if emitted >= max_comments:
                    break
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

    def _fetch_extra_replies(
        self,
        parent_comment_id: str,
        max_replies: int,
    ) -> Iterator[dict[str, Any]]:
        """comments.list?parentId=... to get replies beyond the 5 inlined by
        commentThreads. Each page is 1 quota unit."""
        emitted = 0
        page_token: Optional[str] = None
        while emitted < max_replies:
            params: dict[str, Any] = {
                "part": "snippet",
                "parentId": parent_comment_id,
                "maxResults": min(_REPLY_PAGE_SIZE, max_replies - emitted),
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get("/comments", params)
            for reply in payload.get("items") or []:
                yield reply
                emitted += 1
                if emitted >= max_replies:
                    break
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        queries: list[str] = config.get("search_queries") or []
        if not queries:
            raise ValueError("youtube_comments stream config missing 'search_queries'")

        max_videos = int(config.get("max_videos_per_query", 25))
        max_comments = int(config.get("max_comments_per_video", 200))
        max_replies = int(config.get("max_replies_per_thread", 100))
        search_order = config.get("search_order", "relevance")
        comment_order = config.get("comment_order", "relevance")
        min_views = int(config.get("min_video_views", 0))
        within_days = int(config.get("published_within_days", 0))
        display_name = config.get("name") or "youtube-search"

        # Fail fast if the key is missing — otherwise every search silently
        # errors and the run produces zero items with only a log entry.
        self._key()

        floor: float = float(cursor.cursor_ts or 0)

        # Compute publishedAfter for the search: whichever of {cursor,
        # within_days} is more recent. Both are optional; using the newer
        # bound prevents deep historical dredging on first run.
        candidates: list[float] = []
        if floor > 0:
            candidates.append(floor)
        if within_days > 0:
            days_ago = (datetime.now(timezone.utc) - timedelta(days=within_days)).timestamp()
            candidates.append(days_ago)
        published_after_iso: Optional[str] = None
        if candidates:
            published_after_iso = datetime.fromtimestamp(
                max(candidates), tz=timezone.utc,
            ).isoformat().replace("+00:00", "Z")

        # 1) Search — one call per query. Collect a de-duped video-id set.
        video_ids: list[str] = []
        video_id_to_query: dict[str, str] = {}
        for q in queries:
            try:
                ids = self._search_videos(q, max_videos, search_order, published_after_iso)
            except RuntimeError as e:
                log.warning("youtube search failed q=%r: %s", q, e)
                stats.ceiling_hits.append(
                    (f"youtube:{display_name}:search-failed", 0.0)
                )
                continue
            for vid in ids:
                if vid not in video_id_to_query:
                    video_id_to_query[vid] = q
                    video_ids.append(vid)

        if not video_ids:
            log.info("youtube search returned no videos for stream=%s", display_name)
            return

        # 2) Enrich videos with metadata (views, description, channel).
        videos_meta = self._fetch_video_metadata(video_ids)

        # 3) For each video: comment threads + extra replies.
        newest_seen = floor
        for vid in video_ids:
            video = videos_meta.get(vid)
            if not video:
                continue
            v_stats = video.get("statistics") or {}
            if min_views > 0 and int(v_stats.get("viewCount") or 0) < min_views:
                continue

            # Track newest video publishedAt for cursor advancement.
            v_published = _parse_iso((video.get("snippet") or {}).get("publishedAt"))
            if v_published is not None:
                ts = v_published.timestamp()
                if ts > newest_seen:
                    newest_seen = ts

            matched_q = video_id_to_query.get(vid, "")
            for thread in self._fetch_comment_threads(vid, max_comments, comment_order):
                item = _thread_to_item(thread, video, matched_q)
                if item is None:
                    continue
                yield item

                # Inline replies (up to 5) come for free with commentThreads.
                inline_replies = (thread.get("replies") or {}).get("comments") or []
                for r in inline_replies:
                    r_item = _reply_to_item(r, video, item.external_id, matched_q)
                    if r_item is not None:
                        yield r_item

                # If the thread has more replies than the 5 inlined AND the
                # user asked for full replies, hydrate the rest.
                total_reply_count = int(
                    (thread.get("snippet") or {}).get("totalReplyCount") or 0
                )
                if max_replies > 0 and total_reply_count > len(inline_replies):
                    remaining = min(max_replies, total_reply_count - len(inline_replies))
                    inline_ids = {r.get("id") for r in inline_replies}
                    fetched = 0
                    for extra in self._fetch_extra_replies(item.external_id, remaining + len(inline_replies)):
                        if extra.get("id") in inline_ids:
                            continue  # already yielded via inline
                        r_item = _reply_to_item(extra, video, item.external_id, matched_q)
                        if r_item is not None:
                            yield r_item
                            fetched += 1
                            if fetched >= remaining:
                                break

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
