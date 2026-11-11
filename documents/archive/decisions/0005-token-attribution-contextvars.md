# ADR-0005: Token attribution via contextvars, warehoused in llm_usage

- **Status:** Accepted (implemented in `d907145`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** infra
- **Plan reference:** [POST_V1_PLAN.md §4.11](../POST_V1_PLAN.md), D10, D21

## Context

V1 makes LLM calls but doesn't record token counts. Users have zero
visibility into which stage or which source contributed to their bill.
Post-V1 adds three more LLM use cases (wizard, snippet candidates,
prompt suggestions), each with independent cost dynamics. Without
attribution, users can't answer "why did last month's bill spike?"

Attribution needs three fields per call: `run_id` (which pipeline run
it belonged to; empty for global assistant work), `stage` (which
pipeline stage or assistant use case), `source_id + item_id` (which
piece of content the call was about).

## Options considered

1. **Pass context explicitly through the LLMClient interface.** Every
   caller supplies `LLMCallContext` as an argument. Explicit, testable,
   works everywhere. But: touches every call site. Nested calls (e.g.,
   the classifier internally calling a helper LLM) require threading
   the context through. Verbose.

2. **`threading.local` for context propagation.** Set once at run
   start; every LLM call reads it. But: breaks under `asyncio`
   (contextvars work, threadlocals don't propagate correctly);
   breaks under worker pool patterns; nested calls that switch threads
   see wrong context.

3. **`contextvars.ContextVar`.** Same "set once, read everywhere"
   ergonomics as threadlocals, but works correctly under async, nested
   calls, and threads. Standard Python since 3.7.

## Decision

Option 3. Attribution context propagated via `contextvars.ContextVar`.
`LLMClient.call()` reads the current context at call time. Recorded to
a new warehouse table:

```sql
CREATE TABLE llm_usage (
  ts                 TIMESTAMP,
  run_id             VARCHAR,   -- empty for assistant work
  stage              VARCHAR,   -- relevance | classify | assistant_wizard | ...
  source_id          VARCHAR,   -- empty for cross-source work
  item_id            VARCHAR,   -- empty for aggregate work
  endpoint           VARCHAR,
  model              VARCHAR,
  prompt_tokens      INT,
  completion_tokens  INT,
  cached_input_tokens INT,      -- for providers with prompt caching
  total_tokens       INT
);
```

Context set at run start:
```python
run_context.set(LLMCallContext(run_id=..., stage=..., ...))
```

Nested LLM calls that need a different stage push a new context onto
the stack (contextvars support this natively).

## Rationale

- **contextvars is the modern standard.** Async pipelines are a real
  possibility (some sources fetch concurrently); threadlocals would
  break silently. Better to build on the async-correct primitive from
  day one.
- **Explicit passing (option 1) is verbose.** ~50 call sites in the
  code today; adding a mandatory context param would touch every one.
  contextvars keeps the LLMClient interface clean.
- **Warehouse-first attribution.** A DuckDB table is cheap, easy to
  query, and integrates with the existing snapshot / eval / trend
  dashboards. JSONL alternative would require ad-hoc parsers.

## Consequences

**Makes easy:**
- All four aggregate views (per-run, per-source, per-product,
  per-assistant-LLM) computable from one SQL table.
- Async pipelines "just work" — no attribution drift.
- Adding new attribution dimensions (e.g., `product_id`, `week_id`)
  is an `ALTER TABLE` and a context-var addition.

**Makes hard or expensive to reverse:**
- LLMClient is now contextvar-aware. If we later want a truly stateless
  client (for multi-tenant server use), we'd need to route context
  through explicit args. Not a real concern for a personal tool.
- Every LLM call now writes a warehouse row. High-frequency calls
  (e.g., a run classifying 500 items) produce 500 rows. Table grows.
  Mitigation: partition by week (implied by `ts`) and archive/purge on
  a schedule (deferred).

**Locks us into:**
- The `llm_usage` schema. Adding fields is compatible; removing is not.
- ContextVar as the propagation mechanism. Migrating to explicit
  passing later would be a full-codebase edit.

## Revisit if

- We ever ship a multi-tenant deployment where request context needs
  to be truly stateless. Then explicit passing wins.
- ContextVar semantics change in a future Python version (they've been
  stable since 3.7).
- Volume of `llm_usage` rows becomes a performance concern (unlikely at
  personal-tool scale, real at multi-user scale).
