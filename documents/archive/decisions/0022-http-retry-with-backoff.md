# ADR-0022: Centralized retry-with-backoff for source HTTP calls

- **Status:** Accepted
- **Date:** 2026-09-15
- **Area:** infra

## Context

`tenacity==9.0.0` has been in `requirements.txt` since V1 but is imported
by nothing. Every source connector calls `httpx` directly and treats any
non-2xx response as a fatal error for that fetch. A single transient
`429 Too Many Requests` from Reddit or `503 Service Unavailable` from
Anthropic's status page RSS dropped the entire stream for the week.

The pattern was invisible in interactive use — the operator saw it, re-ran,
moved on. But on the target deployment (headless server, weekly `cron` /
scheduler tick, no one watching), every transient blip produced a thinner
digest with no error surfaced to the operator. The failure mode is silent
and cumulative.

LLM calls are NOT affected: `LLMClient` passes `max_retries` to the OpenAI
SDK, which already retries transient provider errors internally.

Constraints:
- Adding retry per-source would scatter policy across 8+ connectors; one
  would drift.
- Some source calls MUST NOT retry — `sources/scrapecreators/client.py`
  hits a paid API where 402 is a hard halt and every attempt costs money.
- Per [ADR-0006](0006-feature-flags-off-by-default.md) any new
  cross-cutting capability ships behind a flag, default off.

## Options considered

1. **`tenacity` decorators sprinkled on each source method** — quickest to
   apply but scatters policy; per-source tuning drifts; hard to audit
   which sources retry and which don't.
2. **Wrap `httpx.Client` with a retrying transport** — clean at the
   transport layer, but hides retry behavior from the reading eye and
   makes per-call opt-out (scrapecreators) awkward.
3. **Single `request_with_retry(call)` helper in `pipeline/http.py`** —
   explicit at every call site, trivially auditable via grep, natural
   opt-out (just don't call it), no dependency on tenacity's decorator
   surface.

## Decision

Option 3. `pipeline/http.py` exports `request_with_retry(call, *, source_id)`,
a small wrapper that:

- Retries up to 3 attempts (initial + 2 retries).
- Backoff: full-jitter exponential (1s, 2s, 4s; capped at 30s).
- Retryable statuses: `408, 425, 429, 500, 502, 503, 504`.
- Retryable exceptions: `httpx.TransportError`, `httpx.TimeoutException`.
- Honors `Retry-After` on 429 / 5xx when the server sends it.
- Non-retryable statuses (4xx that isn't a rate-limit code) return
  immediately — retrying a 401 wastes time and hides the real problem.
- WARN-logs every retry with source_id, attempt, status/exception, delay.
  Silent retries would make debugging impossible.
- Gated by `http_retry_enabled` feature flag (default `false`). When off,
  the callable runs once with no wrapper — preserves current behavior for
  the dogfooding window.

Sources adopt by importing `pipeline.http as _retry_http` and wrapping
their existing `client.get(...)` call. Initial adopters: `sources/hn.py`,
`sources/rss.py` (the two feeding most digest content).

Explicit non-adopters:
- `sources/scrapecreators/client.py` — paid API, 402 must halt hard.
- Any future source hitting a paid or credit-metered endpoint.

## Rationale

The helper-not-decorator shape wins because:
- **Auditable.** `grep request_with_retry` lists every retrying call site.
- **Opt-out is trivial.** Paid APIs just don't call it; no exclusion list
  to keep in sync.
- **No decorator plumbing.** Sources keep their existing sync httpx
  client; the wrapper is a `Callable[[], Response]` — five extra lines per
  call site.
- **Flag-gated shipping.** Off by default lets us watch retry logs in
  dogfooding before making retry the norm.

## Consequences

**Easier:**
- Adding retry to a new source is one import + one lambda wrap.
- Policy changes (attempts, statuses, backoff) live in one file.
- Every retry produces a log line — the Admin > Runs view will show the
  transient-error rate operators are seeing.

**Harder:**
- Callers must remember to wrap; there's no automatic behavior. This is
  by design — the paid-API exclusion is easier to enforce this way.
- Retries add latency on genuinely-broken endpoints. The 3-attempt cap
  bounds worst-case at ~30s per fetch; acceptable for a weekly job.

## Flag sunset plan

Ship default `false`. After one week of runs with the flag flipped `true`
in the maintainer's install (and retry logs reviewed), flip the global
default in `config/features.yaml`. Two weeks later, remove the flag and
the pass-through branch in `pipeline/http.py`. Track under this ADR — no
separate sunset ADR needed unless the retry policy itself is contested.

## Revisit if

- A source needs different retry policy (e.g. longer backoff for a
  particularly touchy endpoint) — add per-call overrides to the helper
  rather than forking a second helper.
- Providers start returning a body-level `retry_at` field that isn't a
  standard `Retry-After` header — parser needs extension.
- We add async source fetch — helper would need an async twin.
