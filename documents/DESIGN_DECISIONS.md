# Key design decisions

Public summary of choices that shape **cost**, **performance**, and
**operator trust**. Full ADR history (local archive) lives under
`documents/archive/decisions/` on developer machines.

## Local-first, single tenant

All product data stays on disk (`data/<product_id>/`). The UI binds to
`127.0.0.1` by default. There is no telemetry phone-home and no shared
cloud warehouse. Outbound hooks (e.g. run webhooks) are **opt-in** and
must never fail the pipeline if they fail.

**Why it matters:** feedback text never leaves your machine unless you
chose a hosted LLM or a source API you configured. Compliance and cost
both stay under your control.

## Raw JSONL as system of record

Fetch writes immutable raw files. Normalize and every later stage can
replay from those files (`--skip-fetch`). Re-tuning prompts or taxonomy
does not require re-hitting Reddit/RSS/GitHub.

**Why it matters:** cut API quota spend when iterating; failed LLM
stages can be retried without re-fetching.

## Two LLM budgets: pipeline vs assistant

- **Pipeline LLM** — per product, per stage (`llm_routing.yaml`). Use a
  small/cheap/local model for high-volume relevance + classify.
- **Assistant LLM** — one global connection for wizard drafting,
  headlines, and other infrequent “smart” passes.

**Why it matters:** the expensive model is not billed on every item;
batch stages stay cheap.

## Structured outputs + prompt caching

Classification and related calls use **structured contracts** (JSON /
tool schemas) with bounded retries on parse failure — fewer wasted
tokens on malformed answers.

Where the provider supports it, **prompt caching** keeps stable system
and few-shot prefixes warm; cache hits show up as
`cached_input_tokens` in usage tracking.

## Cache-first headlines

Digest headlines are keyed by
`sha256(content + prompt + model)`. Re-rendering a week does not
re-call the model when content and prompt are unchanged.

## Relevance before classify

A dedicated **relevance** stage (plus a cheap context/needle pre-gate
from product themes) drops off-topic items before the heavier classify
prompt runs.

**Why it matters:** most of the bill in a noisy week is irrelevant
traffic; gating early multiplies savings.

## Keyless-first sources

Default onboarding prefers sources that need no API key (HN, RSS media
catalog, Reddit RSS, …). Hosted LLM keys are opt-in.

**Why it matters:** time-to-first-report stays minutes, not an afternoon
of OAuth apps — and zero LLM spend is a valid mode (fetch → filter only).

## Feature flags off by default

New user-visible capabilities ship behind flags in
`config/features.yaml`, default **off**, so upgrades do not surprise
operators or suddenly enable embedding/torch stacks.

(Exception: a narrow set of read-only admin telemetry surfaces that
cannot spend money.)

## Bounded concurrent fetch

Streams fetch in parallel with a global concurrency cap and a
**per-host** semaphore (defaults are conservative). Retries use shared
backoff for flaky HTTP sources.

**Why it matters:** wall-clock fetch drops without hammering a single
publisher or getting banned.

## Canonical URL dedup

Normalize collapses the same article/thread seen via multiple feeds
into one item where URLs canonicalize equal.

**Why it matters:** less double-counting in digests and less duplicate
LLM work.

## Digest v2 as the sole report

One static HTML digest per week replaces a scatter of per-area pages.
Attribution partials are required in every item-displaying template.

**Why it matters:** operators have one artifact to open and share;
provenance stays honest.

## Token attribution everywhere

Every LLM call site records usage into the warehouse via a
contextvars-based **TokenContext**. Admin → Tokens shows cost by
product, stage, and provider.

**Why it matters:** you can see which stage and which product burn
budget — silent calls are treated as bugs.

## Plugin-shaped sources

New sources are plugins (manifest + `fetch_since`), not core forks.
Drop-ins require an explicit trust path; pip entry points are consent
via install.

**Why it matters:** the core stays small; custom enterprise sources do
not fork the pipeline.

## What we deliberately skip

| Non-goal | Rationale |
|---|---|
| Multi-tenant SaaS | Complexity and data boundary clash with local-first |
| Real-time streaming | Week-cadence digests match PM workflows and batch LLM pricing |
| Always-on outbound sync | Failure domains and surprise egress |
| One LLM for everything | Cost and quality tradeoffs differ by stage |

For how these pieces fit together day to day, see
[Architecture](ARCHITECTURE.md) and [Setup](SETUP.md).
