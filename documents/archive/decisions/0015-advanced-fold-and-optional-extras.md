# ADR-0015: Product page "Advanced" fold + optional `extras.py`

- **Status:** Accepted
- **Date:** 2026-07-25
- **Area:** frontend/infra
- **Related:** [ADR-0013](0013-product-facts-schema.md) (facts schema),
  [ADR-0014](0014-wizard-v2-four-screen-flow.md) (wizard v2)

## Context

Wizard v2 lands facts as first-class fields on products (ADR-0013). That
makes the product dashboard the natural surface for editing those facts —
and it also exposes a legacy problem: the dashboard mixes primary controls
(Sources, Taxonomy, Snippets, Runs) with hand-tuning surfaces (Prompts,
Vendors, LLM Routing, raw YAML). New users see 8+ cards and don't know which
matter.

Separately, `scaffold_product()` writes `extras.py` for every product even
though the vast majority of products never customize it. That file becomes
noise in `git diff` and, more importantly, encodes an implicit "run arbitrary
Python at load time" contract into every product — which is fine when the
user asked for it, and surprising when they didn't.

## Options considered

1. **Delete the legacy surfaces.** Too aggressive — the wizard drafts good
   defaults but power users still want to tune prompts by hand.
2. **Keep everything visible, add a Profile card at the top.** Doesn't fix
   the "which of these do I care about?" problem for first-time users.
3. **Group prompts/vendors/LLM-routing/raw-YAML under an `<details>`
   "Advanced" fold; add a Profile card up top; make `extras.py` optional in
   both scaffold and loader** (chosen).

## Decision

Product dashboard restructures into:

- **Primary cards** (visible by default): Profile, Setup (taxonomy + sources
  + snippets), Runs.
- **Setup detail** (collapsed, existing pattern): expandable breakdown of
  the Setup card.
- **Advanced** (collapsed, new): Prompts, Vendors, LLM Routing, raw-YAML
  edit links, extras module info.

New Profile page at `GET/POST /products/{id}/profile`:
- Chip editor for aliases, not_to_be_confused_with, competitors, scope_in,
  scope_out, plus URL and goals checkboxes.
- Shares the `webui/templates/wizard/_chips.html` partial with wizard v2 so
  visual + interaction language matches (build once, use twice).
- `POST` routes through `pipeline.product.save_product_facts()` for
  validation.

`extras.py`:
- **Loader**: `pipeline.product.load_product()` treats `extras_module`/
  `extras_class` as optional. When absent (or when the file doesn't exist),
  substitutes an empty `ProductExtras` class via
  `_empty_product_extras()`. Products that *do* declare `extras_module`
  continue to import the file exactly as before — no regression for
  existing products.
- **Scaffold**: `scaffold_product()` no longer writes `extras.py` and no
  longer emits `extras_module`/`extras_class` in `product.yaml`. Users who
  want custom classification fields opt in from the Advanced page (future
  work: an explicit "add extras" button that scaffolds the file and adds
  the key with a "this executes Python at load time" warning per
  SECURITY.md).
- **Existing products on disk**: nothing changes. Their `extras.py`
  continues to load.

`save_product_meta()` no longer auto-inserts `extras_module`/`extras_class`
for products that never had them — matches the new scaffold behavior.

## Rationale

The fold is progressive disclosure. First-time users see the four things
they'll actually touch on day one (Profile, Sources, Taxonomy, Runs).
Power users click Advanced when they need to hand-tune. The Prompts /
Vendors / LLM Routing routes are unchanged; only their entry points move.

Optional `extras.py` matches SECURITY.md's posture — Python at load time
is a real capability, not a default. Products that don't need it don't
carry it. The empty `ProductExtras` fallback keeps the composed
`CoreClassification + extras` shape stable everywhere else.

Sharing `_chips.html` between the wizard and the profile page means only
one surface has to look right. Any future improvement (drag reorder, chip
suggestions) benefits both.

## Consequences

**Easier:**
- Product dashboard scans faster; first-run experience is honest about
  which surfaces are common vs advanced.
- Facts are editable outside the wizard (no need to reopen the wizard just
  to add an alias).
- `git diff` on a scaffolded product is smaller — no boilerplate
  `extras.py` for products that don't use it.

**Harder / locked-in:**
- Two places to update if we change what a "product" looks like:
  wizard v2's Screen 2 and the profile page. The shared `_chips.html`
  reduces but doesn't eliminate the duplication.
- Advanced page currently only *shows* extras info. When we add the
  "add extras.py" button later, we need to bring the security warning
  copy from SECURITY.md into the UI itself.
- Products created by v1 wizard still carry the auto-inserted
  `extras_module`/`extras_class`. That's fine — they load unchanged — but
  it means "does this product have extras?" isn't a simple field check;
  it's `product_meta.get("extras_module") and (dir / f"{module}.py").exists()`.

## Revisit if

- Users click "Prompts" often — signal that the fold hides too much and
  the entry point deserves promotion.
- We add per-product overrides for taxonomy proposal / prompt suggestion
  tuning (would deserve its own Advanced sub-fold rather than a flat
  grid).
- The chip partial grows beyond ~50 lines — probably means we should
  break it into a proper macro or lift to a small custom element.
