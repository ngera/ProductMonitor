"""ScrapeCreators — TikTok plugin (POST_V1_PLAN §4.9).

Fetches recent videos from a creator + optional comments per video.

Endpoints:
  GET /v3/tiktok/profile/videos  Recent videos for a profile
  GET /v1/tiktok/video/comments  Comments for one video

Config (stream_fields):
  username             (required) TikTok username, no leading @
  display              display label; default "@<username>"
  fetch_comments       bool
  max_videos_per_run   hard cap on videos pulled per stream
  max_comments_per_video

Credit accounting: 1 request = 1 credit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest
from sources.scrapecreators.client import (
    ScrapeCreatorsClient,
    ScrapeCreatorsHalted,
    get_shared_client,
)

MANIFEST = SourceManifest(
    plugin_id="scrapecreators_tiktok",
    display_name="TikTok (via ScrapeCreators)",
    version="0.1.0",
    docs_url="https://scrapecreators.com/",
    help=(
        "TikTok ingest via the ScrapeCreators API. One SCRAPECREATORS_API_KEY "
        "shared with the SC Reddit and X plugins. Per-run credit cap in "
        "config/app.yaml under fetching.scrapecreators_max_credits_per_run "
        "(default 200). 402 = hard halt for this source."
    ),
    connection_fields=[
        FieldSpec(
            name="SCRAPECREATORS_API_KEY",
            label="ScrapeCreators API Key",
            type="secret",
            required=True,
            help="One key covers Reddit, X, and TikTok SC plugins. Get it at scrapecreators.com.",
        ),
    ],
    stream_fields=[
        FieldSpec(name="username", label="TikTok username", type="text", required=True,
                  placeholder="microsoft", help="No leading @."),
        FieldSpec(name="display", label="Display label", type="text",
                  placeholder="@microsoft",
                  help="Human-readable name shown in reports. Defaults to @<username>."),
        FieldSpec(name="fetch_comments", label="Fetch comments", type="bool", default=True,
                  help="If unchecked, only videos — no comment fetch, cheaper on credits."),
        FieldSpec(name="max_videos_per_run", label="Max videos per run", type="number",
                  default=30),
        FieldSpec(name="max_comments_per_video", label="Max comments per video",
                  type="number", default=100),
    ],
    identifier_field="username",
    supports_bulk_add=True,
)


class ScrapeCreatorsTikTokSource(Source):
    name = "scrapecreators_tiktok"

    def __init__(self, client: Optional[ScrapeCreatorsClient] = None) -> None:
        self._client = client or get_shared_client()

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        username = config["username"].lstrip("@")
        display = config.get("display", f"@{username}")
        fetch_comments = bool(config.get("fetch_comments", True))
        max_videos = int(config.get("max_videos_per_run", 30))
        max_comments = int(config.get("max_comments_per_video", 100))
        floor = cursor.cursor_ts or 0.0
        newest_seen = floor

        try:
            payload = self._client.get(
                "/v3/tiktok/profile/videos", {"username": username}
            )
        except ScrapeCreatorsHalted:
            stats.ceiling_hits.append((display, 0.0))
            return

        videos = list(payload.get("videos") or [])[:max_videos]

        for video in videos:
            created = _epoch(video.get("create_time") or video.get("created_at"))
            if created is None or created <= floor:
                continue
            newest_seen = max(newest_seen, created)
            yield _video_to_item(video, username, display)

            if not fetch_comments:
                continue
            vid = str(video.get("id") or "")
            if not vid:
                continue
            try:
                cpayload = self._client.get(
                    "/v1/tiktok/video/comments", {"id": vid}
                )
            except ScrapeCreatorsHalted:
                break

            comments = list(cpayload.get("comments") or [])
            if len(comments) > max_comments:
                stats.comment_cap_hits.append((vid, len(comments), max_comments))
                comments = comments[:max_comments]
            parent_context = {
                "title": video.get("description") or "",
                "body": "",
            }
            for c in comments:
                item = _comment_to_item(c, vid, username, display, parent_context)
                if item is not None:
                    yield item

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen


# ---------------------------------------------------------------------------
# Payload → RawItem
# ---------------------------------------------------------------------------


def _video_to_item(v: dict[str, Any], username: str, display: str) -> RawItem:
    vid = str(v.get("id") or "")
    return RawItem(
        source="scrapecreators_tiktok",
        source_display_name=display,
        external_id=vid,
        url=v.get("url") or f"https://www.tiktok.com/@{username}/video/{vid}",
        parent_external_id=None,
        author=username,
        created_at=_dt(v.get("create_time") or v.get("created_at")),
        title=None,
        body=v.get("description") or "",
        engagement={
            "likes":    int(v.get("digg_count") or v.get("likes") or 0),
            "comments": int(v.get("comment_count") or 0),
            "shares":   int(v.get("share_count") or 0),
            "plays":    int(v.get("play_count") or 0),
        },
        raw={"kind": "video", "id": vid, "provider": "scrapecreators"},
    )


def _comment_to_item(
    c: dict[str, Any],
    parent_vid: str,
    parent_username: str,
    display: str,
    parent_context: dict[str, str],
) -> Optional[RawItem]:
    body = c.get("text") or ""
    if not body:
        return None
    cid = str(c.get("id") or c.get("cid") or "")
    if not cid:
        return None
    author = c.get("author") or c.get("username") or "unknown"
    return RawItem(
        source="scrapecreators_tiktok",
        source_display_name=display,
        external_id=cid,
        url=f"https://www.tiktok.com/@{parent_username}/video/{parent_vid}",
        parent_external_id=parent_vid,
        author=str(author),
        created_at=_dt(c.get("create_time") or c.get("created_at")),
        title=None,
        body=body,
        engagement={"likes": int(c.get("digg_count") or c.get("likes") or 0)},
        raw={
            "kind": "comment",
            "id": cid,
            "provider": "scrapecreators",
            "parent_context": parent_context,
        },
    )


def _epoch(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime:
    epoch = _epoch(v)
    if epoch is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
