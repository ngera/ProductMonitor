"""Conformance kit run against the built-in HN source.

Two goals:
  1. Prove the kit is usable end-to-end (the reason we ship it).
  2. Give the HN source a floor of coverage that would break if a
     future refactor violated the contract.

Uses FakeHTTPTransport to serve deterministic Algolia responses, so
the test never hits the real hn.algolia.com.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("httpx")

import httpx

from sources.testkit import SourceConformanceTests, FakeHTTPTransport


def _algolia_response(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape of Algolia's search_by_date response."""
    return {"hits": hits, "nbHits": len(hits)}


_FIXTURE_HITS = [
    {
        "objectID": "40000001",
        "title": "The joy of building simple things",
        "url": "https://example.com/simple",
        "author": "example_user",
        "created_at_i": 1_726_400_000,   # ~2024-09-15
        "points": 42,
        "num_comments": 3,
        "_tags": ["story"],
    },
    {
        "objectID": "40000002",
        "title": "Second HN post",
        "url": "https://example.com/second",
        "author": "another_user",
        "created_at_i": 1_726_500_000,
        "points": 10,
        "num_comments": 1,
        "_tags": ["story"],
    },
]


class TestHNSourceConformance(SourceConformanceTests):
    from sources.hn import HackerNewsSource
    source_class = HackerNewsSource
    stream_config = {
        "name": "test-stream",
        "search_queries": ["testing"],
        "include_tags": ["story"],
        "max_pages_per_query": 1,
        "hits_per_page": 100,
    }
    expects_items = True

    def build_source(self):
        src = self.source_class()
        # Swap the client's transport to our fake so no real network calls fly.
        transport = FakeHTTPTransport()
        # Algolia paginates via `page=N`; register the first page with hits
        # and subsequent pages as empty so the loop terminates.
        def _paginated(req: httpx.Request) -> httpx.Response:
            import json as _json
            page = req.url.params.get("page", "0")
            if page == "0":
                return httpx.Response(200, content=_json.dumps(_algolia_response(_FIXTURE_HITS)).encode())
            return httpx.Response(200, content=_json.dumps(_algolia_response([])).encode())
        transport.register_dynamic(
            lambda req: "/search_by_date" in str(req.url), _paginated,
        )
        # Rebuild the source's client to point at the fake transport but
        # keep the same base_url/headers so the source code paths match
        # production exactly.
        src._client = httpx.Client(
            base_url=str(src._client.base_url),
            headers=dict(src._client.headers),
            transport=transport,
        )
        self._fake_transport = transport  # for test introspection
        return src


def test_fake_transport_was_actually_called() -> None:
    """Meta-test: prove the conformance run went through the mock, not
    the real network. If the source's client didn't get rewired, the
    calls list will be empty and the real API would have been hit."""
    tc = TestHNSourceConformance()
    tc._fetch_all()
    assert len(tc._fake_transport.calls) >= 1, (
        "FakeHTTPTransport recorded zero calls — the source may have "
        "bypassed the mock and hit the real network"
    )
