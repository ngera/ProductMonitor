# ADR-0030: Stream auto-discovery — LLM queries + provider-native search

- **Status:** Accepted
- **Date:** 2026-09-17
- **Area:** infra / frontend / plugins

## Context

Every source plugin that accepts per-stream identifiers (subreddit for
Reddit, repo for GitHub Issues, app_id for Apple App Store, feed_url
for RSS, etc.) currently forces the operator to type those identifiers
into a wizard textarea. The wizard has an assistant-LLM suggester
([pipeline/stream_suggestions.py](../../pipeline/stream_suggestions.py))
that proposes 3-8 candidate identifiers up-front, but the operator
still has to review each suggestion and check its box before advancing.

Two problems:

1. **Hallucination risk.** The LLM proposes subreddit names purely
   from the product profile. If the LLM guesses `r/notionable` and
   that subreddit doesn't exist (or is unrelated to the product Notion),
   the operator sees a checkbox for a phantom sub. Silent quality
   drift on wizard completion.

2. **No ongoing signal freshness.** Once the wizard commits a source's
   streams to `products/<pid>/sources.yaml`, no code path ever
   revisits the choice. If a new subreddit becomes relevant six months
   later (product ships a feature, community forms a dedicated sub),
   nothing surfaces it. Operator has to notice and manually add.

The operator asked: "Can the system search and find relevant subreddits
on its own?" The refined design they proposed — LLM generates 3-4 word
search *queries*, then those queries feed a provider-native search API
whose results are the candidate set — cleanly solves problem #1 by
grounding every candidate through the provider's own data. Adding a
weekly review pass on top solves problem #2.

## Options considered

1. **Auto-accept the current LLM suggestions.** Wizard skips the
   confirmation UI. Cheapest to build. Rejected — still trusts the
   LLM's picks blindly; still frozen at wizard time; still misses
   subreddits the LLM didn't think of; hallucination-proof this is not.

