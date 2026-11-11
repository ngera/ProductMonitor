# ADR-0012: First-run UX — welcome screen, keyless-first sources, wizard LLM chooser

- **Status:** Accepted
- **Date:** 2026-07-21
- **Area:** frontend / infra

## Context
[first_run_solution.md](../first_run_solution.md) diagnoses the pre-V1
first-run experience as hostile: Reddit app approval, Foundry Local
setup, `init_db.py` step, and golden-set labeling all gate the user
*before* they see a single report. The doc's rung-2 goal is a real
report on the user's own product within 15 minutes.

Two mechanical changes carry most of the win:

1. **Keyless-first sources**. HN, RSS, Apple App Store, and Microsoft
   Community all need zero credentials. Reddit / GitHub / Stack Exchange
   / YouTube each need ~5–15 minutes of external setup. Sequencing
   matters: today's wizard's sources step forces the user to think about
   all of them equally.

2. **LLM chooser**. The V1 default endpoint (`http://localhost:5273/v1`,
   Windows-only Foundry Local) rules out ~80% of first-run environments.
   The user should not have to discover a broken LLM 20 minutes into
   their first run.

We also want first-time visitors to the webui to land on something more
useful than an empty "Products" grid.

## Options considered

1. **(chosen) Bake all three into the existing wizard + `/` route.**
   - Pros: reuses existing draft state / persistence machinery; no new
     top-level route tree; the wizard already survives leave-and-resume.
   - Cons: wizard grows from 7 to 8 steps.

2. **Ship a separate `/first-run` app that only the fresh-install path
   uses.**
   - Pros: keeps the wizard focused on power users.
   - Cons: two forms to maintain; wizard's LLM-assist infrastructure
     isn't reachable from a standalone first-run.

3. **Do nothing; put the setup steps in the README and CLAUDE.md.**
   - Pros: zero code.
   - Cons: docs don't solve the sequencing problem — users still hit
     the LLM wall mid-run.

## Decision

**Wizard changes** (`pipeline/wizard.py`, `webui/templates/wizard.html`):

- Add an `llm` step between `sources` and `snippets` with a 3-button
  chooser: **hosted API key** (Anthropic / OpenAI / Gemini / Azure
  OpenAI), **Ollama**, or **skip**. Ends with a live
  `LLMClient.health_check` so a broken config surfaces before finalize.
  Chosen provider is written into `.env` (API key) and into the
  product's `llm_routing.yaml` (endpoint + model + `api_key_env`).
- Pre-populate `draft.sources` with a keyless HN search stream keyed on
  the product display name (`keyless_default_sources(slug, display)`).
  The sources step's UI surfaces "more coverage (optional)" for keyed
  sources with honest cost estimates rather than a raw JSON textarea
  as the only interaction surface.
- `PROVIDER_PRESETS` in `pipeline/wizard.py` centralizes each hosted
  provider's endpoint + model default + env var. Adding a provider is
  one dict entry.

**First-run welcome** (`webui/app.py` index route, `templates/index.html`):

- When `available_products()` contains only `demo` (or nothing) AND
  `wizard_enabled` is on, `/` renders two big cards above the normal
  grid: "Try the demo" (links to the demo product) and "Monitor your
  product" (links to the wizard). The classic scaffold form stays
  below for advanced users.

## Rationale
- The 3-button LLM chooser matches the actual decision users face on
  first setup: "do I have a cloud key, do I want to run local, or do I
  just want to see the pipeline structure?" A radio group with three
  options is honest; the current implicit-Foundry-Local default was not.
- Keyless-first is a re-sequencing not a feature: HN and Apple App Store
  connectors already exist and don't need credentials. Making them the
  default is a two-line change (`keyless_default_sources`) plus a
  friendlier sources-step template.
- Landing on a welcome screen is a template change, not new architecture.
  Making it show up only when there are no non-demo products avoids
  disrupting workflows for anyone who's already using the tool.

## Consequences
- **Easy**: adding a new hosted provider (one entry in
  `PROVIDER_PRESETS`). Adding a new keyless-first source (append the
  type + default stream to `keyless_default_sources`).
- **Hard**: changing the WIZARD_STEPS order would rename URLs and
  break in-flight drafts. Any reorder needs a draft migration.
- **Locked in**: the shape of the `.env`-based API key storage
  (writes via `dotenv.set_key` under `PROVIDER_PRESETS[…].api_key_env`).
  If we ever want to move keys out of `.env` (e.g., into a keyring),
  the wizard needs to route through a shared "connections" service
  instead of writing directly.

## Revisit if
- The welcome page's "Try the demo" card becomes the top-clicked route
  from established users too — that suggests we're gating too much of
  the product surface behind product IDs.
- The wizard's LLM step's live health check fails for a substantial
  fraction of legitimate configs (e.g., new provider whose ping shape
  we don't support). At that point the health check should become
  advisory rather than surfacing as `[FAIL]`.
- Users start asking for keyless *keyed* sources (e.g., an OAuth
  device-code flow) — the `hosted` branch would need a "start OAuth
  flow" button rather than a raw text input.
