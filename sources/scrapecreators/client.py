"""Shared HTTP client for ScrapeCreators (POST_V1_PLAN §4.9).

Handles auth, credit tracking, 402 halt, and mock-mode dispatch. All three
platform plugins (reddit, x, tiktok) share ONE instance per fetch — the
credit budget is enforced globally, not per-platform.

Design decisions:

- **One credit per request** is the typical cost. SC doesn't publish per-
  endpoint costs, so we count requests and treat that as credits. If the
  API surfaces a `credits_used` header later, wire it here.

- **402 (Payment Required) is a HARD halt.** No retries with backoff — that
  would burn credits. The affected source raises `ScrapeCreatorsHalted`,
  the fetch stage catches it, appends to `stats.errors`, and moves on to
  the next source.

- **Rate limits.** SC docs claim "no enforced rate limits" but we still
  sleep 100ms between calls to be a good citizen and to hedge against
  future policy changes.

- **Mock mode.** `SCRAPECREATORS_MOCK=1` diverts every `get()` to the
  fixture reader (mock.py). No network traffic. Credit tracker still
  increments so tests can exercise the cap logic.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.scrapecreators.com"
_DEFAULT_CAP = 200
_INTER_REQUEST_SLEEP_SECONDS = 0.1


class ScrapeCreatorsError(RuntimeError):
    """Base class for SC-specific failures."""


class ScrapeCreatorsHalted(ScrapeCreatorsError):
    """Raised when the shared client refuses to make more calls: either
    the per-run credit cap is exhausted, or the API returned 402."""


class ScrapeCreatorsAuthError(ScrapeCreatorsError):
    """401/403 from SC — key is wrong or has been revoked."""


@dataclass
class CreditTracker:
    """Counts credits (requests) spent this fetch. Enforces a per-run cap."""

    cap: int = _DEFAULT_CAP
    spent: int = 0
    halted: bool = False
    halt_reason: str = ""

    def would_exceed(self) -> bool:
        return self.spent >= self.cap

    def spend(self, n: int = 1) -> None:
        self.spent += n

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason


@dataclass
class ScrapeCreatorsClient:
    """One instance per fetch. Threads share the same credit tracker.

    Instantiate once per fetch run; pass into each platform plugin's
    `fetch_since()`. Do NOT reuse across runs — the credit counter is
    per-run.
    """

    api_key: str
    tracker: CreditTracker = field(default_factory=CreditTracker)
    mock: bool = False
    timeout_seconds: float = 30.0
    _client: Optional[httpx.Client] = None

    @classmethod
    def from_env(cls, cap: Optional[int] = None) -> "ScrapeCreatorsClient":
        """Build a client from env: SCRAPECREATORS_API_KEY + SCRAPECREATORS_MOCK.

        `cap` overrides the tracker's default. Callers pass in
        `fetching.scrapecreators_max_credits_per_run` from app.yaml.
        """
        api_key = os.environ.get("SCRAPECREATORS_API_KEY", "").strip()
        mock = _is_truthy(os.environ.get("SCRAPECREATORS_MOCK", ""))
        if not api_key and not mock:
            raise ScrapeCreatorsAuthError(
                "SCRAPECREATORS_API_KEY is not set. Get a key at "
                "scrapecreators.com and add it via /connections."
            )
        return cls(
            api_key=api_key or "mock",
            tracker=CreditTracker(cap=cap if cap is not None else _DEFAULT_CAP),
            mock=mock,
        )

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Fetch a JSON payload from SC. Increments the credit tracker.

        Raises:
          ScrapeCreatorsHalted   Cap exhausted (before request) or 402 (after).
          ScrapeCreatorsAuthError 401/403 — key is wrong.
          ScrapeCreatorsError    Any other transport / 5xx failure.
        """
        if self.tracker.halted:
            raise ScrapeCreatorsHalted(
                f"scrapecreators halted earlier this run: {self.tracker.halt_reason}"
            )
        if self.tracker.would_exceed():
            self.tracker.halt(f"per-run credit cap ({self.tracker.cap}) exhausted")
            raise ScrapeCreatorsHalted(self.tracker.halt_reason)

        if self.mock:
            from sources.scrapecreators import mock as _mock
            self.tracker.spend(1)
            return _mock.get_fixture(path, params or {})

        try:
            resp = self._http().get(
                _BASE_URL + path,
                params=params or {},
                headers={"x-api-key": self.api_key},
            )
        except httpx.HTTPError as e:
            raise ScrapeCreatorsError(f"scrapecreators transport error: {e}") from e

        self.tracker.spend(1)
        time.sleep(_INTER_REQUEST_SLEEP_SECONDS)

        if resp.status_code == 402:
            self.tracker.halt("scrapecreators returned 402 (payment required / credits exhausted)")
            raise ScrapeCreatorsHalted(self.tracker.halt_reason)
        if resp.status_code in (401, 403):
            raise ScrapeCreatorsAuthError(
                f"scrapecreators {resp.status_code}: check SCRAPECREATORS_API_KEY"
            )
        if resp.status_code >= 400:
            raise ScrapeCreatorsError(
                f"scrapecreators {resp.status_code} on {path}: {resp.text[:200]}"
            )
        return resp.json()

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _is_truthy(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Shared per-run client
# ---------------------------------------------------------------------------
#
# All three SC plugins share ONE credit budget (§4.9 trade-off). We keep a
# process-wide singleton so each SC Source subclass instantiates the same
# ScrapeCreatorsClient during a single fetch run. Callers reset it between
# runs via `reset_shared_client()`.

_shared_client: Optional[ScrapeCreatorsClient] = None
_pending_cap: Optional[int] = None


def get_shared_client(cap: Optional[int] = None) -> ScrapeCreatorsClient:
    """Return the process-wide SC client, creating it on first call.

    `cap` is applied only when creating the client; subsequent calls ignore
    it. Callers wanting a fresh budget must first call `reset_shared_client()`.
    """
    global _shared_client
    if _shared_client is None:
        effective_cap = cap if cap is not None else _pending_cap
        _shared_client = ScrapeCreatorsClient.from_env(cap=effective_cap)
    return _shared_client


def reset_shared_client(cap: Optional[int] = None) -> None:
    """Reset the process-wide client. Call at the start of each fetch run
    so the credit tracker starts at zero and any config changes (cap,
    mock flag) are re-read from env.

    Pass `cap` here to pre-set the per-run credit budget without needing
    to instantiate the client eagerly (which would fail when no API key
    is configured but the product doesn't use SC).
    """
    global _shared_client, _pending_cap
    if _shared_client is not None:
        _shared_client.close()
    _shared_client = None
    _pending_cap = cap
