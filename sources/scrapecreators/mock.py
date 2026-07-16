"""Fixture-based mock mode for ScrapeCreators (POST_V1_PLAN §4.9).

Diverts every `ScrapeCreatorsClient.get()` to a fixture file read from
disk instead of hitting the API. Enables:

  - Plugin author iteration without burning free-tier credits.
  - Deterministic integration tests — the emit shape from each platform
    plugin is verified against known fixtures.
  - CI runs without an SC key configured.

Activation: set `SCRAPECREATORS_MOCK=1` in the env. The client picks
this up in `from_env()`.

Fixture layout:

  tests/fixtures/scrapecreators/
    reddit_subreddit__windows11.json     # /v1/reddit/subreddit?subreddit=Windows11
    reddit_post_comments__abc123.json    # /v1/reddit/post/comments?url=...&id=abc123
    twitter_user_tweets__msftwindows.json
    twitter_tweet__1234567890.json
    tiktok_profile_videos__someuser.json
    tiktok_video_comments__9876543.json

Fixture selection rules:
  path             → strip leading /v1 or /v3, lowercase, replace / with _
  params           → append `__<identifier>` derived from the first
                     identifying parameter (subreddit, username, url, id).

Missing fixture → returns an empty payload matching the endpoint's shape
so plugins gracefully yield zero items rather than crashing (better test
UX than a hard error).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "scrapecreators"

# Fields in `params` that most-strongly identify a call. First match wins.
_IDENTIFIER_PARAMS = ("subreddit", "handle", "username", "user", "id", "url", "query")

# Fallback empty payloads by path prefix. Return shapes match live API — a
# list under `posts`/`tweets`/`videos`/`comments` etc. — so downstream
# plugin code doesn't KeyError when running against a missing fixture.
_EMPTY_SHAPES: dict[str, dict[str, Any]] = {
    "reddit/subreddit":      {"posts": []},
    "reddit/post/comments":  {"comments": []},
    "reddit/search":         {"posts": []},
    "twitter/user-tweets":   {"tweets": []},
    "twitter/tweet":         {"tweet": None, "replies": []},
    "tiktok/profile/videos": {"videos": []},
    "tiktok/video/comments": {"comments": []},
}


def get_fixture(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Read a fixture JSON for the given API call, or return an empty payload.

    Path examples:
      /v1/reddit/subreddit
      /v1/reddit/post/comments
      /v3/tiktok/profile/videos
    """
    fname = _fixture_name(path, params)
    fpath = _FIXTURES_DIR / fname
    if fpath.exists():
        try:
            return json.loads(fpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.warning("scrapecreators mock: fixture %s is malformed: %s", fname, e)
    return _empty_for(path)


def _fixture_name(path: str, params: dict[str, Any]) -> str:
    """Derive `<endpoint>__<identifier>.json` from an API call."""
    stem = re.sub(r"^v\d+/", "", path.strip("/"))
    stem = stem.lower().replace("/", "_").replace("-", "_")

    identifier = ""
    for key in _IDENTIFIER_PARAMS:
        val = params.get(key)
        if val:
            identifier = _slug(str(val))
            break

    if identifier:
        return f"{stem}__{identifier}.json"
    return f"{stem}.json"


def _slug(text: str) -> str:
    """Filesystem-safe slug for a URL / handle / etc."""
    # Strip scheme + host if a URL was passed
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text[:60]  # cap length


def _empty_for(path: str) -> dict[str, Any]:
    stem = re.sub(r"^v\d+/", "", path.strip("/"))
    return _EMPTY_SHAPES.get(stem, {})
