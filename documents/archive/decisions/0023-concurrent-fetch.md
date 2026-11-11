# ADR-0023: Bounded concurrent stream fetch

- **Status:** Accepted
- **Date:** 2026-09-15
- **Area:** infra

## Context

The fetch stage walks every configured source, then every stream inside
it, strictly in order, with an inter-stream sleep. Sixteen news feeds
plus ten subreddits is minutes of wall clock spent almost entirely
waiting on network — the CPU sits idle behind one blocking `httpx.get`
at a time.

Post-V1 wave grew the source count meaningfully (RSS + media-coverage
sources, ADR-0021 taxonomy). The serial cost was tolerable at 3 sources;
at 20+ it's the dominant term in a run's wall-clock. On the unattended
compose profile ([ADR-0025](0025-run-notifications-via-webhook.md),
`documents/OPERATING.md`) the pipeline runs weekly with nobody
watching — a shorter run is unambiguously better.

Constraint from CLAUDE.md: **per-source rate limits are real**. Reddit
rate-limits by IP+UA regardless of stream count. Parallelizing every
stream at once would produce more 429s, not fewer.

Prior perf work (#6 batched DB writes, ADR-not-required) explicitly
sequenced this ADR after itself: concurrent fetch against per-item
`filter_unseen` calls would have multiplied SQLite lock contention.
Now that fetch dedup is one query per stream, the lock-contention
argument is gone.

## Options considered

1. **`asyncio` retrofit.** Rewrite every source connector to `httpx.AsyncClient`,
   `aiohttp`, async praw. Best possible concurrency ceiling, but is a
   multi-week rewrite touching every plugin and every plugin author's
   contract. Rejected as out of scope.

2. **`multiprocessing.Pool`.** True parallelism, no GIL. But every worker
   would need its own SQLite/DuckDB connection and its own source client;
   inter-process state sharing for `FetchStats` accumulators is awkward.
   Overkill — the workload is I/O-bound, not CPU-bound.

3. **`ThreadPoolExecutor` with two-tier semaphore.** Threads let the sync
   source stack (httpx + praw + feedparser) parallelize the network wait
   without any source-side rewrite. The GIL doesn't hurt because every
   worker is blocked on socket read for the vast majority of its runtime.
   Two-tier semaphore keeps rate-limits sane.

4. **Same as (3) but per-host cap = global cap = 1.** Serial, but with
   the executor framework in place. Rejected — that's the current
   behavior with more code.

## Decision

**Option 3.** `pipeline/fetch.py::_dispatch(work_items, max_workers,
per_host_cap)`:

- `ThreadPoolExecutor(max_workers=N)` for total in-flight cap.
- `threading.Semaphore(per_host_cap)` per host key, lazily created.
- Host key: URL host for URL-based streams (RSS `feed_url`); source_type
  for API-based streams (praw, github_issues) so all Reddit streams
  count against the same "reddit" quota regardless of subreddit.
- Per-stream errors surface as `_StreamResult(errors=[...])` — a
  crashing worker never kills the executor; per-stream isolation
  preserved.

**Configuration** — `config/app.yaml`:

    fetching:
      max_concurrent_streams: 4        # global cap
      max_concurrent_per_host: 1       # per-provider cap
      sleep_between_streams_seconds: 0 # serial path only; ignored when concurrent

Defaults are conservative: 4 total, 1 per host. Someone with 30 RSS
feeds across many hosts can raise the global cap. Someone hitting one
provider heavily leaves per-host at 1 and gets serial behavior against
that provider — which is what CLAUDE.md's "per-source rate limits are
real" requires.

**Feature flag** — `fetch_concurrency_enabled` in `config/features.yaml`,
default `false`. Same on/off shape as ADR-0022's `http_retry_enabled`.

**Structural change** in [pipeline/fetch.py](../../pipeline/fetch.py):
- `run_fetch()` enumerates work as `(worker_callable, host_key)` tuples.
- `_make_stream_worker()` binds one stream's fetch loop into a zero-arg
  callable — the SAME body that used to live inline.
- Dispatch: concurrent when flag on, serial when off. The serial path
  preserves `sleep_between_streams_seconds` for host politeness.
- Reducer folds per-stream `_StreamResult` back into run-level
  counters/errors/completeness. Reducer runs single-threaded regardless
  of dispatch mode.

**Thread-safety status** — this is the reason for the flag being off by
default. The audit is not complete; the flag turns on ONLY after these
sources have been individually verified:

| Source                                    | Client type            | Thread-safe? | Notes |
|-------------------------------------------|------------------------|--------------|-------|
| `sources/hn.py` (httpx.Client)            | Shared per-instance    | Yes          | httpx.Client is thread-safe for concurrent request calls |
| `sources/rss.py` (httpx.Client)           | Shared per-instance    | Yes          | Same |
| `sources/stackex.py` (httpx.Client + backoff state) | Shared per-instance | **NO**       | `_backoff_until` is mutated without a lock; needs a lock or per-thread client before concurrency is enabled for stackex |
| `sources/github_issues.py` (httpx.Client) | Shared per-instance    | Yes          | Same as HN |
| `sources/reddit.py` (praw)                | Shared per-instance    | **NO**       | praw is documented as not thread-safe; needs per-thread Reddit client OR a source-level lock |
| `sources/reddit_rss.py` (feedparser)      | No shared state        | Yes          | feedparser is a pure parser |
| `sources/microsoft_community.py`          | httpx.Client           | Yes          | Same |
| `sources/apple_appstore.py`               | httpx.Client           | Yes          | Same |
| `sources/producthunt.py`                  | httpx.Client           | Yes          | Same |
| `sources/youtube_comments.py`             | httpx.Client           | Yes          | Same |
| `sources/scrapecreators/client.py`        | Shared credit counter  | **NO**       | Credit counter is not lock-protected; concurrent calls could exceed the cap. Also a paid API — probably shouldn't be parallelized anyway. |

Shared state on non-thread-safe sources is documented here rather than
retrofitted preemptively; the flag stays off until either those sources
are fixed OR the operator explicitly opts in knowing which sources are
enabled for their product. Sources that ARE thread-safe already work
correctly under the flag.

## Rationale

- **Threads not async.** Sync source stack, GIL-tolerant I/O-bound
  workload, zero source-side rewrite. Threads win on every axis that
  matters for THIS codebase.
- **Two-tier semaphore not one.** A single global cap would let 4
  streams hit Reddit simultaneously if we picked 4 as the cap. The
  per-host cap of 1 keeps single-provider workloads serial while still
  parallelizing across providers.
- **Flag off by default with a per-source audit.** ADR-0006 says every
  new capability ships behind a flag off. The audit table above is what
  makes the flip-to-on defensible — this ADR is the audit's home.
- **Extract-then-dispatch shape.** Serial and concurrent paths dispatch
  the SAME `_make_stream_worker` callable. Behavior is identical either
  way; only the dispatch mode differs. Guards against "the concurrent
  path silently diverges" drift.

## Consequences

**Easier:**
- Wall-clock cut for multi-source installs. Rough back-of-envelope:
  26 streams × 3s each serial = 78s; at 4 workers with mixed hosts,
  ~20s. Real-world speedups depend on host distribution.
- Adding a source no longer worsens run time linearly. New feeds are
  cheap if they're on a new host.

**Harder:**
- Bugs in source connectors that only manifest under concurrent access
  (praw, stackex, scrapecreators noted above). The flag-off default
  contains this — operators who enable it accept the audit.
- Log-line interleaving: `stream_fetched` messages from multiple
  streams appear intermixed. structlog's JSON output is still parseable
  by `run_id`+`stream`, but reading the stdout tail during a
  concurrent run is harder than during a serial run. Acceptable trade.

## Flag sunset plan

Ship default `false`. Fix stackex + praw + scrapecreators thread-safety
in follow-ups (either per-thread clients or module-level locks). Once
every shipped source is green in the audit table above, flip the global
default to `true` and remove the serial dispatch path. Track under this
ADR — the audit table IS the sunset criteria.

## Revisit if

- A single source needs a different per-host cap than others (e.g. HN's
  Algolia is fine at 4, but Reddit needs 1). Add per-source override to
  `fetching.per_host_caps: {reddit: 1, hn: 4}`; keep the top-level cap
  as the fallback.
- Async retrofit becomes viable (e.g. praw ships an async variant). Then
  ADR-0023 is superseded by an "async fetch" ADR, and this executor
  scaffolding retires.
- Concurrent execution surfaces race conditions in `pipeline.storage`
  (SQLite/DuckDB write paths). It shouldn't — every storage function
  opens+closes its own connection in-thread — but if it does, add a
  process-wide storage write lock as an intermediate.