2. **Reddit-search-only (no LLM).** Query Reddit's subreddit search
   directly against product.display + aliases. Deterministic. Rejected
   — keyword-only matching gives narrow results (`r/notion` hits, but
   `r/producthunt` where Notion is heavily discussed doesn't). Misses
   the semantic reach the LLM provides.

3. **LLM-generated queries → provider-native search → merge → user
   approves.** The operator's proposed shape. LLM produces 3-4 word
   search terms from the profile facts; every candidate comes from
   the provider's real data (guaranteed to exist); operator reviews
   the merged/ranked list. Generalizes to any source with a
   provider-native search endpoint.

4. **Continuous discovery + human approval** (adds on top of #3).
   Weekly re-discover pass; diffs against current config; new
   candidates surface on the product page as pending suggestions
   for the operator to approve. Never silently adds.

## Decision

**Option 3 for initial config + Option 4 for ongoing freshness.** These
compose — 3 covers wizard-time and product-page "Discover more" clicks;
4 runs weekly and files new candidates for review.

### Plugin contract

New optional method on `sources.base.Source`:

```python
def discover_streams(
    self,
    profile_facts: dict[str, Any],
    max_candidates: int = 8,
) -> list[StreamCandidate]:
    """Provider-validated stream candidates. Default returns []."""
    return []
```

`StreamCandidate` is a small dataclass:

```python
@dataclass
class StreamCandidate:
    stream_config: dict[str, Any]   # {"subreddit": "windows11"} etc.
    display_name: str               # "r/windows11"
    rationale: str                  # one-line justification
    quality_signal: str = ""        # "2.3M subscribers"
    provider_url: str = ""          # link the operator can click
```

Default `discover_streams()` returns `[]` — plugins that support
discovery override. Plugins that can't (RSS with no universal search,
paid APIs without free search) keep the default; wizard falls through
to the current manual-textarea path for them.

### Query generation

A new assistant-LLM prompt (`assistant_stream_search_queries`)
generates 3-8 three-to-four-word search queries per (plugin, profile).
Response schema is a plain list of strings. Cached per
`(profile_hash, plugin_id)` for a week — same profile, same plugin,
same queries.

Prompt shape:

```
Given a product profile, generate 3-8 short search queries (3-4
words each) suitable for finding relevant <plugin_id> streams. Each
query should be specific enough that a keyword-based provider search
returns tightly-scoped results. Prefer product-specific terms over
generic industry terms.

Product: {display}
Aliases: {aliases}
Description: {description[:400]}
Scope in: {scope_in}
Scope out: {scope_out}

Return only the JSON array of query strings, no prose.
```

### Provider search + rank

Each `discover_streams()` implementation:

1. Calls the query-generation service.
2. Executes each query against the provider's native search API.
3. Dedups results by identifier.
4. Ranks by a provider-native quality signal (subscriber count for
   Reddit, star count for GitHub, review count for Apple App Store).
5. Filters (min quality threshold, exclude NSFW, exclude archived).
6. Truncates to `max_candidates`.

### Wizard integration

Step 3 configure phase becomes uniform. For every enabled source:

- Call `plugin.discover_streams(profile_facts)`.
- Non-empty return → render the "we found these — pick which to
  include" checklist. Every candidate shows `display_name`,
  `rationale`, and `quality_signal`. Operator can uncheck defaults,
  add more via a textarea fallback for anything discovery missed.
- Empty return → fall through to the current textarea path (unchanged
  behavior for RSS / StackEx / paid sources).

Every checked candidate becomes a stream in `stream_config`.

### Weekly review loop

New pipeline stage `discover_streams` (or a separate scheduled job)
runs weekly per product:

1. For every enabled source, call `plugin.discover_streams()`.
2. Diff against current `products/<pid>/sources.yaml` streams.
3. Anything new → append to `data/<pid>/pending_stream_suggestions.jsonl`
   with a `suggested_at` timestamp.
4. Product page Summary tab gets a "Suggested new streams (N)"
   callout when the file is non-empty. Operator reviews, approves
   (moves to sources.yaml) or dismisses (marks the suggestion
   `dismissed_at`).
5. Auto-expire dismissed + un-actioned suggestions after 4 weeks so
   the file doesn't accumulate stale data.

### Conformance kit assertion

`sources/testkit/conformance.py` grows a test that verifies
`discover_streams()` — if the plugin overrides the default — returns
`StreamCandidate` instances with non-empty `stream_config` and
`display_name`. Skips cleanly when the plugin uses the default `[]`.

## Rationale

- **Grounding.** Every candidate exists in provider data. No
  hallucinated subs.
- **Reach.** LLM query generation captures semantic proximity that
  keyword search alone misses ("productivity software" query surfaces
  subs the product name wouldn't).
- **Deterministic verifiability.** Same profile → same queries → same
  provider results → same candidates (within provider API stability).
- **Uniform contract.** Any source can opt in by implementing one
  method. Sources that can't (RSS, paid APIs) stay on the manual
  path with zero code change.
- **Continuous freshness without silent additions.** Weekly loop
  finds new candidates; nothing enters `sources.yaml` without an
  operator click.

## Consequences

**Easier:**
- Wizard-time onboarding: operator ticks a "Auto-discover streams"
  checkbox (default on for discovery-capable sources) and reviews a
  short well-scoped list rather than typing subreddit names blind.
- Ongoing freshness: the product stays current on where its users
  are actually talking, without the operator manually auditing.
- Cross-source consistency: same UX shape for Reddit, GitHub Issues,
  Apple App Store, Product Hunt, YouTube — anything that ships a
  free search API.

**Harder:**
- Wizard latency: each discovery-capable source adds ~2-4s (LLM call
  + provider search). A wizard with 5 discovery-capable sources adds
  ~15s. Mitigable with parallelization + caching.
- LLM cost: 1 query-gen call per source per fresh wizard, plus the
  weekly loop. Estimate: <$0.001/product/week at Haiku prices.
  Cheap; not free.
- Ongoing suggestion review is a new maintenance surface. Product
  page grows a callout. Operators who never look at it accumulate
  suggestions — expire policy is the mitigation.
- New per-source code (each plugin adds ~50-80 lines).

## Consequences (limitations)

- **Query quality decides everything.** If the LLM produces
  "developer tools ecosystem" as a query, Reddit returns 20 marginally-
  relevant subs and rank-by-subscribers can't fix it. Prompt template
  needs iteration + a small eval snippet set for the query-gen prompt.
- **Reddit's subreddit search is keyword-only.** Semantic subs (r/pcmasterrace
  discussing Windows 11 without "Windows" in name/description) can be
  missed. Mitigation: the LLM can generate community-name queries
  ("gaming pc community", "power user forum") that surface those.
- **Rate limits at scale.** Reddit's public search allows ~100/min
  unauthenticated. Wizard-time discovery for a single new product is
  fine; the weekly loop across many products needs pacing.
- **Sources without a free search API** (X/TikTok via ScrapeCreators,
  potentially others) don't benefit. The manual textarea remains.

## Implementation sequence

1. **ADR-0030 accepted** (this file). ✔
2. **Plugin contract** — `StreamCandidate` dataclass, default
   `Source.discover_streams()` returning `[]`. `sources/base.py`.
3. **Query-generation service** — `pipeline/stream_query_generation.py`
   exposing `generate_search_queries(profile_facts, plugin_id) →
   list[str]`. Uses the assistant LLM contract; caches per
   `(profile_hash, plugin_id)` on disk under
   `data/.discovery_cache/queries/<hash>.json`.
3. **Prompt template** — `assistant_stream_search_queries` registered
   in `pipeline/prompt_templates.py`.
4. **Reddit implementation** — `sources/reddit.py::discover_streams()`.
   Uses `reddit.com/subreddits/search.json?q=...` (public read),
   dedups, filters NSFW / < 1k subs, ranks by subscribers × description-
   keyword-match, truncates to `max_candidates`.
5. **Wizard Step 3 rewiring** — call `discover_streams` for each
   enabled source; render candidates as checklist with quality signals;
   keep textarea fallback for empty results / operator additions.
6. **Weekly loop** — new stage or scheduled job that re-runs discovery,
   diffs against current streams, files pending suggestions.
7. **Product page callout** — Summary tab renders pending
   suggestions with approve/dismiss buttons.
8. **Second-plugin proof point** — `sources/apple_appstore.py::discover_streams()`
   using iTunes Search API. Validates the contract generalizes.
9. **Conformance kit** — one new test asserting overriding plugins
   return well-shaped candidates.

## Revisit if

- Query quality issues prove chronic. Options: (a) two-pass generation
  (LLM generates queries, then LLM re-ranks results); (b) fine-tune
  the prompt with a small eval set of expected-vs-actual candidates;
  (c) allow the operator to edit generated queries and re-run search.
- Reddit's public subreddit search API changes shape or gets
  auth-required. Fallback: use praw's `subreddits.search()` which
  goes through OAuth (Reddit source already requires OAuth).
- The weekly loop generates too many false-positive candidates.
  Auto-expire aggressively; consider per-source suggestion caps
  (max 3 pending suggestions per source at any time).
- A plugin needs SEVERAL search endpoints (e.g., HN Algolia's user +
  story search). Extend the contract to allow a plugin to return
  candidates from multiple internal calls, aggregated.
