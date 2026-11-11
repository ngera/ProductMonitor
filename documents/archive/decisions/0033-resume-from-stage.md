# ADR-0033: Resume cancelled/failed runs from a pipeline stage

- **Status:** Accepted
- **Date:** 2026-09-19
- **Area:** infra / frontend

## Context

Operators cancel long classify/relevance runs (Ollama cost/time) after early
stages have already written durable warehouse state. Re-triggering a full
run re-pays fetch and LLM work already done. There was no way to continue
from the interrupted stage; `--skip-fetch` only skipped network fetch.

## Options considered

1. **Revive the same `run_id` / process** — Impossible after cancel writes a
   terminal JSON and kills the subprocess; run ids are immutable.
2. **Always full re-run with skip-fetch only** — Cheap to keep, wasteful for
   mid-classify cancels (re-runs relevance + all classify calls).
3. **New run with `--from-stage` + skip already-classified items** — New
   `run_id`, skip completed stages, classify only unfinished /
   `classification_failed` items. Resume button on the stopped run's detail
   page spawns that new run.

## Decision

Option 3. Cancelled/failed/crashed run detail pages get a **Resume** control
that starts a **new** UI run with `--from-stage=<inferred>` (and
`--skip-fetch` when the resume stage is after `fetch`). Classify skips items
that already have `item_classifications` rows unless `filter_status` is
`classification_failed` (those retry).

## Rationale

Warehouse state for filter/relevance/partial classify is already the source
of truth; a new run id keeps history honest. Inferring the stage from the
`.out` log matches what the operator sees on the run page.

## Consequences

Easy: recover from cancel without re-fetching or re-scoring relevance.
Hard: stage order and optional stages (`persistent_issue`, `digest`) must
stay aligned between CLI and the webui stage list; resume inference is
best-effort from logs. Feature is always available (no flag) — operator
action only, no new outbound behavior.

## Revisit if

Resume must continue under the **same** run_id for auditing, or mid-stage
checkpointing becomes finer than per-item classify rows.
