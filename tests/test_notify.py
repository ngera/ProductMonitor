"""Tests for pipeline.notify (ADR-0025 webhook notifications).

Covers:
- Silent no-op when flag off, url missing, or outcome not in notify_on.
- Payload shape (Slack-compatible `text` + structured fields).
- `success_with_zero_items` derivation from counters.
- Failure of the outbound HTTP call must NEVER raise.
- Webhook URL is redacted in log lines (Slack embeds a token in the path).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from pipeline import notify as notify_mod


# ---------------------------------------------------------------------------
# Fixture: flag + config toggle without touching real yaml
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_notify_config(monkeypatch: pytest.MonkeyPatch):
    """Return a mutable dict that the module treats as its config."""
    state: dict[str, Any] = {"flag": False, "config": {}}
    monkeypatch.setattr(notify_mod, "_flag_enabled", lambda: state["flag"])
    monkeypatch.setattr(notify_mod, "_config", lambda: state["config"])
    return state


@pytest.fixture
def capture_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Intercept httpx.post; record calls; return recorded payloads."""
    captured: list[dict] = []

    def _fake_post(url: str, *, json: dict, timeout: float, headers: dict):
        captured.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify_mod.httpx, "post", _fake_post)
    return captured


# ---------------------------------------------------------------------------
# Outcome derivation
# ---------------------------------------------------------------------------


class TestOutcome:
    def test_failed_maps_direct(self) -> None:
        assert notify_mod._outcome("failed", {"fetched": 100}) == "failed"

    def test_partial_maps_direct(self) -> None:
        assert notify_mod._outcome("partial", {"fetched": 100}) == "partial"

    def test_success_with_items(self) -> None:
        assert notify_mod._outcome("success", {"fetched": 12}) == "success"

    def test_success_with_zero_fetched_is_own_outcome(self) -> None:
        # This is the ADR's motivating case: green status masking a
        # silent failure (expired API key, broken feed).
        assert notify_mod._outcome("success", {"fetched": 0}) == "success_with_zero_items"

    def test_success_with_no_counters_is_zero_items(self) -> None:
        assert notify_mod._outcome("success", None) == "success_with_zero_items"


# ---------------------------------------------------------------------------
# Silent no-op paths
# ---------------------------------------------------------------------------


class TestOptIn:
    def test_flag_off_never_posts(self, stub_notify_config, capture_posts) -> None:
        stub_notify_config["flag"] = False
        stub_notify_config["config"] = {"webhook_url": "https://example.com/x"}
        notify_mod.notify_run(
            status="failed", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 0}, errors=["boom"],
        )
        assert capture_posts == []

    def test_missing_url_never_posts(self, stub_notify_config, capture_posts) -> None:
        stub_notify_config["flag"] = True
        stub_notify_config["config"] = {}
        notify_mod.notify_run(
            status="failed", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 0}, errors=["boom"],
        )
        assert capture_posts == []

    def test_empty_url_never_posts(self, stub_notify_config, capture_posts) -> None:
        stub_notify_config["flag"] = True
        stub_notify_config["config"] = {"webhook_url": "   "}
        notify_mod.notify_run(
            status="failed", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 0}, errors=["boom"],
        )
        assert capture_posts == []

    def test_outcome_not_in_notify_on_is_skipped(
        self, stub_notify_config, capture_posts,
    ) -> None:
        # Default notify_on excludes "success"; a clean run should be silent.
        stub_notify_config["flag"] = True
        stub_notify_config["config"] = {"webhook_url": "https://example.com/x"}
        notify_mod.notify_run(
            status="success", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 200}, errors=[],
        )
        assert capture_posts == []


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


class TestPayload:
    @pytest.fixture(autouse=True)
    def _enable(self, stub_notify_config):
        stub_notify_config["flag"] = True
        stub_notify_config["config"] = {
            "webhook_url": "https://hooks.slack.com/services/T00/B00/xyz",
        }

    def test_failed_run_posts(self, capture_posts) -> None:
        notify_mod.notify_run(
            status="failed", product_id="windows", run_id="ui-x",
            week_id="2026-W37", duration_seconds=42.5,
            counters={"fetched": 100, "classified": 0},
            errors=["fatal: LLM unreachable"],
        )
        assert len(capture_posts) == 1
        body = capture_posts[0]["json"]
        assert body["status"] == "failed"
        assert body["outcome"] == "failed"
        assert body["product_id"] == "windows"
        assert body["run_id"] == "ui-x"
        assert body["week_id"] == "2026-W37"
        assert body["duration_seconds"] == 42.5
        assert body["counters"] == {"fetched": 100, "classified": 0}
        assert body["errors"] == ["fatal: LLM unreachable"]

    def test_text_field_is_human_readable(self, capture_posts) -> None:
        notify_mod.notify_run(
            status="failed", product_id="windows", run_id="ui-x",
            week_id="2026-W37", duration_seconds=1.0,
            counters={"fetched": 100, "classified": 42}, errors=["e1", "e2"],
        )
        text = capture_posts[0]["json"]["text"]
        assert "failed" in text
        assert "windows" in text
        assert "2026-W37" in text
        assert "2 errors" in text
        assert "100 fetched" in text

    def test_zero_items_success_triggers_notification(
        self, stub_notify_config, capture_posts,
    ) -> None:
        # notify_on includes success_with_zero_items? Not by default —
        # explicitly opt in to prove the outcome-key wiring works.
        stub_notify_config["config"] = {
            "webhook_url": "https://example.com/x",
            "notify_on": ["failed", "partial", "success_with_zero_items"],
        }
        notify_mod.notify_run(
            status="success", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 0}, errors=[],
        )
        assert len(capture_posts) == 1
        assert capture_posts[0]["json"]["outcome"] == "success_with_zero_items"


# ---------------------------------------------------------------------------
# Failure never raises
# ---------------------------------------------------------------------------


class TestFailureSwallowed:
    @pytest.fixture(autouse=True)
    def _enable(self, stub_notify_config):
        stub_notify_config["flag"] = True
        stub_notify_config["config"] = {"webhook_url": "https://example.com/x"}

    def test_connection_error_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(*_a, **_k):
            raise httpx.ConnectError("network down")
        monkeypatch.setattr(notify_mod.httpx, "post", _boom)

        # Should return without raising.
        notify_mod.notify_run(
            status="failed", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 0}, errors=["x"],
        )

    def test_5xx_response_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _fivexx(url, **_k):
            return httpx.Response(500, text="bad", request=httpx.Request("POST", url))
        monkeypatch.setattr(notify_mod.httpx, "post", _fivexx)

        notify_mod.notify_run(
            status="failed", product_id="p", run_id="r", week_id="w",
            duration_seconds=1.0, counters={"fetched": 0}, errors=["x"],
        )


# ---------------------------------------------------------------------------
# URL redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_slack_path_stripped(self) -> None:
        redacted = notify_mod._redact(
            "https://hooks.slack.com/services/T00/B00/secret_token"
        )
        assert "secret_token" not in redacted
        assert redacted == "https://hooks.slack.com/..."

    def test_discord_path_stripped(self) -> None:
        redacted = notify_mod._redact(
            "https://discord.com/api/webhooks/12345/very_secret"
        )
        assert "very_secret" not in redacted

    def test_invalid_url_returns_placeholder(self) -> None:
        assert notify_mod._redact("::::") == "<invalid-url>"
