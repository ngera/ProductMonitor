"""Outbound run-completion notifications (ADR-0025).

One JSON POST per completed run to a user-supplied webhook URL. Works
with Slack incoming webhooks, Discord, ntfy.sh, or any receiver that
accepts application/json.

Contract:
- User-supplied URL only. No default endpoint, no phone-home.
- Opt-in via `run_notifications_enabled` feature flag AND a non-empty
  `notifications.webhook_url` in config/app.yaml.
- Failure of the notification must NEVER fail the run — try/except
  with WARN log, run's terminal `.json` is unaffected.
- Which outcomes emit is configured by `notifications.notify_on`
  (defaults to [failed, partial]).

See ADR-0025 for the rationale on webhook-over-email and the
supersession of CLAUDE.md's "No email / cloud delivery in v1" non-goal.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

import httpx

_log = logging.getLogger(__name__)

_DEFAULT_NOTIFY_ON = ("failed", "partial")
_DEFAULT_TIMEOUT_SECONDS = 5.0


def _outcome(status: str, counters: dict[str, Any] | None) -> str:
    """Derived outcome key for the `notify_on` allowlist.

    - `success` when the pipeline ran clean.
    - `success_with_zero_items` when it "succeeded" but fetched nothing —
      commonly a silent-failure signal (expired API key, broken feed).
      This is the case that motivates the ADR: a green status masking
      a real problem.
    - `partial` / `failed` map straight from run status.
    """
    if status == "failed":
        return "failed"
    if status == "partial":
        return "partial"
    if status == "success":
        fetched = int((counters or {}).get("fetched") or 0) if counters else 0
        return "success_with_zero_items" if fetched == 0 else "success"
    return status  # unknown status pass-through


def _flag_enabled() -> bool:
    # Late import — avoids config-load side effects at module import time.
    try:
        from pipeline.features import enabled
        return enabled("run_notifications_enabled")
    except Exception:
        return False


def _config() -> dict[str, Any]:
    try:
        from pipeline.config import app_config
        return app_config().get("notifications") or {}
    except Exception:
        return {}


def _human_summary(
    *, status: str, product_id: str, week_id: Optional[str],
    counters: dict[str, Any] | None, errors: list[str] | None,
) -> str:
    """One-line human-readable text; Slack renders this as the message
    body when it can't (or won't) parse the structured payload."""
    err_count = len(errors or [])
    parts = [
        f"ProductMonitor: run {status}",
        f"— '{product_id}'",
    ]
    if week_id:
        parts.append(week_id)
    fetched = int((counters or {}).get("fetched") or 0) if counters else 0
    classified = int((counters or {}).get("classified") or 0) if counters else 0
    parts.append(f"({fetched} fetched, {classified} classified)")
    if err_count:
        parts.append(f"[{err_count} error{'s' if err_count != 1 else ''}]")
    return " ".join(parts)


def notify_run(
    *,
    status: str,
    product_id: str,
    run_id: str,
    week_id: Optional[str],
    duration_seconds: float,
    counters: dict[str, Any] | None,
    errors: list[str] | None,
    report_url: Optional[str] = None,
) -> None:
    """Fire the webhook for one completed run. Silent no-op when
    disabled or misconfigured; WARN log on delivery failure.

    Never raises. The pipeline's terminal `.json` is written before
    this is called and must remain the source of truth about the run
    outcome.
    """
    if not _flag_enabled():
        return
    cfg = _config()
    url = (cfg.get("webhook_url") or "").strip()
    if not url:
        # No URL configured — normal state for installs that haven't
        # opted in even with the flag on. Silent.
        return

    notify_on = tuple(cfg.get("notify_on") or _DEFAULT_NOTIFY_ON)
    outcome = _outcome(status, counters)
    if outcome not in notify_on:
        return

    timeout = float(cfg.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    payload = {
        "text": _human_summary(
            status=status, product_id=product_id, week_id=week_id,
            counters=counters, errors=errors,
        ),
        "status": status,
        "outcome": outcome,
        "product_id": product_id,
        "run_id": run_id,
        "week_id": week_id,
        "duration_seconds": round(float(duration_seconds), 2),
        "counters": counters or {},
        "errors": list(errors or []),
        "report_url": report_url,
    }

    try:
        resp = httpx.post(
            url, json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            _log.warning(
                "notify_run_http_error status=%d url=%s body=%s",
                resp.status_code, _redact(url), resp.text[:200],
            )
        else:
            _log.info(
                "notify_run_sent product=%s run=%s outcome=%s status=%d",
                product_id, run_id, outcome, resp.status_code,
            )
    except Exception as e:
        # Notification failure must not fail the run. Log with the redacted
        # URL so a bad receiver is diagnosable without leaking the secret
        # path segment of the webhook (Slack embeds a token in the path).
        _log.warning(
            "notify_run_failed product=%s run=%s outcome=%s error=%s url=%s",
            product_id, run_id, outcome, type(e).__name__, _redact(url),
        )


def _redact(url: str) -> str:
    """Strip the path from a webhook URL so log lines don't leak the
    secret token embedded in it. `https://hooks.slack.com/services/T00/B00/xxx`
    → `https://hooks.slack.com/...`."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    if not parts.scheme or not parts.netloc:
        return "<invalid-url>"
    return f"{parts.scheme}://{parts.netloc}/..."
