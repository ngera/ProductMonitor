"""Product Hunt connector via the public v2 GraphQL API.

Auth: bearer token in PRODUCTHUNT_TOKEN. Create a personal token at
    https://api.producthunt.com/v2/oauth/applications
(register an app, click 'Create Token' in the app's page). Tokens don't
expire for developer/personal apps.

Rate limit: 900 complexity points / 15 min. Each Post query costs ~5-10 pts;
each Comments-per-post nested query adds more. In practice a run of 20-50
posts with comments is well under budget.

Stream config:

    streams:
      - name: technology-launches
        topic_slug: technology         # optional; empty = across all topics
        max_posts: 50                  # cap per run
        fetch_comments: true           # emit comments as child items
        max_comments_per_post: 50      # safety cap on hot threads

Cursor is the epoch-second `createdAt` of the newest post seen. Comments
inherit their parent's cursor treatment (dedup catches replays).

Signal notes:
  - Launch-day posts have high vote_count + heavy first-24h comments — good
    for gauging initial reception.
  - Comments are the actionable feedback (bugs, feature asks, comparisons);
    posts themselves are marketing copy.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="producthunt",
    display_name="Product Hunt",
    version="0.1.0",
    docs_url="https://api.producthunt.com/v2/docs",
    help=(
        "GraphQL v2. Requires PRODUCTHUNT_TOKEN in .env — get one at "
        "api.producthunt.com/v2/oauth/applications (Create Token). "
        "Streams are topic-filtered. Comments are the substantive "
        "feedback; the post body is mostly launch marketing copy."
    ),
    connection_fields=[
        FieldSpec(name="PRODUCTHUNT_TOKEN", label="Bearer token", type="secret",
                  help="Personal developer token from api.producthunt.com/v2/oauth/applications. Required."),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="technology-launches", help="Internal label for cursor / dedup."),
        FieldSpec(name="topic_slug", label="Topic slug", type="text", default="",
                  placeholder="artificial-intelligence",
                  help="Topic slug from producthunt.com/topics/{slug}. Empty = across all topics (usually too broad)."),
        FieldSpec(name="max_posts", label="Max posts per run", type="number", default=50,
                  help="Cap to keep API complexity budget reasonable. Each post also fetches its comments if enabled."),
        FieldSpec(name="fetch_comments", label="Fetch comments", type="bool", default=True,
                  help="Emit each post's comments as child items. Comments are the substantive feedback."),
        FieldSpec(name="max_comments_per_post", label="Max comments per post", type="number", default=50,
                  help="Safety cap on hot threads. Older comments past the cap are skipped."),
    ],
    identifier_field="topic_slug",
    source_category="custom_source",
    content_types=["user_feedback", "media_coverage"],
)

log = logging.getLogger(__name__)

_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"
_USER_AGENT = "product-monitor/0.1"

# Fetch posts + nested comments in one round-trip. `postedAfter` is native
# to the API and lets us page from the cursor forward.
_POSTS_QUERY = """
query FeedbackMonitorPosts(
  $first: Int!, $topic: String, $postedAfter: DateTime, $after: String,
  $commentsFirst: Int!
) {
  posts(first: $first, topic: $topic, postedAfter: $postedAfter, after: $after, order: NEWEST) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        tagline
        description
        url
        website
        votesCount
        commentsCount
        createdAt
        user { name username }
        topics(first: 5) { edges { node { slug name } } }
        comments(first: $commentsFirst, order: NEWEST) {
          edges {
            node {
              id
              body
              votesCount
              createdAt
              user { name username }
            }
          }
        }
      }
    }
  }
}
"""


def _parse_ph_ts(raw: str) -> Optional[datetime]:
    """PH ISO-8601 with Z: '2026-07-08T14:22:31Z'. Return UTC-aware."""
    if not raw:
        return None
    try:
        s = raw.rstrip("Z")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _post_to_item(post: dict[str, Any]) -> Optional[RawItem]:
    """Map one Post node to a RawItem. External id is the PH post id."""
    post_id = post.get("id")
    if not post_id:
        return None
    user = post.get("user") or {}
    topic_slugs = [
        (edge.get("node") or {}).get("slug")
        for edge in ((post.get("topics") or {}).get("edges") or [])
    ]
    topic_slugs = [t for t in topic_slugs if t]

    body = (post.get("description") or "").strip()
    if not body:
        body = post.get("tagline") or ""

    created = _parse_ph_ts(post.get("createdAt") or "") or datetime.now(timezone.utc)

    return RawItem(
        source="producthunt",
        source_display_name="Product Hunt",
        external_id=str(post_id),
        url=f"https://www.producthunt.com/posts/{post.get('url', '').split('/')[-1] or post_id}"
            if not (post.get("url") or "").startswith("http")
            else post["url"],
        parent_external_id=None,
        author=user.get("name") or user.get("username"),
        created_at=created,
        title=post.get("name"),
        body=body,
        engagement={
            "votes_count": int(post.get("votesCount") or 0),
            "comments_count": int(post.get("commentsCount") or 0),
        },
        raw={
            "topics": topic_slugs,
            "website": post.get("website"),
            "tagline": post.get("tagline"),
            "is_comment": False,
        },
    )


def _comment_to_item(comment: dict[str, Any], parent_post: dict[str, Any]) -> Optional[RawItem]:
    comment_id = comment.get("id")
    if not comment_id:
        return None
    user = comment.get("user") or {}
    created = _parse_ph_ts(comment.get("createdAt") or "") or datetime.now(timezone.utc)
    parent_url = parent_post.get("url") or ""
    return RawItem(
        source="producthunt",
        source_display_name="Product Hunt",
        external_id=f"c{comment_id}",   # namespaced so it can't collide with post ids
        url=f"{parent_url}#comment-{comment_id}" if parent_url.startswith("http") else parent_url,
        parent_external_id=str(parent_post.get("id")),
        author=user.get("name") or user.get("username"),
        created_at=created,
        title=None,
        body=(comment.get("body") or "").strip(),
        engagement={
            "votes_count": int(comment.get("votesCount") or 0),
        },
        raw={
            "is_comment": True,
            "parent_context": {
                "title": parent_post.get("name"),
                "tagline": parent_post.get("tagline"),
            },
        },
    )


class ProductHuntSource(Source):
    name = "producthunt"

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": _USER_AGENT},
        )

    def _post_query(self, variables: dict[str, Any]) -> dict[str, Any]:
        token = os.environ.get("PRODUCTHUNT_TOKEN")
        if not token:
            raise RuntimeError(
                "PRODUCTHUNT_TOKEN not set. Create a token at "
                "https://api.producthunt.com/v2/oauth/applications "
                "and add it to .env."
            )
        resp = self._client.post(
            _ENDPOINT,
            headers={"Authorization": f"Bearer {token}"},
            json={"query": _POSTS_QUERY, "variables": variables},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            # PH returns partial data + errors. Log and continue with what we got.
            log.warning("producthunt errors: %s", payload["errors"])
        return payload.get("data") or {}

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        topic_slug = (config.get("topic_slug") or "").strip() or None
        max_posts = int(config.get("max_posts", 50))
        fetch_comments = bool(config.get("fetch_comments", True))
        max_comments = int(config.get("max_comments_per_post", 50)) if fetch_comments else 0
        page_size = min(20, max_posts)  # PH caps `first` at 20 per page
        display_name = config.get("name") or f"producthunt-{topic_slug or 'all'}"

        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor
        posted_after: Optional[str] = None
        if floor > 0:
            posted_after = datetime.fromtimestamp(floor, tz=timezone.utc).isoformat()

        after: Optional[str] = None
        emitted = 0

        while emitted < max_posts:
            variables: dict[str, Any] = {
                "first": min(page_size, max_posts - emitted),
                "commentsFirst": max_comments,
            }
            if topic_slug:
                variables["topic"] = topic_slug
            if posted_after:
                variables["postedAfter"] = posted_after
            if after:
                variables["after"] = after

            data = self._post_query(variables)
            posts_conn = data.get("posts") or {}
            edges = posts_conn.get("edges") or []
            if not edges:
                break

            for edge in edges:
                post = edge.get("node") or {}
                item = _post_to_item(post)
                if item is None:
                    continue

                ts = item.created_at.timestamp()
                if ts <= floor:
                    # Order is NEWEST — once we hit the cursor, the rest are older.
                    return
                if ts > newest_seen:
                    newest_seen = ts

                emitted += 1
                yield item

                if fetch_comments:
                    comment_edges = ((post.get("comments") or {}).get("edges")) or []
                    for c_edge in comment_edges:
                        c_item = _comment_to_item(c_edge.get("node") or {}, post)
                        if c_item is None:
                            continue
                        # Update newest_seen against comments too so cursor
                        # advances even if all top-of-window posts are older.
                        c_ts = c_item.created_at.timestamp()
                        if c_ts > newest_seen:
                            newest_seen = c_ts
                        yield c_item

                if emitted >= max_posts:
                    break

            page_info = posts_conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen
        log.info("producthunt topic=%s emitted=%d", topic_slug or "all", emitted)

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
