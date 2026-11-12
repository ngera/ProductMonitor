# ADR-0037: Host cooldown after 429, and long Retry-After is not waited out

- **Status:** Accepted
- **Date:** 2026-11-11
- **Area:** infra

## Context

In the Supabase 2026-W39 run, 5 of 6 subreddits came back empty. Reddit
answered the first feed with 429, but every remaining subreddit still made
its request, plus ADR-0022's retries, and also got 429. The next run, started
minutes later, did the same. The failures only showed up in the run log, so
the digest looked like a quiet week.

ADR-0022 honors `Retry-After` inside a fetch. Reddit sends values in
minutes, so honoring them stalls every stream queued behind that host
(per-host concurrency is 1, ADR-0023).

## Options considered

1. **Keep retrying with longer backoff** — more 429s and slower runs; Reddit
   extends the block.
2. **Per-host cooldown, in process only** — fixes the run, but a re-run
   started straight away hits Reddit again.
3. **Per-host cooldown persisted under `data/`, plus a digest notice** —
   fixes both, and the reader sees what is missing.

## Decision

Option 3:

- A 429 on an RSS feed starts a cooldown for that host. reddit.com,
  www.reddit.com and old.reddit.com share one cooldown. Its length comes from
  `Retry-After`, defaults to 5 minutes, and is capped at 1 hour. It is
  stored in `data/.rate_limits.json`.
- While a host is cooling down, its streams skip the request and report
  `rate-limited` in `completeness.ceiling_hits`.
- `request_with_retry` returns a retryable response immediately when its
  `Retry-After` is over the 30-second backoff cap, instead of sleeping.
- The digest lists empty streams by reason under "Missing sources this
  week" (`pipeline/digest/fetch_gaps.py`).

## Rationale

Waiting out a minutes-long block inside a run buys nothing: the items
arrive next run either way, and every other stream waits too. Stopping
early, remembering the block and telling the reader is cheaper and honest.

## Consequences

- A run started during a cooldown fetches nothing from that host.
  Deleting `data/.rate_limits.json` forces a retry.
- Keyless Reddit still has no scores. Anonymous JSON is blocked, so RSS
  items rank by recency; the OAuth Reddit source is the route to
  engagement.
- `tests/test_live_sources.py` (opt-in, `PM_LIVE_TESTS=1`) checks the real
  endpoints; the offline suite covers the cooldown with a mock transport.

## Revisit if

- Other sources (Stack Exchange, Discourse) start hitting 429. Move the
  cooldown into `pipeline/http.py` so every plugin shares it.
