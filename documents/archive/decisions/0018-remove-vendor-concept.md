# ADR-0018: Remove vendor as a first-class concept

- **Status:** Accepted
- **Date:** 2026-07-28
- **Area:** data / infra / frontend

## Context

Vendor was a first-class concept in three cooperating pieces:

1. **Config-driven regex pre-pass** — every product carried a
   `vendors.yaml` with `- canonical: <name>` entries. `pipeline.extract`
   compiled those into word-boundary regexes and injected any hits into
   the classifier prompt as `{vendor_hits}`.
2. **LLM-extracted vendor entity attribute** — every `Entity` the
   classifier returned carried `vendor: str`, persisted as an
   `entity_mentions.vendor` column that was part of the table's PK and
   backed two indexes. Reports and the digest read from that column for
   the "Top vendor" tile, per-area top-vendor rollups, and the digest's
   Competition table (mentions + sentiment vs. declared competitors).
3. **Wizard step + admin editor** — Wizard v1 had a whole "Key vendors /
   products" step, wizard v2 seeded `vendors.yaml` from the product's
   declared competitors, and `/products/{id}/vendors` had a dedicated
   admin form.

Three problems triggered the removal:

- **Schema drift bug.** Wizard v2 wrote `- name: X` while
  `pipeline/extract.py` expected `- canonical: X` — so *every*
  wizard-created product crashed at the classify stage with
  `KeyError('canonical')` on the first item, surfacing as
  `fatal: 'canonical'` in the run's error list.
- **Weak value.** For non-Windows products the vendor concept
  contributed little the LLM couldn't do inline. Regex vendor hints
  seeded a controlled-vocabulary that felt Windows/hardware-specific;
  the classifier could already name products/companies in `entities` and
  in its free-text `summary`.
- **Maintenance cost.** Vendor touched 20+ files across the pipeline,
  the wizard, the admin UI, the reports, tests, and the DDL. Every new
  product-shape decision had to reason about vendors even when they were
  irrelevant.

The competitor-tracking use case (how many people mention <competitor>
this week?) still matters, but it doesn't need a dedicated
config-driven vendor table — a substring search over item text against
`product.competitors` covers the same UX at digest render time.

## Options considered

1. **Keep the concept, fix the schema drift.**
   Cheapest patch — align wizard writers with the `canonical` key.
   Doesn't remove the surface area, doesn't address weak value, keeps
   the coupling between competitors + regex + LLM entities + storage
   PK. Rejected because we already had a signal from the user that this
   concept was carrying its weight poorly.
2. **Group 1 only** (rip vendors.yaml, regex pre-pass, `{vendor_hits}`,
   wizard step, admin form). Keep `Entity.vendor` and the
   `entity_mentions.vendor` column so the LLM can still attribute vendor
   per-entity. Small blast radius, no DB migration. Rejected because it
   would leave a half-alive concept.
3. **Groups 1 + 2** (also drop `Entity.vendor`, the
   `entity_mentions.vendor` column, top-vendor report tile, and rewrite
   competitor tracking as text search). What we did.

## Decision

Remove vendor as a first-class concept, everywhere:

- **Data model.** Drop `Entity.vendor`, `RegexExtractions.vendor_hits`,
  `Product.vendors`, `Product.vendors_version`.
- **Schema.** Drop `entity_mentions.vendor` (PK rebuild →
  `(item_id, type, product_key, role)`), `regex_extractions.vendor_hits`,
  `weekly_rollup.top_vendors_json`, `runs.vendors_version`, plus the
  `idx_entity_vendor` / `idx_entity_type_vendor` indexes. Ship a
  standalone migration at `scripts/migrate_drop_vendors.py` that
  ALTER-TABLEs / rebuilds existing warehouses in place; fresh installs
  use the updated DDL directly.
- **Pipeline.** Delete `_top_vendors_by_area`, `_global_top_vendor`,
  `_vendor_patterns`, and the vendor slot from the entity group-key
  (`entity:{area}:{type}:{product}`).
