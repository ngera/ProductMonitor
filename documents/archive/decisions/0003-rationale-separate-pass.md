# ADR-0003: Report rationale runs as a separate on-demand pass, not during classify

- **Status:** Accepted (implemented in `f07157d`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** data
- **Plan reference:** [POST_V1_PLAN.md §4.7](../POST_V1_PLAN.md), D3, D4

## Context

Reports today show item title, source, sentiment, primary area, and a
snippet. Users want to know **why** each item was selected and **what**
it highlights.

The natural place to produce this is the classify stage — the LLM
already sees the item and produces labels. Adding two more output
fields (`rationale`, `highlights`) would be a small classify prompt
change.

But: report rendering is often done multiple times (weekly view,
different date ranges, PDF export). Rationale generated once at
classify time is stuck with whatever the model + prompt were at that
moment.

## Options considered

1. **Bake rationale generation into classify.** One LLM call per item
   covers classification + rationale + highlights. Slightly larger
   output schema; same call count as today. But: rationale is stuck
   with the classify-time model + prompt. Cannot regenerate for old
   runs without re-classifying. Loses independence between how items
   are classified and how they're explained.

2. **Separate on-demand pass at report render time.** Report render
   iterates items to include and calls LLM per item to generate
   rationale + highlights. Cached by
   `(item_id, classify_prompt_version, model_name)`. Existing runs can
   have rationale regenerated. Independent LLM choice possible (though
   we default to product's classify LLM per D4).

3. **User-triggered on-demand only.** No automatic generation. Reports
   render without rationale by default; user clicks "generate
   rationale" per item. Cheapest but worst UX.

## Decision

Option 2. Separate on-demand pass at report render time. Uses the
product's classify LLM by default (see D4). Cache key includes prompt
version and model name so stale cached rationale is regenerated when
classify inputs change.

## Rationale

- **Decoupled from classify.** Rationale prompt is tunable
  independently. Adding, removing, or reformatting highlights doesn't
  require a re-classify.
- **Regeneratable for old runs.** Reports rendered before this feature
  landed can be re-rendered with rationale after the feature ships.
  No re-classification needed.
- **Cache invalidates correctly.** Cache key includes the classify
  prompt version — if the classification changed (schema update, prompt
  change, LLM swap), the rationale cache miss forces regeneration.
  Prevents silently stale rationale.
- **Same cost as option 1** amortized: rationale runs once per item at
  first report render, then never again unless invalidated. Option 1
  runs once per item at classify time; option 2 runs once per item at
  first render. Both are one call per item per lifetime.

## Consequences

**Makes easy:**
- Retroactive rationale on old reports.
- Independent tuning of the rationale prompt.
- Cache eviction on prompt change (via the cache key).
- Regenerating for a single item on user request ("this rationale
  seems wrong — regenerate").

**Makes hard or expensive to reverse:**
- Two LLM calls per item over its lifetime (classify + rationale)
  instead of one. Only matters for archival purposes; ongoing runs
  hit the rationale cache.
- Report render stage is now slower on first render (per-item LLM
  call). Cache warmup mitigation: pre-generate rationale in the
  classify stage's "spare time" as an async job — deferred.

**Locks us into:**
- Report render must be able to reach an LLM. Currently render is
  deterministic; this makes it LLM-dependent. If the LLM endpoint is
  down at render time, rationale is skipped (fallback: summary field)
  and the report still renders.

## Revisit if

- Cache hit rate turns out to be low (classify prompts change more
  than expected). Suggests we should batch rationale generation with
  classify after all.
- Rationale quality varies wildly with the classify LLM (e.g., a
  cheap local model produces terrible rationale). We might carve out
  a dedicated `rationale` stage in `llm_routing.yaml` to override.
- Report render latency becomes a UX problem (first render on a
  200-item report = 200 LLM calls). Consider async batch generation.
