# ADR-0009: Evals scope is end-to-end classify output only, not per-stage

- **Status:** Accepted (implemented in `9690f70`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** data (evaluation)
- **Plan reference:** [POST_V1_PLAN.md §4.10](../POST_V1_PLAN.md), D14
- **Supersedes:** none (revises v0.2 §4.10 which proposed per-stage evals)

## Context

The v0.2 plan proposed evaluating each pipeline stage independently:
filter, relevance, classify, group each get their own precision/recall
against the golden set.

Critical review of v0.2 identified a fundamental mismatch:

- **Snippets don't carry per-stage expectations.** A snippet says "this
  post is positive/negative for this product." It doesn't say "the
  filter stage should keep this" or "if it survives filter, classify
  should assign it to `audio`."
- **Cross-stage effects are silent.** If filter drops item X, the
  relevance stage never sees X. Filter's eval says "recall −1" (we
  dropped a labeled-relevant item). But relevance's eval is silent
  about X entirely.
- **Per-stage metrics don't compose.** F1 on relevance × F1 on classify
  doesn't give F1 on the final report. Users don't care about relevance
  in isolation; they care about "what shows up in my report."

## Options considered

1. **Per-stage evals (v0.2 proposal).** Score filter, relevance,
   classify, group each on their own metric. Rich signal in theory;
   requires snippets to carry per-stage labels
   (`expected_filter_decision`, `expected_primary_area`, etc.).
   Breaking schema change; large labeling burden.

2. **End-to-end evals only.** Score the pipeline's final classify
   output — primary_area, content_types, sentiment, severity, etc. —
   against the golden set. Simple; matches how reports are read.

3. **Both.** End-to-end plus per-stage where snippet labels support
   it. Best of both worlds; largest complexity budget.

## Decision

Option 2. End-to-end classify output only. Metrics against golden set
per [DESIGN.md §7.3](../DESIGN.md#L619):

- Primary-area accuracy (single-label)
- Content-types F1 (multi-label)
- Sentiment 3-class accuracy + sign agreement
- Severity F1 (when labeled)
- Entities F1 (when labeled)
- KB / build extraction F1 (when labeled)

For each metric: 95% bootstrap CI. Regression rule: >5pp drop AND CI
on delta excludes zero.

Per-stage debugging remains available via the existing per-stage
snapshots (`data/<pid>/temp_runs/<run_id>/<stage>.jsonl`) — users can
inspect what each stage did to a specific item, but no automated
per-stage metric.

## Rationale

- **Well-defined at the golden set.** Snippets say what an item's
  final classification should be. That aligns exactly with end-to-end
  metrics.
- **Matches how reports are read.** Users read the classified output;
  they don't parse per-stage traces. The eval should measure what the
  user sees.
- **Actionable regressions.** "Content-types F1 dropped 8pp" is
  concrete: bad LLM change or prompt regression. "Filter recall
  dropped 3pp" is ambiguous — could mean filter tightened, or upstream
  fetch caught more marginal items.
- **No breaking schema change.** Snippets stay as they are. Users
  don't have to re-label anything.
- **Per-stage snapshots already exist.** For debugging a specific
  item's journey, users use the stage-snapshot UI, not automated
  per-stage F1.

## Consequences

**Makes easy:**
- Bootstrap CI on end-to-end metrics is well-defined.
- Adding a new eval metric = adding a Pydantic field to the golden-set
  schema. No stage-wiring.
- Regression detection is a single delta computation per metric.
- Users understand what the score means.

**Makes hard or expensive to reverse:**
- Users who want to know "which stage caused this regression" have to
  investigate manually via the per-stage snapshot UI. Acceptable trade
  for a personal tool; would be a real problem for a large team.
- Some pipeline errors (e.g., filter suddenly dropping way more items)
  won't show up as an end-to-end regression if the dropped items
  weren't in the golden set. Compensating: the fetch-and-filter
  counters already surface item-count changes prominently on the run
  detail page.

**Locks us into:**
- The v0.2 per-stage-labeled snippet schema is off the table. Bringing
  per-stage evals back later would require the schema change we're
  deferring.

## Revisit if

- Users routinely ask "which stage caused the regression?" as a
  first-order question (evidence: repeated debugging patterns).
  Reconsider per-stage evals with the schema change.
- The eval system grows into a serious ML ops function where per-stage
  attribution has known industry value. Reconsider.
- A pipeline stage becomes complex enough that per-stage validation
  is genuinely required (e.g., filter grows heuristics that need
  targeted testing). Then that stage gets a dedicated eval, but as a
  targeted unit test, not a general framework.
