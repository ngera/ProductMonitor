"""GitHub Issues connector (SOURCE_GITHUB_ISSUES.md).

Stream config:

    streams:
      - name: microsoft-dev-tools
        repos:
          - microsoft/PowerToys
          - microsoft/terminal
          - microsoft/WSL
        exclude_labels: [duplicate, wontfix]
        include_labels: []                # empty = all
        fetch_comments: true
        max_comments_per_issue: 50

Auth: GITHUB_TOKEN env var (fine-grained PAT, read-only on public repos).
Cursor: ISO 8601 timestamp stored as epoch seconds internally. Across a
stream with multiple repos we advance to the MAX updated_at seen and rely
on seen_ids to dedup the small overlap on the next run.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from pipeline import http as _retry_http

from sources.base import FieldSpec, SourceManifest

MANIFEST = SourceManifest(
    plugin_id="github_issues",
    display_name="GitHub Issues",
    version="0.1.0",
    docs_url="https://docs.github.com/en/rest/issues",
    help=(
        "GitHub REST /repos/{owner}/{repo}/issues. Needs a fine-grained "
        "PAT in .env as GITHUB_TOKEN (Public Repositories, read-only). "
        "Each stream is a set of repos."
    ),
    connection_fields=[
        FieldSpec(name="GITHUB_TOKEN", label="Personal Access Token (PAT)", type="secret",
                  required=True,
                  help="Fine-grained PAT, public-repos read access. Starts with 'github_pat_…'. Treated as a credential."),
    ],
    stream_fields=[
        FieldSpec(name="name", label="Stream name", type="text", required=True,
                  placeholder="microsoft-dev-tools", help="Internal label for cursor / dedup."),
        FieldSpec(name="repos", label="Repos (one per line, owner/repo)", type="textarea_list", required=True,
                  placeholder="microsoft/PowerToys\nmicrosoft/terminal\nmicrosoft/WSL",
                  help="Each line is one repo. Cursor advances on MAX(updated_at) across them; dedup catches the small overlap."),
        FieldSpec(name="include_labels", label="Include only labels (comma list)", type="csv", default="",
                  help="Empty = all issues. If set, only issues with at least one of these labels are kept."),
        FieldSpec(name="exclude_labels", label="Exclude labels (comma list)", type="csv", default="duplicate,wontfix",
                  help="Drop issues with any of these labels. Defaults exclude obvious noise."),
        FieldSpec(name="fetch_comments", label="Fetch comments", type="bool", default=True,
                  help="Fetch comments on each issue. Adds API calls but gives the classifier more context."),
        FieldSpec(name="max_comments_per_issue", label="Max comments per issue", type="number", default=50,
                  help="Safety cap on hot threads. Older comments past the cap are dropped."),
    ],
    identifier_field="repos",
    source_category="custom_source",
    content_types=["user_feedback"],
)

from pipeline.models import RawItem
from sources.base import FetchStats, Source, SourceCursor

_BASE_URL = "https://api.github.com"
_USER_AGENT = "product-monitor/0.1"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_DEFAULT_PER_PAGE = 100
_DEFAULT_MAX_COMMENTS = 50
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    # GitHub returns Z-suffixed UTC.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _next_url(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


def _issue_to_item(issue: dict[str, Any], repo: str) -> RawItem:
    owner, repo_name = repo.split("/", 1)
    created = _parse_iso(issue["created_at"])
    author = (issue.get("user") or {}).get("login")
    return RawItem(
        source="github_issues",
        source_display_name=f"github:{owner}/{repo_name}",
        external_id=f"{owner}/{repo_name}#{issue['number']}",
        url=issue["html_url"],
        parent_external_id=None,
        author=author,
        created_at=created,
        title=issue["title"],
        body=issue.get("body") or "",
        content_type="user_feedback",
        engagement={
            "comments": issue.get("comments", 0),
            "reactions": issue.get("reactions") or {},
            "state": issue.get("state"),
            "labels": [l["name"] for l in (issue.get("labels") or [])],
        },
        raw={
            "html_url": issue["html_url"],
            "node_id": issue.get("node_id"),
            "repo": repo,
            "number": issue["number"],
            "updated_at": issue.get("updated_at"),
        },
    )


def _comment_to_item(
    comment: dict[str, Any], issue: dict[str, Any], repo: str
) -> RawItem:
    owner, repo_name = repo.split("/", 1)
    created = _parse_iso(comment["created_at"])
    author = (comment.get("user") or {}).get("login")
    issue_ext_id = f"{owner}/{repo_name}#{issue['number']}"
    return RawItem(
        source="github_issues",
        source_display_name=f"github:{owner}/{repo_name}",
        external_id=f"{owner}/{repo_name}#{issue['number']}#comment-{comment['id']}",
        url=comment["html_url"],
        parent_external_id=issue_ext_id,
        author=author,
        created_at=created,
        title=None,
        body=comment.get("body") or "",
        content_type="user_feedback",
        engagement={
            "reactions": comment.get("reactions") or {},
        },
        raw={
            "html_url": comment["html_url"],
            "comment_id": comment["id"],
            "repo": repo,
            "issue_number": issue["number"],
            "parent_context": {
                "title": issue["title"],
                "body": (issue.get("body") or "")[:500],
                "labels": [l["name"] for l in (issue.get("labels") or [])],
                "state": issue.get("state"),
            },
        },
    )


class GitHubIssuesSource(Source):
    name = "github_issues"

    def __init__(self) -> None:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN not set. Create a fine-grained PAT with read-only "
                "public repo access at https://github.com/settings/tokens?type=beta "
                "and put it in .env."
            )
        self._client = httpx.Client(
            base_url=_BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": _USER_AGENT,
            },
            timeout=httpx.Timeout(30.0),
        )

    # --- public API ---------------------------------------------------------

    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        repos: list[str] = config.get("repos") or ([config["repo"]] if config.get("repo") else [])
        if not repos:
            return
        include_labels = set(config.get("include_labels") or [])
        exclude_labels = set(config.get("exclude_labels") or [])
        fetch_comments = bool(config.get("fetch_comments", True))
        max_comments = int(config.get("max_comments_per_issue", _DEFAULT_MAX_COMMENTS))
        per_page = int(config.get("per_page", _DEFAULT_PER_PAGE))
        sleep_between_repos = float(config.get("sleep_between_repos_seconds", 0.5))

        floor: float = float(cursor.cursor_ts or 0)
        newest_seen = floor

        for idx, repo in enumerate(repos):
            if idx and sleep_between_repos:
                time.sleep(sleep_between_repos)
            for issue in self._iter_issues(repo, floor, per_page):
                if "pull_request" in issue:
                    continue
                labels = {l["name"] for l in (issue.get("labels") or [])}
                if include_labels and not (labels & include_labels):
                    continue
                if exclude_labels and (labels & exclude_labels):
                    continue
                yield _issue_to_item(issue, repo)

                # Track newest updated_at (cursor is on updated_at, not created_at,
                # so re-edits/new comments pull the issue back into the feed).
                ts = _parse_iso(issue["updated_at"]).timestamp()
                if ts > newest_seen:
                    newest_seen = ts

                if fetch_comments and (issue.get("comments") or 0) > 0:
                    yielded_count = 0
                    for comment in self._iter_comments(repo, issue["number"], per_page):
                        if yielded_count >= max_comments:
                            stats.comment_cap_hits.append(
                                (
                                    f"{repo}#{issue['number']}",
                                    int(issue.get("comments") or 0),
                                    max_comments,
                                )
                            )
                            break
                        body = comment.get("body") or ""
                        if body in ("[deleted]", "[removed]"):
                            continue
                        yielded_count += 1
                        yield _comment_to_item(comment, issue, repo)

        if newest_seen > floor:
            cursor.cursor_ts = newest_seen

    # --- helpers ------------------------------------------------------------

    def _iter_issues(self, repo: str, floor: float, per_page: int) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": per_page,
        }
        if floor > 0:
            params["since"] = _iso(floor)
        url: Optional[str] = f"/repos/{repo}/issues"
        while url:
            # On first page use url+params; on follow-ups use the full URL from Link header.
            resp = _retry_http.request_with_retry(
                (lambda u=url, p=params: self._client.get(u, params=p))
                if params else (lambda u=url: self._client.get(u)),
                source_id="github_issues",
            )
            resp.raise_for_status()
            for issue in resp.json():
                yield issue
            url = _next_url(resp.headers.get("Link"))
            params = {}  # next URL already contains its own query string

    def _iter_comments(
        self, repo: str, issue_number: int, per_page: int
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": per_page}
        url: Optional[str] = f"/repos/{repo}/issues/{issue_number}/comments"
        while url:
            resp = _retry_http.request_with_retry(
                (lambda u=url, p=params: self._client.get(u, params=p))
                if params else (lambda u=url: self._client.get(u)),
                source_id="github_issues",
            )
            resp.raise_for_status()
            for comment in resp.json():
                yield comment
            url = _next_url(resp.headers.get("Link"))
            params = {}

    def __del__(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
