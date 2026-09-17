"""ScrapeCreators — Reddit plugin (POST_V1_PLAN §4.9).

Fetches subreddit posts + comments via SC's Reddit endpoints. Distinct
from `sources/reddit.py` (PRAW-based) — this exists so users without
Reddit's non-commercial API approval can still ingest Reddit data.

Endpoints:
  GET /v1/reddit/subreddit         Recent posts (paged)
  GET /v1/reddit/post/comments     Comment tree for one post

Config (stream_fields):
  subreddit                  (required) subreddit name, no r/ prefix
  display                    display label; default "r/<subreddit>"
  fetch_comments             bool; if true, fetches comments for each post
  max_posts_per_run          hard cap on posts pulled from /subreddit
  max_comments_per_post      cap for /post/comments walk

Credit accounting: 1 request = 1 credit. Comment fetch is 1 credit per
post regardless of comment count (SC returns the whole tree in one call).
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
    plugin_id="scrapecreators_reddit",
    display_name="Reddit (via ScrapeCreators)",
    version="0.1.0",
    docs_url="https://scrapecreators.com/",
    help=(
        "Reddit ingest via the ScrapeCreators API. Alternative to the direct "
        "PRAW connector for users who don't have Reddit non-commercial API "
        "approval. Shares one API key + credit budget with the SC X and "
        "TikTok plugins. Set SCRAPECREATORS_API_KEY on /connections and cap "
        "credits per run in config/app.yaml under "
        "fetching.scrapecreators_max_credits_per_run (default 200)."
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
        FieldSpec(name="subreddit", label="Subreddit", type="text", required=True,
                  placeholder="Windows11", help="Subreddit name, no r/ prefix."),
        FieldSpec(name="display", label="Display label", type="text",
                  placeholder="r/Windows11",
                  help="Human-readable name shown in reports. Defaults to r/<subreddit>."),
        FieldSpec(name="fetch_comments", label="Fetch comments", type="bool", default=True,
                  help="If unchecked, only posts are fetched — cheaper on credits."),
        FieldSpec(name="max_posts_per_run", label="Max posts per run", type="number", default=50,
                  help="Hard cap on posts pulled from /subreddit; 1 credit per page."),
        FieldSpec(name="max_comments_per_post", label="Max comments per post", type="number",
                  default=200,
                  help="Applied client-side after the whole tree is fetched."),
    ],
    identifier_field="subreddit",
    supports_bulk_add=True,
    source_category="third_party_scraper",
    content_types=["user_feedback"],
)


class ScrapeCreatorsRedditSource(Source):
    name = "scrapecreators_reddit"

    def __init__(self, client: Optional[ScrapeCreatorsClient] = None) -> None:
        self._client = client or get_shared_client()

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        subreddit = config["subreddit"]
        display = config.get("display", f"r/{subreddit}")
        fetch_comments = bool(config.get("fetch_comments", True))
        max_posts = int(config.get("max_posts_per_run", 50))
        max_comments = int(config.get("max_comments_per_post", 200))
        floor = cursor.cursor_ts or 0.0
        newest_seen = floor

        try:
            payload = self._client.get("/v1/reddit/subreddit", {"subreddit": subreddit})
        except ScrapeCreatorsHalted as e:
            stats.ceiling_hits.append((display, 0.0))
            _log_halt(stats, str(e))
            return

        posts = list(payload.get("posts") or [])[:max_posts]

        for post in posts:
            created = _epoch(post.get("created_utc") or post.get("created"))
            if created is None or created <= floor:
                continue
            newest_seen = max(newest_seen, created)
            yield _post_to_item(post, display)

            if not fetch_comments:
                continue
            post_id = post.get("id") or ""
            permalink = post.get("permalink") or ""
            if not post_id:
                continue
            try:
                cpayload = self._client.get(
                    "/v1/reddit/post/comments",
                    {"id": post_id, "url": f"https://reddit.com{permalink}"},
                )
            except ScrapeCreatorsHalted as e:
                _log_halt(stats, str(e))
                break

            comments = list(cpayload.get("comments") or [])
            if len(comments) > max_comments:
                stats.comment_cap_hits.append((post_id, len(comments), max_comments))
                comments = sorted(
                    comments, key=lambda c: int(c.get("score") or 0), reverse=True
                )[:max_comments]
            parent_context = {
                "title": post.get("title") or "",
                "body": (post.get("selftext") or "")[:500],
            }
            for c in comments:
                item = _comment_to_item(c, post_id, permalink, display, parent_context)
                if item is not None:
                    yield item

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen


# ---------------------------------------------------------------------------
# Payload → RawItem
# ---------------------------------------------------------------------------


def _post_to_item(post: dict[str, Any], display: str) -> RawItem:
    permalink = post.get("permalink") or ""
    return RawItem(
        source="scrapecreators_reddit",
        source_display_name=display,
        external_id=str(post.get("id") or ""),
        url=f"https://reddit.com{permalink}" if permalink else str(post.get("url") or ""),
        parent_external_id=None,
        author=post.get("author"),
        created_at=_dt(post.get("created_utc") or post.get("created")),
        title=post.get("title"),
        body=post.get("selftext") or "",
        content_type="user_feedback",
        engagement={
            "upvotes": int(post.get("score") or post.get("ups") or 0),
            "comment_count": int(post.get("num_comments") or 0),
            "upvote_ratio": float(post.get("upvote_ratio") or 0.0),
        },
        raw={"kind": "submission", "id": post.get("id"), "provider": "scrapecreators"},
    )


def _comment_to_item(
    c: dict[str, Any],
    post_id: str,
    post_permalink: str,
    display: str,
    parent_context: dict[str, str],
) -> Optional[RawItem]:
    body = c.get("body") or ""
    if body in ("[deleted]", "[removed]") or not body:
        return None
    cid = str(c.get("id") or "")
    if not cid:
        return None
    return RawItem(
        source="scrapecreators_reddit",
        source_display_name=display,
        external_id=cid,
        url=f"https://reddit.com{post_permalink}{cid}/" if post_permalink else f"https://reddit.com/comments/{post_id}/_/{cid}/",
        parent_external_id=post_id,
        author=c.get("author"),
        created_at=_dt(c.get("created_utc") or c.get("created")),
        title=None,
        body=body,
        content_type="user_feedback",
        engagement={"upvotes": int(c.get("score") or 0)},
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


def _log_halt(stats: FetchStats, reason: str) -> None:
    # Repurpose ceiling_hits for the "halted mid-fetch" signal; the run
    # detail health card treats this as an incompleteness marker.
    stats.ceiling_hits.append(("scrapecreators_halt", 0.0))
