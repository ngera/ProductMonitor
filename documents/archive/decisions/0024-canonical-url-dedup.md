# ADR-0024: Cross-source canonical-URL dedup at normalize

- **Status:** Accepted
- **Date:** 2026-09-15
- **Area:** data

## Context

Item identity is `source:external_id`. Deduplication happens in two places
today, both scoped to a single source:

- **Fetch time.** `state.seen_ids` is keyed on `(source, external_id)` —
  answers "did we already fetch this from THIS feed."
- **Normalize time.** `items.id` is `{source}:{external_id}` — natural PK,
  never collides across sources.

Wire syndication defeats both. An article on a company blog picked up by
Ars, cross-posted to HN, and shared to r/programming becomes four items:

| source | external_id     | url                                    |
|--------|-----------------|----------------------------------------|
| rss    | ars-12345       | https://arstechnica.com/…?utm_source=… |
| hn     | 987654          | https://arstechnica.com/…              |
| reddit | t3_abc          | https://arstechnica.com/…?ref=reddit   |
| rss    | ars-12345       | https://arstechnica.com/…              |

Each flows through Filter, Relevance, Classify (four LLM passes per stage
instead of one), all four land in the digest under different bylines,
each with slightly different scoring based on Reddit upvotes vs HN karma.
The report reads as though four different people all wrote about the same
thing this week.

The problem has been latent while sources were mostly primary (Reddit
comments, GitHub issues — each source's items really ARE unique to that
source). The Post-V1 wave added RSS and media-coverage sources, which
routinely republish primary reporting → syndication became common.

## Options considered

1. **Change `items.id` to a URL hash.** Cleanest identity story, but
   breaks the "stable cross-cutting IDs" MUST rule in CLAUDE.md — every
   FK from `item_classifications`, `item_areas`, `bug_attributes`,
   `scores`, `item_areas` etc. would need a migration. Massive blast
   radius for the actual problem, which is "don't show duplicates in
   the digest."
2. **Cluster at group time via embeddings.** Detect near-duplicate
   content post-classification and merge. Powerful but heavy — adds a
   sentence-transformers dep on the hot path, and false positives
   (same topic, different articles) become likely. Also treats a
   symptom, not the cause.
3. **Additive column + collapse at normalize.** Compute a canonical URL
   from `items.url`, store it as an optional field, collapse groups
   BEFORE the LLM stages see them. Additive schema change, deterministic
   rule, easy to reason about and test.

## Decision

Option 3.

**Schema:**
- `items.canonical_url VARCHAR NULL` added additively.
- `idx_items_canonical_url ON items(canonical_url)` for lookup.
- `items.id` stays `source:external_id` — never renamed, never migrated.
  The canonical URL is metadata, not identity.

**Canonicalization** — `pipeline/canonical_url.py`:
- Lowercase scheme and host; drop default ports (`:80`, `:443`).
- Strip fragment (`#anchor`).
- Remove a targeted list of tracking params (`utm_*`, `fbclid`, `gclid`,
  `mc_cid`, `ref`, `ref_src`, `msclkid`, `fbclid`, `dclid`,
  Substack/Reddit/YouTube share tokens, HubSpot params).
- Sort remaining query params — trackers hide in unsorted order too.
- Strip trailing slash on path (unless path IS `/`).
- Non-http(s) schemes return `None` (no attempt to canonicalize).
- Malformed input returns `None` — falls back to per-source identity.

**Dedup at normalize** — `pipeline/normalize._dedup_by_canonical_url`:
- Only NON-reply items with a non-NULL `canonical_url` participate.
  Replies bypass entirely because comment identity is per-thread, and a
  comment's URL can repeat across unrelated threads.
- Groups sharing a canonical_url are collapsed. Winner selection:
  1. Earliest `created_at` (the "original" publication, ahead of the
     syndicator).
  2. Source name alphabetically — deterministic tiebreak.
  3. `external_id` — final tiebreak.
- Every drop is logged as `dedup_dropped_canonical` with kept id,
  dropped id, and canonical URL. Silent dedup would make false
  positives impossible to diagnose.

**What does NOT change:**
- `seen_ids` stays keyed on `(source, external_id)`. It answers "did we
  fetch this before" per-feed; that's the right question for fetch-time.
- `items.id` stays `source:external_id`. Every FK in the warehouse
  depends on this shape.
- Per-source classification prompts, scoring, and grouping are unchanged.
  The winning row carries its natural provenance forward.

## Rationale

- **Coarse by design.** "Same canonical URL → same item" is a hard rule.
  It CAN be wrong (see Revisit), but wrongness is bounded — a false
  positive drops one item, logged, spot-checkable in the runs UI.
- **Reply-safe.** Excluding replies from dedup means the interesting
  primary-source data (Reddit comment threads, GitHub issue discussions)
  is never touched. Dedup only runs where syndication actually occurs.
- **Additive schema.** No FK rewrites, no historical rewrite, no
  operator action required. Existing warehouses get the column via the
  standard `_migrate_items_columns` path.
- **Debuggable heuristic.** "Earliest created_at wins" is easy to
  explain and easy to override manually if it produces a wrong pick —
  the operator can re-fetch or re-classify a specific item without
  rewriting the rule.

## Consequences

**Easier:**
- Adding a new syndication-heavy source (Feedly, Google News-style
  aggregators) doesn't multiply duplicates.
- Report scoring is more honest — an article that got picked up widely
  now scores once with real engagement, not N times with fractional.
- Cross-source citation of the same story is invisible to the operator
  by default; if they want to see it, drop the source from dedup by
  filtering `dedup_dropped_canonical` in the run logs.

**Harder:**
- False positives are possible. Two distinct pieces of content that
  happen to canonicalize the same (rare, but not impossible: a
  publisher that reuses URLs for different articles). The log line is
  the mitigation.
- The tracking-param list is a maintenance burden. New trackers appear;
  the list must grow with them. Not an ADR concern per revision — just
  a code change.

## Revisit if

- False-positive rate is meaningful (>1% of dropped items are
  legitimately-different content). Fix: add a title-similarity gate
  (e.g. shingled Jaccard over the first 200 chars of title+body)
  before the drop. Do NOT jump straight to embeddings.
- A URL-shortener source becomes primary. Short URLs (`bit.ly/xyz`,
  `t.co/xyz`) would need HEAD-resolution to unwrap before
  canonicalization; that adds network calls at normalize time. Consider
  a separate resolution stage before normalize rather than inline.
- Tracking-param drift outpaces the maintainer's ability to update the
  list. At that point, invert the model: allow-list load-bearing params
  per host rather than deny-list trackers globally.
