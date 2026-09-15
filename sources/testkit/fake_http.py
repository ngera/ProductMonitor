"""Deterministic httpx mock transport for plugin conformance tests.

Wraps httpx.MockTransport with a simple URL→response registration API
so plugin authors don't need to learn httpx's mock internals to write
a conformance test.

    transport = FakeHTTPTransport()
    transport.register("https://example.com/feed", body=FEED_XML)
    transport.register("https://example.com/api", json={"items": [...]})

    # Point your source's httpx.Client at this transport:
    with mock.patch.object(my_source, "_client",
                           httpx.Client(transport=transport)):
        ...run fetch_since...

The registry matches on `url.startswith(prefix)` so query-string
variants collapse to one handler. Unmatched requests return 404,
which surfaces missing fixtures as a test failure rather than a
network timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx


@dataclass
class _Handler:
    matcher: Callable[[httpx.Request], bool]
    responder: Callable[[httpx.Request], httpx.Response]


class FakeHTTPTransport(httpx.MockTransport):
    """MockTransport with URL-prefix registration + call recording."""

    def __init__(self) -> None:
        self._handlers: list[_Handler] = []
        self.calls: list[httpx.Request] = []
        super().__init__(self._route)

    # --- public API ---------------------------------------------------------

    def register(
        self,
        url_prefix: str,
        *,
        body: Optional[bytes | str] = None,
        json: Any = None,
        status: int = 200,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Match any request whose URL starts with `url_prefix` and return
        a canned response. Later registrations shadow earlier ones for
        overlapping prefixes."""
        def _match(req: httpx.Request) -> bool:
            return str(req.url).startswith(url_prefix)

        def _respond(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=status,
                headers=headers or {},
                content=self._body_bytes(body, json),
            )
        self._handlers.append(_Handler(_match, _respond))

    def register_dynamic(
        self,
        matcher: Callable[[httpx.Request], bool],
        responder: Callable[[httpx.Request], httpx.Response],
    ) -> None:
        """Register a handler with custom matching + response logic.
        Use for pagination-aware fixtures or when the response depends
        on the request's query params."""
        self._handlers.append(_Handler(matcher, responder))

    # --- transport internals ------------------------------------------------

    def _route(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        # Later registrations win → iterate in reverse.
        for h in reversed(self._handlers):
            if h.matcher(request):
                return h.responder(request)
        return httpx.Response(
            404, content=f"FakeHTTPTransport: no handler for {request.url}".encode(),
        )

    @staticmethod
    def _body_bytes(body: Optional[bytes | str], json: Any) -> bytes:
        if json is not None:
            import json as _json
            return _json.dumps(json).encode("utf-8")
        if body is None:
            return b""
        if isinstance(body, str):
            return body.encode("utf-8")
        return body
