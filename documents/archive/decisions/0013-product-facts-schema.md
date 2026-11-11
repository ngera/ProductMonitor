# ADR-0013: Product facts (aliases / goals / competitors / scope) as first-class product.yaml fields

- **Status:** Accepted
- **Date:** 2026-07-25
- **Area:** data

## Context

The wizard-redesign plan (`documents/wizard_redesign_plan.md`) reframes product
creation around *facts* the user supplies (name, aliases, confusables, goals,
competitors, scope bullets) instead of *machinery* (prompts, taxonomy essays,
vendor lists). Wizard v2 asks the user for these facts on Screen 1 and shows
them again on Screen 2 as an editable profile. To do that, the product model
has to *have* these fields — not just the wizard's draft state, but the
persisted product on disk.

Before this change, the only places these concepts existed were:
- `product.yaml.description` (a single freeform paragraph)
- `prompts.yaml` templates (where "in scope" advice lived inside long
  prose blocks per feature)
- `vendors.yaml.vendors[]` (where competitors would have gone, mixed in
  with genuine vendors used for entity extraction)

None were structured enough for the wizard to reason about, and none were
consumed by the relevance stage.

## Options considered

1. **Keep facts only in the wizard draft.** Cheap; wizard writes the
   templates on materialize and forgets the facts. Con: the *product* itself
   never learns its own aliases; there's no way to edit them post-creation
   without reopening the wizard; every prompt-suggestion regen has to re-derive
   scope from prose.
2. **New sibling file `facts.yaml`.** Clean separation. Con: another file on
   disk to load, back up, and diff; nothing else lives at that layer of the
   spec and it complicates the "one product = one dir" mental model.
3. **First-class fields on `product.yaml` (chosen).** Additive optional keys;
   defaults keep every existing product loading unchanged; one file to edit;
   `ProductSpec` exposes them directly so relevance/classify/wizard/UI all
   read from the same source of truth.

## Decision

Add these optional fields to `product.yaml` and `ProductSpec`:

```yaml
url: ""                       # product website
aliases: []                   # names the product is known by
not_to_be_confused_with: []   # confusables to explicitly exclude
goals: []                     # subset of: bugs, feature_requests, sentiment,
                              #            competitor_compare, churn_signals
competitors: []
scope_in: []                  # short bullets
scope_out: []
```

- Every field is optional with an empty default. Legacy `product.yaml` files
  without any of these keys load unchanged.
- `pipeline.product.validate_facts()` centralizes validation: goals must be
  in `VALID_GOALS`; every list is capped at `MAX_FACTS_LIST_LEN` (20).
- `scaffold_product()` accepts an optional `facts` dict and merges it into
  `product.yaml`. If `facts.aliases` is set, the seed HN stream's
  `search_queries` are extended to `[display, *aliases]`.
- `save_product_facts()` writes the facts block in isolation (does not touch
  display / description / extras_* / schedule / time_range).
- `pipeline.product_facts_prompt.render_product_facts_block()` produces a
  short "PRODUCT CONTEXT" block wrapping each user-controlled value in
  `<user_input>` tags per SECURITY.md §2. Both `relevance._render_prompt` and
  `classify._build_prompt` inject it: via the `{product_facts_block}`
  placeholder if the template exposes one, else prepended to the user prompt.
  The system prompt gets `SYSTEM_PROMPT_SAFETY_PREAMBLE` when facts are
  present so the LLM knows tagged content is data, not instructions.
- `competitors` merges into the classifier's `vendor_hits` list
  (case-insensitive dedupe, regex hits first) so the LLM checks for
  competitor mentions alongside the regex-detected vendors — without
  changing the classification schema.

## Rationale

Facts belong to the product itself, not to the wizard's ephemeral draft.
Once we accept that, `product.yaml` is the least-surprising home: it already
holds identity + wiring for the product, all other spec files reference it
by convention, and adding optional keys is the additive change ADRs 0006 and
CLAUDE.md both bless. Feeding the facts into relevance/classify (instead of
only storing them for the wizard's benefit) makes the schema pay for itself
immediately — aliases and confusables directly improve gate accuracy on the
first run.

## Consequences

**Easier:**
- Wizard v2 can render the profile screen straight from `ProductSpec` fields.
- Prompt-suggestion regen (§4.5) can reason about scope from `scope_in` /
  `scope_out` structured lists instead of parsing prose.
- Facts are visible in `git diff` when someone hand-edits the product.

**Harder / locked-in:**
- Adding a *new* fact field (say, `industry`) is now a schema decision. The
  precedent is set: add it to `product.yaml`, `ProductSpec`, and
  `validate_facts()` together.
- The `<user_input>` prompt safety wrap becomes load-bearing for scope
  content. If we ever remove the wrap, malicious user text in `scope_in`
  could poison the system prompt.
- `competitors` semantics are dual-purpose: they participate in the
  classifier's vendor pre-pass. If we later add a dedicated "competitor
  extraction" pipeline, that path should read from `competitors` directly
  rather than duplicating the field.

## Revisit if

- We add multi-tenant products where facts differ per tenant (would push
  facts back into a per-tenant overlay layer).
- Someone wants to store much longer scope prose (paragraphs, not bullets)
  — that would blow through the 20-item cap and push us toward a separate
  free-text scope file.
- We build a "product facts" API that needs versioning (would require
  adding a `facts_version` hash similar to `taxonomy_version`).
