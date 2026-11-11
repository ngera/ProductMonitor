# ADR-0014: Wizard v2 — 4-screen "react-don't-author" flow with grounded taxonomy

- **Status:** Accepted
- **Date:** 2026-07-25
- **Area:** frontend/infra
- **Related:** [ADR-0012](0012-first-run-and-wizard-defaults.md) (superseded in
  spirit by this ADR — v1's 7-step "author every artifact" flow is replaced;
  v1 stays gated behind `wizard_enabled` for one release as a fallback),
  [ADR-0013](0013-product-facts-schema.md) (this ADR consumes the schema
  landed there)

## Context

Wizard v1 (ADR-0012) took the user through eight screens that each asked
them to author or edit a product-machinery artifact directly: scope prose,
a taxonomy JSON blob, a vendors JSON blob, prompt templates, source configs,
LLM routing, seed snippets. Most users bounced at the taxonomy step. The
`documents/wizard_redesign_plan.md` review session (2026-07-25) reframed the
problem: users know their product; they don't know our taxonomy shape.
Asking them to fill in machinery is the wrong question.

Two principles from the redesign:
1. **Facts vs machinery** — users supply facts (name, url, aliases,
   confusables, goals, competitors, scope bullets); the system generates the
   machinery as drafts to confirm.
2. **React, don't author — show real data early** — the wizard fetches a
   handful of real posts before proposing a taxonomy, so both the user's
   Y/N calibration and the LLM's area suggestions ground in actual content.

## Options considered

1. **Iterate on v1** — reduce the number of blank fields, add better
   defaults. Con: doesn't fix the core "authoring machinery" problem;
   taxonomy still feels like a blank map.
2. **Rebuild as v2 alongside v1** (chosen) — new router module,
   new draft schema, four screens, feature-flagged. Keeps v1 working while
   we validate; gives a clean deletion story once v2 is proven.
3. **Rip v1 out and replace** — smaller diff, but risks a bad rollout with
   no fallback. Users mid-draft when v2 lands would lose state.

## Decision

Ship wizard v2 behind `wizard_v2_enabled` (default off until Phase 7 flip)
with:

- **Four screens**: Describe → Confirm profile → Calibrate → Review & run.
- **Independent router**: `webui/wizard.py` mounted from `app.py`. Route
  prefix `/wizard/*`. Draft state at
  `products/.wizard_drafts/v2/<slug>.yaml` — separate from v1's
  `.wizard_drafts/*.yaml` namespace.
- **Two LLM services powering it**:
  - `pipeline.profile_draft.draft_profile(name, url_or_description, goals)`
    produces the Screen-2 profile (description, aliases, confusables,
    competitors, scope bullets, suggested sources). URL fetch is
    best-effort; degrades to name+goals on failure.
  - `pipeline.taxonomy_proposal.propose_taxonomy(profile_facts, corpus)`
    proposes 3-5 areas from the minifetch corpus. Falls back to a
    deterministic starter guess when no corpus / no assistant LLM, with
    `grounded=False` so the review UI can label it.
  - Both stages tag token usage with `stage=assistant_wizard_*` so the
    global assistant-LLM budget (ADR-0002) picks them up.
- **Live mini-fetch (`pipeline.minifetch`)**: a throw-away 20-second fetch
  across enabled+keyless suggested sources. Writes only to
  `data/.wizard/<slug>/` — never touches the product warehouse, cursors, or
  seen-ids tables. Zero-results path (<3 items) is a first-class state, not
  an error.
- **Materialization (`pipeline.wizard_v2.materialize`)**: turns a completed
  draft into a live product tree. Writes `product.yaml` (facts fields from
  ADR-0013), `taxonomy.yaml` (approved proposal), `sources.yaml` (enabled
  streams), `vendors.yaml` (seeded from `competitors`), optional
  `llm_routing.yaml` (from chooser), and `examples/` (from calibration).
  Validate via `load_product` and roll back on failure. Delete the draft on
  success; cleanup temp corpus.
- **What we removed from the creation surface**: prompt templates, vendors
  authoring, extras.py generation. These become Advanced-only surfaces on
  the product page (Phase 6). Not deleting the machinery — just moving it
  behind an "advanced" fold so first-run users never see it.

## Rationale

The user's leverage during setup is *knowing their product*. Every screen
after Screen 1 asks them to confirm what we drafted, not to invent. Grounding
the taxonomy in real posts (Screen 3 corpus → Screen 4 areas) matches how
users think — they read a few posts, then group them — not the other way
around. Keeping v1 alive behind its flag gives us one release of A/B
comparison and a rollback lever.

Structured outputs (ADR-0007) plus prompt safety wrapping (`wrap_user_content`
around every piece of scraped and user-authored text) make it safe to feed
external HTML into the drafting call. Feature flag (ADR-0006) plus the
independent router mean the flip is one YAML edit.

## Consequences

**Easier:**
- First-time users reach a running report typing only: name, URL, goal
  checkboxes, Y/N judgments, optional API key. The ≤10-minute target from
  Phase 7's DoD is now within reach.
- The taxonomy proposer + calibrator can be reused by Phase 6's Profile
  edit page — the chip partial (`_chips.html`) already does double duty.
- Because facts (aliases / scope / competitors) live on the product
  (ADR-0013), regenerating taxonomy or prompts later re-reads structured
  fields instead of re-parsing prose.

**Harder / locked-in:**
- The materializer bakes in a scaffold-then-overwrite pattern. If we later
  extract `scaffold_product` into a plugin-authored generator, the wizard
  will need a matching hook.
- The temp corpus at `data/.wizard/<slug>/` is a new on-disk shape that
  observability doesn't yet know about — it's tiny and self-cleaning at
  materialize time, but hand-editing it (or a crash mid-fetch) leaves
  orphans until the user discards the draft.
- Wizard v2's LLM chooser writes API keys into `.env` via `dotenv.set_key`.
  Same mechanism `/connections` uses today, but now inline in a flow — one
  more caller if we ever change the key-storage contract.
- The cost estimate is a rough per-item token multiplier. Users may push
  back that "estimate" is misleading if their actual runs land far off; the
  panel is labeled "estimate" and there is only one place to tune the
  constants.

## Revisit if

- The starter-guess fallback (no LLM) is used more often than the grounded
  path (would mean the assistant LLM isn't being provisioned during setup,
  which contradicts the "≤10 minute" target).
- The 20s wall-clock cap on the minifetch is too tight for slow sources
  (surface: user regenerates repeatedly because the deck is always < 3
  items).
- The 3-per-section regen cap frustrates users on ambiguous products —
  bump the cap or make it per-draft-total rather than per-section.
- We add source plugins that need multi-step OAuth — the current "add key
  to .env" chooser can't handle a redirect flow.