- **Prompts.** Drop `{vendor_hits}` from the scaffold and default
  templates; the classify entity spec no longer names `vendor`. Because
  existing per-product `prompts.yaml` files may still reference
  `{vendor_hits}`, the classify formatter uses `format_map` over a
  defaulting dict so removed placeholders silently render empty rather
  than raising `KeyError`.
- **Reports.** "Top vendor" tile removed from the legacy index. The
  digest's Competition section is preserved but rewritten to
  case-insensitive `LIKE` over `items.title || body` per declared
  competitor, so we retain per-competitor mention counts and average
  sentiment without a vendor column.
- **Wizard.** Vendors step removed from wizard v1;
  `VendorSuggestion` / `VendorsSuggestion` / `suggest_vendors` and the
  `_ASSISTANT_V1_VENDORS` prompt template are deleted. Wizard v2 no
  longer emits `vendors.yaml`.
- **WebUI.** `/products/{id}/vendors` GET+POST, the vendors card on the
  product page, the wizard's vendors block, the "vendors" admin
  raw-YAML editor entry, and `vendors_form.html` are all gone.
- **Config.** `config/vendors.yaml`, `products/*/vendors.yaml`, and the
  `_SCAFFOLD_VENDORS_YAML` template are deleted; `vendors_config()` and
  `vendors_version()` are removed from `pipeline.config`.

## Rationale

The bug that surfaced this (`fatal: 'canonical'` on every wizard-created
product) was a symptom of an over-engineered concept. Once we agreed
that "competitors on the product profile + text search at render time"
covered the actual reporting need, the entity-level vendor attribute
lost its remaining justification. Ripping it removed a load-bearing
coupling from prompt → LLM schema → storage PK → aggregation → report,
and it means new products no longer trip a broken vendor schema on
their first classify call.

The `format_map` defaulting-dict escape hatch was a deliberate design
choice: existing `products/*/prompts.yaml` templates were user-authored
and out of our direct edit reach, so silently absorbing removed
placeholders was preferable to forcing every operator to rewrite their
prompt after the upgrade. Same reasoning behind the ALTER-TABLE
migration script rather than "wipe and re-init."

## Consequences

Easy now:
- New products go through the wizard with one fewer step and cannot
  hit the `KeyError('canonical')` bug — because there is no canonical
  key to hit.
- The classify prompt is smaller and topic-agnostic; the "vendors:"
  line no longer bloats every LLM call.
- Entity dedup on `entity_mentions` is stricter (the same type + product
  + role from an item is one row, not one per vendor spelling).

Harder now:
- Historical data with per-vendor mention breakdowns loses that
  attribution — the migration collapses duplicate rows down to one per
  new PK (highest-confidence winner), so we cannot reconstruct "who
  the vendor was" for pre-migration entities.
- Competitor tracking is now text-search precision — no more exact
  entity-match semantics, so common company names that appear as
  substrings inside unrelated words will over-count. This is why the
  section is opt-in and labeled "static list from product.yaml."
- The entity group-key is shorter, so items that used to cluster only
  because they named the *same vendor's* variant of a product now
  cluster more aggressively.

Locked in:
- We can no longer ship per-vendor sentiment or per-vendor market-share
  style dashboards without reintroducing structured vendor data.
- Any product that genuinely needs a vendor-level attribute (e.g. a
  Windows-driver product) would have to re-open the ADR or extend
  `ProductExtras` with a topic-specific `vendor` field.

## Revisit if

- Multiple products come back asking for structured per-vendor
  aggregation the free-text `summary` and text-search competitor table
  can't cover.
- The text-search competitor implementation hits precision issues bad
  enough that we need to re-introduce a controlled vocabulary — at
  which point a per-product opt-in `extras.py` field is a lighter
  hammer than a first-class column.
- LLM classifiers begin returning `vendor` in `product` verbatim
  frequently enough that a downstream parser could re-derive the
  vendor for free.
