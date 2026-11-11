"""Keep the local admin UI local.

The UI binds to 127.0.0.1 and has no login, which is fine for one operator
on one machine — but a browser will still carry requests to it from any web
page the operator visits. Two checks close that:

- **Host allowlist (all requests).** Blocks DNS rebinding, where an attacker's
  domain resolves to 127.0.0.1 so their page counts as same-origin.
- **Same-origin writes (POST/PUT/PATCH/DELETE).** A cross-site form post can
  otherwise change LLM endpoints, write `.env`, start runs or delete products.
  Browsers send `Origin` on cross-origin writes, so a write whose `Origin`
  (or, failing that, `Referer`) is not this UI is refused. Requests with
  neither header — curl, scripts — are let through.

Extra hostnames (a LAN name for a deliberate server install, or
`testserver` under pytest) come from `PRODUCT_MONITOR_ALLOWED_HOSTS`,
comma-separated, in the environment or in `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import structlog

log = structlog.get_logger()

_DEFAULT_HOSTS = ("127.0.0.1", "localhost", "::1")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_env_cache: tuple[float, str] = (-1.0, "")


def _env_file_value() -> str:
    """PRODUCT_MONITOR_ALLOWED_HOSTS from .env, re-read when the file changes.

    The UI writes .env without pushing it into os.environ, so the process
    environment alone would miss a value saved there."""
    global _env_cache
    try:
        mtime = _ENV_FILE.stat().st_mtime
    except OSError:
        return ""
    if mtime != _env_cache[0]:
        value = ""
        try:
            from dotenv import dotenv_values
            value = dotenv_values(_ENV_FILE).get("PRODUCT_MONITOR_ALLOWED_HOSTS") or ""
        except Exception as e:  # unreadable .env: fall back to the defaults
            log.warning("allowed_hosts_env_unreadable", error=str(e))
        _env_cache = (mtime, value)
    return _env_cache[1]


def allowed_hosts() -> set[str]:
    extra = ",".join((os.environ.get("PRODUCT_MONITOR_ALLOWED_HOSTS", ""),
                      _env_file_value()))
    hosts = {h.strip().lower() for h in extra.split(",") if h.strip()}
    return hosts | set(_DEFAULT_HOSTS)


def _split_host(value: str) -> tuple[str, str]:
    """'localhost:8766' → ('localhost', '8766'); '[::1]:8766' → ('::1', '8766')."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        return host, rest.lstrip(":")
    host, _, port = value.partition(":")
    return host, port


def host_allowed(host_header: str, allowed: Iterable[str]) -> bool:
    host, _ = _split_host(host_header)
    return bool(host) and host in set(allowed)


def same_origin(source_url: str, host_header: str) -> bool:
    """True when an Origin/Referer URL points at the host:port being served."""
    parts = urlsplit(source_url or "")
    if not parts.hostname:
        return False  # covers Origin: null (sandboxed iframes, file://)
    src = (parts.hostname.lower(), str(parts.port or ""))
    req_host, req_port = _split_host(host_header)
    return src[0] == req_host and (src[1] == req_port or not (src[1] or req_port))


class LocalOnlyMiddleware:
    """ASGI middleware applying the two checks above."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        host = headers.get("host", "")

        if not host_allowed(host, allowed_hosts()):
            return await _refuse(send, f"Host {host!r} is not allowed. "
                                 "Set PRODUCT_MONITOR_ALLOWED_HOSTS to serve it.")

        if scope.get("method", "GET").upper() in _UNSAFE_METHODS:
            source = headers.get("origin") or headers.get("referer")
            if source is not None and not same_origin(source, host):
                return await _refuse(send, "Cross-site request refused: "
                                     "changes can only be made from this UI.")

        return await self.app(scope, receive, send)


async def _refuse(send, message: str) -> None:
    body = message.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
