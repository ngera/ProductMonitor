# ADR-0034: Themes UI and context-derived relevance pre-gate

- **Status:** Accepted
- **Date:** 2026-09-20
- **Area:** frontend / data
- **Supersedes:** —

## Context

Products often author a 1:1 area↔feature taxonomy (Windows OS is five
themes, each with one feature that duplicates the area name). The
taxonomy editor exposed the full hierarchy plus keywords and entity
hints, which made operators edit the same concept twice. Separately,
the relevance stage gained a brand-only pre-gate (display / aliases /
URL host) to stop weak local models from marking general “AI” RSS as
relevant — but that gate also dropped real posts about product
surfaces that never name the brand (e.g. “Sound settings missing…” for
Windows).

## Options considered

1. **Keep area→feature editor; expand pre-gate with a parallel keyword
   list** — keywords already exist on areas for grouping tie-breaks.
   Pros: small code change. Cons: operators maintain a second keyword
   list that drifts from the recognition descriptions the LLM actually
   reads; keywords are the wrong source of truth.

2. **Flatten UX to Themes (name + description); keep area+feature on
   disk; build relevance context from facts + theme descriptions and
   derive the pre-gate from that same context** — Pros: one authored
   recognition text for both UI and gate/prompt; storage schema
   unchanged; Advanced still preserves multi-feature products.
   Cons: token extraction from descriptions can over-admit noisy
   common words (mitigated with stopwords + length ≥ 5).

3. **Drop the deterministic pre-gate; rely on the LLM only** — Pros:
   no false drops. Cons: already shown to fail-open on weak local
   models for Eleven Labs-style RSS noise.

## Decision

Ship option 2. The taxonomy form is a flat Themes editor that POSTs
the existing `areas`/`features` shape (1:1 by default; extra features
preserved under Advanced). Relevance prompts receive
`build_relevance_context()` (facts + enabled theme display/description
under `<user_input>`). The pre-gate uses
`relevance_context_needles()` from that same context (brand +
scope_in + theme tokens), still labeling drops
`dropped:no_product_mention`.

## Rationale

Feature descriptions were already the LLM recognition text; the
pre-gate should track that authored context rather than a parallel
keyword file. Flattening the form matches how operators think
(“themes”) without a schema migration.

## Consequences

- Theme description wording now affects both the LLM prompt and which
  items reach the LLM — operators should write concrete surface
  language (speakers, headphones) not only abstract category names.
- Multi-feature areas remain editable under Advanced; new themes
  default to 1:1.
- `{product_facts_block}` in existing prompts now substitutes the
  fuller context (facts + themes); `{relevance_context}` is an alias.

## Revisit if

- Pre-gate false-positive rate climbs (common English tokens from
  descriptions matching unrelated news) — tighten tokenization or
  require multi-token overlap.
- Operators need first-class multi-feature themes in the primary UI
  rather than Advanced preservation.
