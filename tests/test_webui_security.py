"""webui/security.py — host allowlist and same-origin writes (ADR-0036)."""

from __future__ import annotations

import asyncio

import pytest

from webui.security import LocalOnlyMiddleware, host_allowed, same_origin

ALLOWED = {"127.0.0.1", "localhost", "::1", "testserver"}


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8766", True),
    ("localhost:8766", True),
    ("[::1]:8766", True),
    ("LOCALHOST", True),
    ("attacker.example:8766", False),   # DNS rebinding
    ("127.0.0.1.attacker.example", False),
    ("", False),
])
def test_host_allowed(host, ok):
    assert host_allowed(host, ALLOWED) is ok


@pytest.mark.parametrize("origin,host,ok", [
    ("http://127.0.0.1:8766", "127.0.0.1:8766", True),
    ("http://localhost:8766/products", "localhost:8766", True),
    ("http://localhost:3000", "localhost:8766", False),   # another local app
    ("https://evil.example", "127.0.0.1:8766", False),
    ("null", "127.0.0.1:8766", False),
    ("http://testserver", "testserver", True),
])
def test_same_origin(origin, host, ok):
    assert same_origin(origin, host) is ok


def _call(method, headers):
    reached = []

    async def app(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": method,
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(LocalOnlyMiddleware(app)(scope, receive, send))
    return sent[0]["status"], bool(reached)


def test_cross_site_post_refused():
    status, reached = _call("POST", {"host": "127.0.0.1:8766",
                                     "origin": "https://evil.example"})
    assert (status, reached) == (403, False)


def test_same_origin_post_allowed():
    status, reached = _call("POST", {"host": "127.0.0.1:8766",
                                     "origin": "http://127.0.0.1:8766"})
    assert (status, reached) == (200, True)


def test_cross_site_referer_refused_when_no_origin():
    status, _ = _call("POST", {"host": "localhost:8766",
                               "referer": "https://evil.example/page"})
    assert status == 403


def test_script_post_without_origin_allowed():
    status, _ = _call("POST", {"host": "127.0.0.1:8766"})
    assert status == 200


def test_rebinding_get_refused():
    status, reached = _call("GET", {"host": "attacker.example:8766"})
    assert (status, reached) == (403, False)


def test_cross_site_get_still_served():
    # Reads are harmless and links from elsewhere should keep working.
    status, _ = _call("GET", {"host": "127.0.0.1:8766",
                              "origin": "https://evil.example"})
    assert status == 200


def test_real_app_refuses_cross_site_form_post():
    pytest.importorskip("fastapi")
    pytest.importorskip("structlog")
    from fastapi.testclient import TestClient
    from webui.app import app

    with TestClient(app) as client:
        r = client.post("/settings/theme", data={"theme": "light"},
                        headers={"Origin": "https://evil.example"},
                        follow_redirects=False)
        assert r.status_code == 403
        r = client.post("/settings/theme", data={"theme": "light"},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False)
        assert r.status_code in (200, 303)
