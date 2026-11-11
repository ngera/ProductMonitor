# ADR-0002: Assistant LLM is a separate global connection, not per-product

- **Status:** Accepted (implemented in `d907145`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** infra
- **Plan reference:** [POST_V1_PLAN.md §4.8](../POST_V1_PLAN.md), D1

## Context

V1's LLM routing is per-product: `products/<pid>/llm_routing.yaml`
picks relevance and classify endpoints/models. This works because a
product's pipeline stages are what use LLMs.

Post-V1 adds three new LLM use cases that aren't per-product-pipeline:
- Guided product setup wizard ([POST_V1_PLAN §4.3](../POST_V1_PLAN.md))
- Snippet candidate discovery ([POST_V1_PLAN §4.4](../POST_V1_PLAN.md))
- Prompt improvement suggestions ([POST_V1_PLAN §4.5](../POST_V1_PLAN.md))

Wizard runs *before* a product exists — chicken-and-egg with per-product
routing. Snippet + prompt work benefits from a strong reasoning model
even when the pipeline uses a cheap local one.

## Options considered

1. **Reuse the current product's classify LLM.** Zero new config
   surface. But: wizard has no product yet; snippet + prompt work would
   inherit whatever the user configured (often cheap local models);
   forces users to configure LLM routing *before* the wizard, defeating
   the wizard's purpose.

2. **New global assistant LLM connection.** One entry on `/connections`,
   configured once, used by wizard + snippets + prompt suggestions.
   Own budget cap. Own token attribution stage
   (`assistant_wizard`, `assistant_snippets`,
   `assistant_prompt_suggestions`). Adds ~1 day of connection UX.

3. **Free local model (Ollama/Foundry) for all assistant work.**
   Zero external cost. But: prompt engineering benefits significantly
   from strong models; a weak assistant produces bad prompts, which
   cascades into bad classification.

## Decision

Option 2. A new `assistant_llm` category in the plugin manifest
system, one global connection stored in `config/assistant_llm.yaml`,
with a per-product monthly budget cap in dollars.

## Rationale

- **Chicken-and-egg.** The wizard needs an LLM before per-product
  routing is set up. A global connection breaks the deadlock cleanly.
- **Different quality needs.** Pipeline classify LLM and assistant LLM
  optimize for different things (fast batch classification vs. thoughtful
  one-off generation). Users should pick different models when
  appropriate; forcing them to share means someone loses.
- **Cost visibility.** Assistant LLM work is bursty (setup, snippet
  review sessions) versus pipeline LLM work (per-run). Attributing them
  separately makes cost analysis meaningful.
- **Fallback exists.** If assistant LLM is not configured, dependent
  features fall back to "manual mode" (all forms, no LLM assist) with
  a banner. Nothing breaks; UX degrades.

## Consequences

**Makes easy:**
- Wizard works before the product's LLM routing exists.
- Users can pick a strong model (Claude Sonnet, GPT-4) for assistant
  work while keeping pipeline runs on a cheap local model.
- Budget-cap enforcement is clean (single spend surface per user).

**Makes hard or expensive to reverse:**
- Users now have to configure two LLM endpoints instead of one (they
  might reuse the same endpoint). Wizard shows a "detected same
  endpoint as your product's classify LLM — proceed?" hint to avoid
  confusion.
- If we later want per-product assistant LLM overrides (some products
  want cheap, some want strong), we'd need to add
  `llm_routing.yaml → assistant` as an optional override. Non-breaking
  extension.

**Locks us into:**
- Global assistant LLM is a top-level concept. Removing it later
  would require migrating features back to per-product routing —
  worth ~1-2 days of work.

## Revisit if

- Users routinely configure assistant LLM to the same endpoint as their
  per-product classify LLM (evidence: telemetry or asks). Suggests the
  separation is confusing.
- A strong per-product override becomes necessary (e.g., a paid
  customer wants an isolated assistant LLM for compliance reasons).
- Wizard usage drops toward zero and we drop the wizard; assistant LLM
  usage collapses to just snippets + prompt suggestions, which could
  reasonably reuse the per-product classify LLM.
