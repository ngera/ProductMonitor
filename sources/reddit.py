"""Reddit connector (DESIGN.md §4.3).

- Triangulate .new + .top + .controversial to mitigate the 1000-item ceiling.
- Fetch full comment trees for relevant posts, with a per-post safety cap.
- Capture parent context on comments so the classifier can read bare replies.
- Every RawItem.url is a direct deep link.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from pipeline.models import RawItem
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="reddit",
    display_name="Reddit",
    version="0.1.0",
    docs_url="https://www.reddit.com/prefs/apps",
    help=(
        "Subreddit-based ingest via PRAW. Needs Reddit non-commercial API "
        "approval and REDDIT_CLIENT_ID/SECRET in .env. Each stream is one "
        "subreddit."
    ),
    connection_fields=[
        FieldSpec(name="REDDIT_CLIENT_ID", label="Client ID", type="text", required=True,
                  help="The short string under the app name (under 'personal use script') on the prefs/apps page."),
        FieldSpec(name="REDDIT_CLIENT_SECRET", label="Client Secret", type="secret", required=True,
                  help="The 'secret' field on the app registration. Treated as a credential."),
        FieldSpec(name="REDDIT_USER_AGENT", label="User Agent", type="text",
                  default="customer-feedback-monitor:0.1 (by /u/yourname)",
                  help="Reddit-mandated format: <platform>:<app-id>:<version> (by /u/<username>). Non-conforming UAs are rate-limited or blocked."),
    ],
    stream_fields=[
        FieldSpec(name="subreddit", label="Subreddit", type="text", required=True,
                  placeholder="Windows11", help="Subreddit name, no r/ prefix."),
        FieldSpec(name="display", label="Display label", type="text",
                  placeholder="r/Windows11",
                  help="Human-readable name shown in reports. Defaults to r/<subreddit>."),
        FieldSpec(name="engagement_threshold", label="Engagement threshold", type="number", default=5,
                  help="Minimum upvotes+comments needed for an item to survive the heuristic filter. Lower = more items + more noise."),
    ],
    identifier_field="subreddit",
    supports_bulk_add=True,
)

try:
    import praw  # type: ignore
except ImportError:  # praw optional at import time; required to actually fetch
    praw = None


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


class RedditSource(Source):
    name = "reddit"

    def __init__(self) -> None:
        if praw is None:
            raise RuntimeError("praw is not installed; cannot fetch from Reddit.")
        self._reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            user_agent=os.environ.get(
                "REDDIT_USER_AGENT", "customer-feedback-monitor/0.1"
            ),
            check_for_async=False,
        )
        self._reddit.read_only = True

    # --- public API ---------------------------------------------------------

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        """`config` is one stream block from sources.yaml plus app fetching opts."""
        subreddit_name = config["subreddit"]
        display = config.get("display", f"r/{subreddit_name}")
        floor = cursor.cursor_ts or 0.0

        new_limit = config.get("new_limit", 1000)
        top_limit = config.get("top_limit", 100)
        controversial_limit = config.get("controversial_limit", 50)
        max_comments = config.get("max_comments_per_post", 500)
        parent_chars = config.get("parent_context_body_chars", 500)
        fetch_comments = config.get("fetch_all_comments", True)

        sub = self._reddit.subreddit(subreddit_name)

        submissions: dict[str, Any] = {}
        oldest_new: Optional[float] = None
        new_count = 0

        for sub_post in sub.new(limit=new_limit):
            new_count += 1
            created = float(sub_post.created_utc)
            oldest_new = created if oldest_new is None else min(oldest_new, created)
            if created <= floor:
                continue
            submissions[sub_post.id] = sub_post

        # Ceiling-hit detection (§4.3 step 2).
        if new_count >= new_limit and oldest_new is not None and oldest_new > floor:
            stats.ceiling_hits.append((display, oldest_new - floor))

        if config.get("triangulate", True):
            for listing, limit in (
                (sub.top, top_limit),
                (sub.controversial, controversial_limit),
            ):
                for sub_post in listing(time_filter="week", limit=limit):
                    if float(sub_post.created_utc) > floor:
                        submissions.setdefault(sub_post.id, sub_post)

        newest_seen = floor
        for sub_post in submissions.values():
            created = float(sub_post.created_utc)
            newest_seen = max(newest_seen, created)
            yield self._post_to_item(sub_post, display)

            if fetch_comments:
                yield from self._comments_to_items(
                    sub_post, display, max_comments, parent_chars, stats
                )

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    # --- helpers ------------------------------------------------------------

    def _post_to_item(self, post: Any, display: str) -> RawItem:
        return RawItem(
            source="reddit",
            source_display_name=display,
            external_id=post.id,
            url=f"https://reddit.com{post.permalink}",
            parent_external_id=None,
            author=str(post.author) if post.author else None,
            created_at=_utc(float(post.created_utc)),
            title=post.title,
            body=post.selftext or "",
            engagement={
                "upvotes": int(post.score),
                "comment_count": int(post.num_comments),
                "upvote_ratio": float(getattr(post, "upvote_ratio", 0.0)),
            },
            raw={"kind": "submission", "id": post.id},
        )

    def _comments_to_items(
        self, post: Any, display: str, max_comments: int, parent_chars: int, stats: FetchStats
    ) -> Iterator[RawItem]:
        parent_context = {
            "title": post.title,
            "body": (post.selftext or "")[:parent_chars],
        }
        try:
            post.comments.replace_more(limit=None)
            all_comments = post.comments.list()
        except Exception:
            # Be resilient: a thread that errors on expansion shouldn't kill the run.
            post.comments.replace_more(limit=0)
            all_comments = post.comments.list()

        capped = False
        if len(all_comments) > max_comments:
            capped = True
            all_comments = sorted(
                all_comments, key=lambda c: int(getattr(c, "score", 0)), reverse=True
            )[:max_comments]
            stats.comment_cap_hits.append(
                (post.id, int(post.num_comments), max_comments)
            )

        for comment in all_comments:
            body = getattr(comment, "body", "") or ""
            if body in ("[deleted]", "[removed]"):
                continue
            yield RawItem(
                source="reddit",
                source_display_name=display,
                external_id=comment.id,
                url=f"https://reddit.com{comment.permalink}",
                parent_external_id=post.id,
                author=str(comment.author) if comment.author else None,
                created_at=_utc(float(comment.created_utc)),
                title=None,
                body=body,
                engagement={"upvotes": int(getattr(comment, "score", 0))},
                raw={"kind": "comment", "id": comment.id, "parent_context": parent_context},
            )
        _ = capped  # already logged via stats
