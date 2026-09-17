"""GitHub Discussions connector — GraphQL repository.discussions.

Reuses GITHUB_TOKEN from the Issues plugin. Many projects moved feature
requests and Q&A to Discussions while keeping Issues for bugs.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from pipeline.models import RawItem
from sources._github_common import (
    graphql_request,
    new_graphql_client,
    split_repo,
)
from sources.base import FetchStats, FieldSpec, Source, SourceCursor, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="github_discussions",
    display_name="GitHub Discussions",
    version="0.1.0",
    docs_url="https://docs.github.com/en/graphql/reference/objects#discussion",
    help=(
        "GitHub GraphQL repository.discussions. Uses the same GITHUB_TOKEN "
        "as GitHub Issues (fine-grained PAT, public repos read-only). "
        "Each stream is a set of owner/repo pairs."
    ),
    connection_fields=[
        FieldSpec(name="GITHUB_TOKEN", label="Personal Access Token (PAT)", type="secret",
                  required=True,
                  help="Same token as GitHub Issues. Fine-grained PAT, public-repos read."),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="myproject-discussions", help="Internal label for cursor / dedup."),
        FieldSpec(name="repos", label="Repos (one per line, owner/repo)", type="textarea_list",
                  required=True,
                  placeholder="getcursor/cursor\nmicrosoft/vscode",
                  help="Each line is one repo with Discussions enabled."),
        FieldSpec(name="fetch_comments", label="Fetch comments", type="bool", default=True,
                  help="Fetch top-level comments on each discussion."),
        FieldSpec(name="max_comments_per_discussion", label="Max comments per discussion",
                  type="number", default=50,
                  help="Safety cap on hot threads."),
    ],
    identifier_field="repos",
    source_category="custom_source",
    content_types=["user_feedback"],
)

_DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body url createdAt updatedAt
        comments { totalCount }
        author { login }
        category { name }
      }
    }
  }
}
"""

_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      comments(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          body url createdAt
          author { login }
        }
      }
    }
  }
}
"""


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _discussion_to_item(d: dict[str, Any], repo: str) -> RawItem:
    owner, repo_name = split_repo(repo)
    created = _parse_iso(d["createdAt"])
    author = (d.get("author") or {}).get("login")
    return RawItem(
        source="github_discussions",
        source_display_name=f"github:{owner}/{repo_name} discussions",
        external_id=f"{owner}/{repo_name}#discussion-{d['number']}",
        url=d["url"],
        parent_external_id=None,
        author=author,
        created_at=created,
        title=d.get("title") or "",
        body=d.get("body") or "",
        content_type="user_feedback",
        engagement={
            "comments": (d.get("comments") or {}).get("totalCount", 0),
            "category": (d.get("category") or {}).get("name"),
        },
        raw={"repo": repo, "number": d["number"], "updated_at": d.get("updatedAt")},
    )


def _comment_to_item(c: dict[str, Any], discussion: dict[str, Any], repo: str) -> RawItem:
    owner, repo_name = split_repo(repo)
    created = _parse_iso(c["createdAt"])
    author = (c.get("author") or {}).get("login")
    parent_id = f"{owner}/{repo_name}#discussion-{discussion['number']}"
    return RawItem(
        source="github_discussions",
        source_display_name=f"github:{owner}/{repo_name} discussions",
        external_id=f"{owner}/{repo_name}#discussion-{discussion['number']}#comment-{c['url'].split('/')[-1]}",
        url=c["url"],
        parent_external_id=parent_id,
        author=author,
        created_at=created,
        title=None,
        body=c.get("body") or "",
        content_type="user_feedback",
        engagement={},
        raw={
            "repo": repo,
            "parent_context": {
                "title": discussion.get("title") or "",
                "body": (discussion.get("body") or "")[:500],
            },
        },
    )


class GitHubDiscussionsSource(Source):
    name = "github_discussions"

    def __init__(self) -> None:
        self._client = new_graphql_client()

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        repos: list[str] = config.get("repos") or []
        if not repos:
            return
        fetch_comments = bool(config.get("fetch_comments", True))
        max_comments = int(config.get("max_comments_per_discussion", 50))
        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor
        sleep_between = float(config.get("sleep_between_repos_seconds", 0.5))

        for idx, repo in enumerate(repos):
            if idx and sleep_between:
                time.sleep(sleep_between)
            owner, name = split_repo(repo)
            g_cursor: Optional[str] = None
            while True:
                data = graphql_request(
                    self._client, _DISCUSSIONS_QUERY,
                    {"owner": owner, "name": name, "cursor": g_cursor},
                )
                repo_data = (data.get("repository") or {})
                conn = repo_data.get("discussions") or {}
                nodes = conn.get("nodes") or []
                if not nodes:
                    break

                stop_repo = False
                for disc in nodes:
                    updated_ts = _parse_iso(disc["updatedAt"]).timestamp()
                    if updated_ts <= floor:
                        stop_repo = True
                        break
                    yield _discussion_to_item(disc, repo)
                    if updated_ts > newest_seen:
                        newest_seen = updated_ts

                    if fetch_comments and (disc.get("comments") or {}).get("totalCount", 0) > 0:
                        yielded = 0
                        c_cursor: Optional[str] = None
                        while yielded < max_comments:
                            cdata = graphql_request(
                                self._client, _COMMENTS_QUERY,
                                {"owner": owner, "name": name,
                                 "number": disc["number"], "cursor": c_cursor},
                            )
                            comments_conn = (
                                (cdata.get("repository") or {})
                                .get("discussion") or {}
                            ).get("comments") or {}
                            for comment in comments_conn.get("nodes") or []:
                                if yielded >= max_comments:
                                    break
                                yielded += 1
                                yield _comment_to_item(comment, disc, repo)
                            if not comments_conn.get("pageInfo", {}).get("hasNextPage"):
                                break
                            c_cursor = comments_conn.get("pageInfo", {}).get("endCursor")

                if stop_repo:
                    break
                page = conn.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                g_cursor = page.get("endCursor")

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
