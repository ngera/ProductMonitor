# ADR-0027: Plugin conformance test kit

- **Status:** Accepted
- **Date:** 2026-09-15
- **Area:** infra

## Context

ADR-0001 says source plugins are the extension point — anything a
contributor writes is coverage the maintainers didn't have to build.
The runtime contract lives in `sources/base.py` (`Source` ABC + the
`SourceCursor` / `FetchStats` / `MANIFEST` shapes), and
`documents/PLUGIN_AUTHORS.md` walks authors through the mechanics.

That gave authors two things: a docs page and a base class. It didn't
give them a way to *verify* their plugin honors the contract before
shipping. The contract has ~10 invariants (url populated on every
item, cursor advances monotonically, `created_at` is UTC-aware,
`FetchStats.ceiling_hits` reported on paging limits, etc.). A
contributor reading the base class can meet 8 of 10, ship, and the
first sign of trouble is a broken digest a week later.

ADR-0023 also raised the stakes: `fetch_concurrency_enabled: true`
means one Source instance now sees `fetch_since` called from multiple
threads. Any plugin holding mutable state on `self` — a shared cursor,
a page counter, a rate-limit budget — corrupts silently. The
contract-in-tests must cover this.

## Options considered

1. **Prose in `PLUGIN_AUTHORS.md` only.** Cheapest. Doesn't fail
   loudly when someone deviates; drifts as the contract evolves.
2. **A `Source` metaclass that runtime-validates.** Catches some
   invariants but not the interesting ones (concurrent invocation,
   cursor monotonicity, url populated on yielded items). Adds import-
   time complexity to every plugin.
3. **A pytest-based conformance kit imported from `sources.testkit`.**
   Authors subclass a base class, set `source_class` + `stream_config`,
   and inherit a battery of contract tests. Ships with the main
   package, so a plugin that passes today's kit will keep passing
   until we bump the contract.

## Decision

Option 3. Kit lives at `sources/testkit/` and exports two names:

- **`SourceConformanceTests`** — pytest base class. Each `test_*`
  method encodes one line of the contract. Authors override
  `source_class`, `stream_config`, and (optionally) `build_source()`
  to inject mocks. All contract tests then run against the plugin
  under pytest's normal collection.
- **`FakeHTTPTransport`** — an `httpx.MockTransport` with URL-prefix
  registration and call recording. Authors don't need to learn httpx
  mock internals to feed deterministic fixtures.

**Test coverage the kit ships with:**
- Manifest exists at module level and is a `SourceManifest` instance.
- `plugin_id` matches `Source.name`.
- `fetch_since` returns an iterator, not a materialized list.
- Every yielded `RawItem` has non-empty `url`, non-empty `external_id`,
  and `source == Source.name`.
- Every `created_at` is timezone-aware UTC (CLAUDE.md MUST rule).
- Cursor advances when items are yielded; stays `None` when empty.
- `FetchStats.ceiling_hits` is a well-shaped list.
- **`fetch_since` is safe to invoke concurrently on one instance**
  (per ADR-0023). Two threads driving the same source must produce
  disjoint external_id sets and a monotonically-advancing cursor.

**Applied to `sources/hn.py`** as the reference example
(`tests/test_hn_conformance.py`). Passes 12 of 12 conformance tests
via `FakeHTTPTransport`, plus a meta-test asserting the mock was
actually invoked (guards against a refactor that silently bypasses
the transport and hits the real API).

## Rationale

- **Tests-as-contract is auditable.** A plugin author runs one pytest
  command and gets pass/fail. No prose to interpret.
- **Ships with the codebase.** Kit imports `sources.base` types
  directly, so contract drift between `base.py` and the kit is a
  compile error, not a documentation problem.
- **Concurrency belongs in the kit.** Once ADR-0023 flipped
  `fetch_concurrency_enabled` on, the concurrent-fetch invariant
  became part of the contract. Anywhere else and it would rot.

## Consequences

**Easier:**
- Accepting a PR that adds a new source: run the kit, review the diff.
  No "I hope this handles rate-limiting."
- Evolving the contract: add a test method, every plugin's next CI
  run flags what needs updating.

**Harder:**
- The kit becomes an API surface. Breaking changes to
  `SourceConformanceTests` mean plugin authors adjust. Version bumps
  should land alongside a plugin-facing changelog entry.
- Fixtures for API-based sources (praw, github with auth) require
  authors to supply mocks. Kit provides `FakeHTTPTransport`; SDK-based
  sources need per-source guidance in PLUGIN_AUTHORS.md.

## Revisit if

- A whole plugin class can't be reasonably tested with the current
  kit shape (e.g., streaming websocket sources). Then add an
  alternative base class rather than bending `SourceConformanceTests`.
- The concurrent test flakes on slow CI. Then bump the sleep budget
  or gate it behind `pytest -m concurrent` — but don't remove it,
  the invariant it enforces is load-bearing.
