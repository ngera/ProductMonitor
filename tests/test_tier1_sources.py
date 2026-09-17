"""Conformance + unit tests for Tier 1 source plugins (Discourse, Play,
GitHub Discussions, Bluesky, Mastodon)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest import mock

import httpx
import pytest

from sources.base import SourceCursor, FetchStats
from sources.testkit import FakeHTTPTransport


# ---------------------------------------------------------------------------
# Discourse
# ---------------------------------------------------------------------------

_DISCOURSE_SEARCH = {
    "topics": [{"id": 42}],
    "posts": [],
}

_DISCOURSE_TOPIC = {
    "id": 42,
    "slug": "hello-world",
    "title": "Hello",
    "views": 100,
    "posts_count": 2,
    "like_count": 5,
    "post_stream": {
        "posts": [
            {
                "id": 1,
                "post_number": 1,
                "username": "alice",
                "created_at": "2026-09-10T12:00:00.000Z",
                "cooked": "<p>First post</p>",
            },
            {
                "id": 2,
                "post_number": 2,
                "username": "bob",
                "created_at": "2026-09-10T13:00:00.000Z",
                "cooked": "<p>Reply</p>",
            },
        ],
    },
}


def test_discourse_search_and_hydrate():
    from sources.discourse import DiscourseSource

    src = DiscourseSource()
    transport = FakeHTTPTransport()
    transport.register(
        "https://forum.example.com/search.json",
        json=_DISCOURSE_SEARCH,
    )
    transport.register(
        "https://forum.example.com/t/42.json",
        json=_DISCOURSE_TOPIC,
    )
    src._client = httpx.Client(transport=transport)

    cur = SourceCursor(cursor_ts=None)
    items = list(src.fetch_since(cur, {
        "host": "forum.example.com",
        "mode": "search",
        "query": "cursor",
        "include_replies": True,
        "max_pages": 1,
    }, FetchStats()))

    assert len(items) == 2
    assert items[0].external_id == "forum.example.com:topic:42:post:1"
    assert items[1].parent_external_id == "forum.example.com:topic:42"
    assert cur.cursor_ts is not None


# ---------------------------------------------------------------------------
# Google Play
# ---------------------------------------------------------------------------

def test_google_play_maps_review():
    from sources import google_play as gp

    review = {
        "reviewId": "rev1",
        "comments": [{
            "userComment": {
                "text": "Great app",
                "starRating": 5,
                "lastModified": {"seconds": "1726500000"},
            },
        }],
    }
    item = gp._review_to_item(review, "com.example.app")
    assert item is not None
    assert item.body == "Great app"
    assert item.engagement["rating"] == 5


def test_google_play_fetch_with_mock_auth():
    from sources.google_play import GooglePlaySource

    fake_creds = SimpleNamespace(token="tok")
    api_response = {
        "reviews": [{
            "reviewId": "r1",
            "comments": [{
                "userComment": {
                    "text": "Buggy",
                    "starRating": 2,
                    "lastModified": {"seconds": str(int(datetime.now(timezone.utc).timestamp()))},
                },
            }],
        }],
    }
    transport = FakeHTTPTransport()
    transport.register(
        "https://androidpublisher.googleapis.com/",
        json=api_response,
    )
    with mock.patch.object(GooglePlaySource, "__init__", lambda self: None):
        src = GooglePlaySource()
        src._creds = fake_creds
        src._client = httpx.Client(transport=transport)
        cur = SourceCursor(cursor_ts=None)
        items = list(src.fetch_since(cur, {"package_name": "com.test.app"}, FetchStats()))
    assert len(items) == 1
    assert items[0].source == "google_play"


# ---------------------------------------------------------------------------
# GitHub Discussions
# ---------------------------------------------------------------------------

def test_github_discussions_graphql_fetch():
    from sources.github_discussions import GitHubDiscussionsSource

    discussion_node = {
        "number": 7,
        "title": "Feature idea",
        "body": "Please add X",
        "url": "https://github.com/o/r/discussions/7",
        "createdAt": "2026-09-10T10:00:00Z",
        "updatedAt": "2026-09-10T10:00:00Z",
        "comments": {"totalCount": 0},
        "author": {"login": "dev1"},
        "category": {"name": "Ideas"},
    }

    def _fake_graphql(client, query, variables):
        return {
            "repository": {
                "discussions": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [discussion_node],
                },
            },
        }

    with mock.patch("sources.github_discussions.graphql_request", _fake_graphql):
        with mock.patch("sources.github_discussions.new_graphql_client") as nc:
            nc.return_value = httpx.Client()
            src = GitHubDiscussionsSource()
            cur = SourceCursor(cursor_ts=None)
            items = list(src.fetch_since(cur, {"repos": ["o/r"]}, FetchStats()))
    assert len(items) == 1
    assert "discussion-7" in items[0].external_id


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

def test_bluesky_search_maps_posts():
    from sources.bluesky import BlueskySource

    posts = [{
        "uri": "at://did:plc:abc/app.bsky.feed.post/xyz123",
        "record": {"text": "Love Cursor", "createdAt": "2026-09-10T10:00:00.000Z"},
        "author": {"handle": "user.bsky.social"},
        "likeCount": 3,
        "replyCount": 1,
        "repostCount": 0,
    }]
    transport = FakeHTTPTransport()
    transport.register(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
        json={"posts": posts},
    )
    src = BlueskySource()
    src._client = httpx.Client(transport=transport)
    cur = SourceCursor(cursor_ts=None)
    items = list(src.fetch_since(cur, {"search_queries": ["Cursor"]}, FetchStats()))
    assert len(items) == 1
    assert "Cursor" in items[0].body or items[0].body == "Love Cursor"


# ---------------------------------------------------------------------------
# Mastodon
# ---------------------------------------------------------------------------

def test_mastodon_tag_timeline():
    from sources.mastodon import MastodonSource

    statuses = [{
        "id": "99",
        "content": "<p>Hello #cursor</p>",
        "created_at": "2026-09-10T10:00:00.000Z",
        "url": "https://mastodon.social/@u/99",
        "account": {"acct": "u"},
        "favourites_count": 2,
        "reblogs_count": 1,
        "replies_count": 0,
    }]
    transport = FakeHTTPTransport()
    transport.register(
        "https://mastodon.social/api/v1/timelines/tag/cursor",
        json=statuses,
    )
    src = MastodonSource()
    src._client = httpx.Client(transport=transport)
    cur = SourceCursor(cursor_ts=None)
    items = list(src.fetch_since(cur, {
        "instance": "mastodon.social",
        "tags": ["cursor"],
    }, FetchStats()))
    assert len(items) == 1
    assert items[0].source == "mastodon"


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------

def test_registry_discovers_tier1_plugins(monkeypatch):
    from sources import registry

    registry.reset_registry()
    monkeypatch.delenv("TRUST_PLUGINS_DIR", raising=False)
    reg = registry.discover(trust_plugins_dir=False)
    for pid in ("discourse", "google_play", "github_discussions", "bluesky", "mastodon"):
        assert reg.get(pid) is not None, f"missing plugin {pid}"
