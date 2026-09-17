"""ScrapeCreators — X (Twitter) plugin (POST_V1_PLAN §4.9).

Fetches recent tweets from a target handle + optional replies per tweet.

Endpoints:
  GET /v1/twitter/user-tweets   Recent tweets for a handle
  GET /v1/twitter/tweet         One tweet + its replies

Config (stream_fields):
  handle              (required) X handle, no leading @
  display             display label; default "@<handle>"
  fetch_replies       bool; if true, one credit per tweet for replies
  max_tweets_per_run  hard cap on tweets pulled per stream
  max_replies_per_tweet cap for the /tweet reply walk

Credit accounting: 1 request = 1 credit. Reply fetch: 1 credit per tweet
regardless of reply count.
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
    plugin_id="scrapecreators_x",
    display_name="X / Twitter (via ScrapeCreators)",
    version="0.1.0",
    docs_url="https://scrapecreators.com/",
    help=(
        "X (Twitter) ingest via the ScrapeCreators API. One SCRAPECREATORS_API_KEY "
        "shared with the SC Reddit and TikTok plugins. Per-run credit cap in "
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
        FieldSpec(name="handle", label="X handle", type="text", required=True,
                  placeholder="MSFTWindows", help="No leading @."),
        FieldSpec(name="display", label="Display label", type="text",
                  placeholder="@MSFTWindows",
                  help="Human-readable name shown in reports. Defaults to @<handle>."),
        FieldSpec(name="fetch_replies", label="Fetch replies", type="bool", default=True,
                  help="If unchecked, only tweets — no reply fetch, cheaper on credits."),
        FieldSpec(name="max_tweets_per_run", label="Max tweets per run", type="number",
                  default=40,
                  help="Hard cap on tweets pulled per handle. 1 credit per page."),
        FieldSpec(name="max_replies_per_tweet", label="Max replies per tweet", type="number",
                  default=100),
    ],
    identifier_field="handle",
    supports_bulk_add=True,
    source_category="third_party_scraper",
    content_types=["user_feedback"],
)


class ScrapeCreatorsXSource(Source):
    name = "scrapecreators_x"

    def __init__(self, client: Optional[ScrapeCreatorsClient] = None) -> None:
        self._client = client or get_shared_client()

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        handle = config["handle"].lstrip("@")
        display = config.get("display", f"@{handle}")
        fetch_replies = bool(config.get("fetch_replies", True))
        max_tweets = int(config.get("max_tweets_per_run", 40))
        max_replies = int(config.get("max_replies_per_tweet", 100))
        floor = cursor.cursor_ts or 0.0
        newest_seen = floor

        try:
            payload = self._client.get("/v1/twitter/user-tweets", {"handle": handle})
        except ScrapeCreatorsHalted:
            stats.ceiling_hits.append((display, 0.0))
            return

        tweets = list(payload.get("tweets") or [])[:max_tweets]

        for tweet in tweets:
            created = _epoch(tweet.get("created_at_epoch") or tweet.get("created_at"))
            if created is None or created <= floor:
                continue
            newest_seen = max(newest_seen, created)
            yield _tweet_to_item(tweet, handle, display)

            if not fetch_replies:
                continue
            tid = str(tweet.get("id") or tweet.get("id_str") or "")
            if not tid:
                continue
            try:
                cpayload = self._client.get("/v1/twitter/tweet", {"id": tid})
            except ScrapeCreatorsHalted:
                break

            replies = list(cpayload.get("replies") or [])
            if len(replies) > max_replies:
                stats.comment_cap_hits.append((tid, len(replies), max_replies))
                replies = replies[:max_replies]
            parent_context = {
                "title": None,
                "body": (tweet.get("text") or "")[:500],
            }
            for r in replies:
                item = _reply_to_item(r, tid, handle, display, parent_context)
                if item is not None:
                    yield item

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen


# ---------------------------------------------------------------------------
# Payload → RawItem
# ---------------------------------------------------------------------------


def _tweet_to_item(t: dict[str, Any], handle: str, display: str) -> RawItem:
    tid = str(t.get("id") or t.get("id_str") or "")
    author = t.get("author") or t.get("username") or handle
    return RawItem(
        source="scrapecreators_x",
        source_display_name=display,
        external_id=tid,
        url=f"https://x.com/{author}/status/{tid}",
        parent_external_id=None,
        author=str(author),
        created_at=_dt(t.get("created_at_epoch") or t.get("created_at")),
        title=None,
        body=t.get("text") or "",
        content_type="user_feedback",
        engagement={
            "likes":    int(t.get("favorite_count") or t.get("likes") or 0),
            "retweets": int(t.get("retweet_count") or t.get("retweets") or 0),
            "replies":  int(t.get("reply_count") or t.get("replies") or 0),
        },
        raw={"kind": "tweet", "id": tid, "provider": "scrapecreators"},
    )


def _reply_to_item(
    r: dict[str, Any],
    parent_tid: str,
    parent_handle: str,
    display: str,
    parent_context: dict[str, Optional[str]],
) -> Optional[RawItem]:
    body = r.get("text") or ""
    if not body:
        return None
    rid = str(r.get("id") or r.get("id_str") or "")
    if not rid:
        return None
    author = r.get("author") or r.get("username") or "unknown"
    return RawItem(
        source="scrapecreators_x",
        source_display_name=display,
        external_id=rid,
        url=f"https://x.com/{author}/status/{rid}",
        parent_external_id=parent_tid,
        author=str(author),
        created_at=_dt(r.get("created_at_epoch") or r.get("created_at")),
        title=None,
        body=body,
        content_type="user_feedback",
        engagement={
            "likes":    int(r.get("favorite_count") or r.get("likes") or 0),
            "retweets": int(r.get("retweet_count") or r.get("retweets") or 0),
        },
        raw={
            "kind": "reply",
            "id": rid,
            "provider": "scrapecreators",
            "parent_context": parent_context,
            "parent_handle": parent_handle,
        },
    )


def _epoch(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            pass
        # ISO8601 fallback: "2024-06-01T12:34:56Z"
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _dt(v: Any) -> datetime:
    epoch = _epoch(v)
    if epoch is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
