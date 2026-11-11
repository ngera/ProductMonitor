# ADR-0025: Run notifications via outbound webhook

- **Status:** Accepted
- **Date:** 2026-09-15
- **Area:** infra
- **Supersedes:** the "No email / cloud delivery in v1" line in
  [CLAUDE.md](../../CLAUDE.md)'s Non-goals section.

## Context

The Non-goals line in CLAUDE.md was written when ProductMonitor was an
interactive tool — the operator opened the UI, triggered a run, watched
it complete. Silence was fine; the operator was there.

That framing is stale. Post-V1 shipped:
- ADR-0016 persistent-issue stage (assumes multi-week continuity)
- The scheduler + `products/<id>/schedule.yaml` cadence
- ADR-0022 HTTP retry (transient errors that previously crashed a
  fetch now log-and-retry, hiding weeks-thinner reports)
- The `headless` compose profile (ADR-0012 in spirit; see
  [documents/OPERATING.md](../OPERATING.md))

The install shape this ADR is written for: a headless server, `docker
compose --profile headless up -d`, weekly cadence, nobody watching. In
that shape, a run that fails, or one that finishes with zero items
because a YouTube key expired, produces silence that looks exactly like
"nothing happened this week."

`/healthz` (from #12b) closes part of the gap — an external monitor can
detect stale-successful-run. But it can't distinguish "a run failed" from
"a run succeeded but the LLM classified everything as irrelevant," and
it requires the operator to have wired up an external monitor at all.

## Options considered

1. **Outbound webhook POST at end of run.** One config field
   (`notification_webhook_url`), one HTTP call per completed run, JSON
   body. Works with Slack incoming webhooks, Discord, ntfy.sh,
   self-hosted receivers. Zero SaaS coupling — the user provides their
   own receiver, so we preserve "local-first" cleanly.

2. **Local file + separate opt-in relay.** Write per-run JSON under
   `data/<product>/notifications/`; ship no delivery. Users who want
   Slack/email install a separate relay. Purest local-first, but the
   silence bug stays half-fixed — the notification exists but nothing
   reads it by default.

3. **Email via user-supplied SMTP.** Config for host/port/user/pass in
   `.env`, `smtplib` at end of run. Ubiquitous transport, no third-party
   sign-up. But: SMTP config surface is huge, deliverability from a
   home IP is unreliable, and Gmail/Outlook require app passwords or
   OAuth — friction that argues against.

4. **Both webhook and email.** Ship in one PR. Rejected as scope creep
   — every user's setup is one or the other; ship the smaller thing
   first and add email later behind the same abstraction if requested.

## Decision

**Option 1.** Single outbound webhook POST at end of run.

**Configuration** — new global key in `config/app.yaml` (not per-product;
one operator, one receiver):

```yaml
notifications:
  webhook_url: https://hooks.slack.com/services/T00/B00/xxx     # optional
  notify_on: [failed, partial, success_with_zero_items]         # subset of triggers
  timeout_seconds: 5
```

Empty / missing `webhook_url` → notifications skipped silently (opt-in).

`notify_on` controls which run outcomes emit. Defaults to `[failed,
partial]` — success is the norm and not worth pinging on. The
`success_with_zero_items` trigger fires when status=success but
`counters.fetched == 0` (the "YouTube key expired" case).

**Payload** — one JSON POST, both Slack-compatible and generic:

```json
{
  "text": "ProductMonitor: run failed — 'windows' 2026-W37 (2 errors)",
  "status": "failed",
  "product_id": "windows",
  "run_id": "sched-20260915T090000-abc123",
  "week_id": "2026-W37",
  "duration_seconds": 187.4,
  "counters": {"fetched": 421, "classified": 0},
  "errors": ["fatal: LLM endpoint unreachable"],
  "report_url": null
}
```

`text` is a human-readable one-liner for chat clients that treat the
whole payload as a fallback (Slack, Discord). Everything else is
structured — a self-hosted receiver can key off `status` and route.

**Feature flag.** `run_notifications_enabled`, default `false`. Even
with the config filled in, no notifications until the flag is on.
Prevents notifications from firing during upgrades or the first run
after a fresh install by accident.

**Failure handling.** A failed webhook must NOT fail the run.
Try/except with a WARN log; the run's `.json` completes normally. This
follows CLAUDE.md's "optional telemetry logged, config errors fatal" —
notifications are optional telemetry.

**Local-first stance.** The webhook URL is user-supplied → no
phone-home. If the user points it at Slack, THEIR data goes to Slack;
that's their choice, explicit in their config. This preserves the
Non-goals principle in spirit ("no default outbound calls that aren't
LLM/source fetches the user explicitly configured") while unblocking
the operability need. Documented in
[documents/OPERATING.md](../OPERATING.md).

## Rationale

The webhook shape wins on every axis that matters here:

- **Ergonomics for OSS.** Slack incoming webhook URL, one-time setup,
  no OAuth app, no client registration. Discord ditto. ntfy.sh works
  identically. Same payload works for a self-hosted `nc -l 8080`.
- **Local-first preserved.** No default outbound; explicit user config
  required.
- **Small blast radius.** One 5-second HTTP call at end of run. If
  it's slow or fails, the run doesn't care.
- **Debuggable.** WARN logs on failure; the payload is JSON any
  operator can `jq` or eyeball.
- **Fits the deploy shape.** Both the embedded scheduler and the
  headless `webui.scheduler_runtime` service already invoke
  `pipeline.run.main` for every run, so the call site is one place.

Email loses on config surface, deliverability, and OAuth friction.
Local-file-only loses on solving the actual problem (silence).

## Consequences

**Easier:**
- Silent-failure detection: unattended installs get a real signal.
- Slack integration for teams that use it — the same webhook works.
- Extending to `on_zero_new_items` or `on_no_bug_reports_this_week` is
  additive; the `notify_on` list grows.

**Harder:**
- Adds one config surface. Small; contained to `config/app.yaml` +
  a new module.
- Third-party outbound is now a normal thing in this codebase.
  ADR-0022 already opened that door (retrying LLM/source HTTP), but
  this ADR is the first that makes an outbound call that isn't a
  data-fetch. Every future outbound integration should cite this ADR
  as precedent and stay opt-in.

**CLAUDE.md updates:**
- Replace the "No email / cloud delivery in v1" bullet with:
  "**No default-on outbound integrations.** Notifications, exports,
  and other outbound calls stay opt-in and user-configured; ADR-0025
  is the pattern."
- The "local-first stays local" MUST rule is unchanged — user-supplied
  webhook URLs remain user-controlled data.

## Revisit if

- Users start asking for email. Then either add an email transport
  behind the same `pipeline.notify` abstraction, or (better) recommend
  they point the webhook at a receiver like Zapier / n8n / a tiny
  self-hosted forwarder. The webhook stays the primary; email is a
  transport-plugin question.
- A run produces multiple notifications (per-product breakdown, per-
  severity bug list). Currently one POST per run; batching is out of
  scope and no user has asked.
- Someone wants HMAC-signed payloads (webhook receiver wants to verify
  the caller is us). Add `notifications.webhook_secret` and an
  `X-Signature-SHA256` header; the payload signing is a small addition
  to the same code path.
