"""Shared GitHub API helpers for Issues + Discussions plugins."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from pipeline import http as _retry_http

REST_BASE_URL = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
_USER_AGENT = "product-monitor/0.1"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


def github_token() -> str:
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN not set. Create a fine-grained PAT with read-only "
            "public repo access at https://github.com/settings/tokens?type=beta "
            "and put it in .env."
        )
    return token


def rest_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {github_token()}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }


def graphql_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {github_token()}",
        "User-Agent": _USER_AGENT,
    }


def new_rest_client() -> httpx.Client:
    return httpx.Client(
        base_url=REST_BASE_URL,
        headers=rest_headers(),
        timeout=httpx.Timeout(30.0),
    )


def new_graphql_client() -> httpx.Client:
    return httpx.Client(
        headers=graphql_headers(),
        timeout=httpx.Timeout(30.0),
    )


def graphql_request(client: httpx.Client, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    resp = _retry_http.request_with_retry(
        lambda: client.post(GRAPHQL_URL, json={"query": query, "variables": variables}),
        source_id="github",
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


def split_repo(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner.strip(), name.strip()
