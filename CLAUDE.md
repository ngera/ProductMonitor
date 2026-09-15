## Project overview and goals

**ProductMonitor** is a **local-first, single-tenant** tool that watches
public feedback about a product across many sources (Hacker News, RSS,
Reddit, GitHub Issues, Stack Exchange, YouTube, Apple App Store,
Microsoft Tech Community, …), classifies each item with a configurable
LLM (Anthropic / OpenAI / Ollama / Foundry Local / any OpenAI-compatible
endpoint), and renders **static HTML digests** that owners open in the
browser. No SaaS, no phone-home, no shared warehouse — every product's
data lives under `data/<product_id>/` on the user's machine.

### Who it's for

Product owners, PMs, and small teams who want the "what are users
saying about my product this week?" answer without paying for
enterprise sentiment tooling and without handing their raw feedback to
a third party. Runs on Windows, macOS, and Linux (Python 3.11+), or
via `docker compose up`.

### Design principles

- **Local-first, batteries-included.** A brand-new user should reach a
  running report in minutes. Wizard v2 exists for this reason
  ([ADR-0014](documents/decisions/0014-wizard-v2-four-screen-flow.md)).
  Keyless sources default on; hosted LLMs are opt-in.
- **Plugin-shaped sources.** Every fetch source is a plugin with a
  manifest, connection fields, and stream fields (`sources/`). Adding
  a new source is a plugin, not a core change.
- **LLM-agnostic per-stage routing.** `products/<id>/llm_routing.yaml`
  picks model + endpoint per stage. The **assistant LLM**
  ([ADR-0002](documents/decisions/0002-assistant-llm-global-connection.md))
  is a separate global connection for wizard drafting, taxonomy
  proposals, headlines, and other cosmetic passes.
- **Deterministic where it matters.** Eval bootstraps with a fixed
  seed. Headline generation is cache-first keyed on
  `sha256(content + prompt + model)` so re-renders cost nothing.

## Non-goals (do not drift into these without an ADR)

- **No multi-tenancy / user attribution.** One local install, one
  operator. If you find yourself designing per-user auth or
  per-tenant isolation, stop and ask.
- **No cross-product intelligence.** Each `products/<id>/` is a silo;
  persistent-issue identity is per-product per-section
  ([ADR-0016](documents/decisions/0016-persistent-issue-stage.md)).
- **No default-on outbound integrations.** Digest v2 is web-only
  ([ADR-0017](documents/decisions/0017-digest-v2-sole-render.md));
  `email_digest_mockup.html` in `documents/` is obsolete. Notifications,
  exports, and any other outbound calls stay opt-in and user-configured
  — the webhook notifier
  ([ADR-0025](documents/decisions/0025-run-notifications-via-webhook.md))
  is the pattern: user supplies the URL, feature-flag-off by default,
  failure of the outbound call must never fail the run.
- **No real-time / streaming pipeline.** The unit of work is a run
  over a week window; scheduling is cadence-based (daily / weekly /
  bi_weekly / monthly), not event-driven.
- **No vendor as a first-class concept.** Removed per
  [ADR-0018](documents/decisions/0018-remove-vendor-concept.md);
  competitors are the surviving similar concept as rich
  `{name, aliases, color, context}` objects
  ([ADR-0019](documents/decisions/0019-rich-competitor-schema.md)).

---

## MUST rules — violating breaks the project

- **Local-first stays local.** No phone-home, no external telemetry,
  no default-outbound calls that aren't LLM/source fetches the user
  explicitly configured. The webui binds `127.0.0.1:8766` by default;
  any change to that bind needs an ADR.
- **Secrets only in `.env` or process environment.** Never commit API
  keys. Never write them to YAML under `config/` or `products/`. The
  wizard's LLM step and `/connections` page write to `.env` in place;
  new secret-carrying flows must do the same.
- **Attribution mandatory.** Every item rendered to HTML must include
  the `_item_attribution.html.j2` partial (§13). If you add an
  item-displaying template, add its filename to
  `render.ITEM_DISPLAYING_TEMPLATES` so `render.validate_templates()`
  fails the build when the partial is missing.
- **Prompt-injection defenses stay on.**
  `SYSTEM_PROMPT_SAFETY_PREAMBLE` and `<user_input>` wrapping in
  `pipeline/prompt_safety.py` protect user-authored facts from being
  interpreted as instructions. Don't strip them "to save tokens".
- **UTC in storage, always.** `datetime.now(timezone.utc)` on every
  timestamp that persists. Local display only in templates. Week ids
  are ISO `YYYY-Www`.
- **Stable cross-cutting IDs.** `run_id`, `product_id`, `week_id`,
  `issue_id`, `item_id` — never renumber, never reuse. ADR titles
  once accepted are permanent; supersede via a new ADR.
- **Forward-only schema.** Additive columns are fine. Anything that
  drops columns or reshapes tables needs a `scripts/migrate_*.py`
  and an ADR. Never rewrite historical items in place.
- **ADR for architecture change.** See the *Architecture decisions*
  section below. Feature flag every new user-visible capability off
  by default per
  [ADR-0006](documents/decisions/0006-feature-flags-off-by-default.md);
  the narrow "read-only admin telemetry defaults on" exception is
  [ADR-0020](documents/decisions/0020-admin-telemetry-defaults-on.md).
- **Ask before destructive operations.** Schema drops, `git reset
  --hard`, `git push --force`, `docker rm -v`, deleting user data,
  dropping a DuckDB warehouse — none of these happen without explicit
  authorization.
- **Never `--no-verify` on commits, never force-push shared history.**
  Fix the hook or ask.
- **Every LLM call site records to `llm_usage`.** Attribution via
  `TokenContext` per
  [ADR-0005](documents/decisions/0005-token-attribution-contextvars.md).
  Silent LLM calls make the Admin > Tokens tracker lie.

## SHOULD rules — justify deviation in the PR

- **Design before implementing anything non-trivial.** If a change
  touches more than a handful of files, changes public API, or
  requires a schema/config migration, describe the design + tradeoffs
  and confirm scope first. Present options with a recommended choice
  rather than picking silently.
- **Diagnose root cause, don't paper over symptoms.** If something
  crashes, find why. If the fix is a workaround, say so and record
  the underlying issue. Do NOT add retry loops or `try/except` to
  swallow errors that point at a real bug.
- **Small, surgical edits.** A bug fix doesn't need surrounding
  cleanup. Don't refactor while fixing.
- **Delete unused code fully.** No dead `# removed for X` comments,
  no re-export stubs, no compatibility shims for callers that don't
  exist. When a concept is removed, remove it everywhere in one pass
  (see ADR-0018 as the pattern).
- **Legacy input fallbacks OK; hypothetical-future fallbacks not.**
  Reading a legacy config key while migrating (e.g.
  `trend_bucket_switch_days` → `_months`) is fine — document the
  sunset. Don't add fallbacks for hypothetical future users.
- **User input fatal; optional telemetry logged.** Config errors and
  prompt errors must surface to the user. A token-usage recording
  failure, a cache miss, a chart-render fallback — log at WARN, keep
  the run going. Silent (unlogged) failures are wrong in both
  directions.
- **Comments explain WHY, not what.** Add one when a future reader
  would ask "why is this here?" — a hidden constraint, an invariant,
  a workaround for a specific bug. Don't describe what the code
  does; good identifiers do that. Don't reference the current PR or
  task; that context belongs in the commit message.
- **Follow existing conventions.** New template? Look at how the
  neighbours are structured (`.dt` tables, chip editors, sub-nav
  tabs). New route? Match the existing handler style. If you're
  changing an established pattern intentionally, explain why in the
  PR body.
- **Cache-first for LLM calls whose input can repeat.** Headlines use
  SHA-256 content+prompt+model keys. Wizard prompts use
  `cacheable_system=True` on stable system prompts. Every new call
  site should think about this before shipping.
- **Justify every new dependency.** A `pip install` addition to
  `requirements.txt` needs a paragraph in the PR on why stdlib or an
  existing dep doesn't cover it. Heavy deps (torch,
  sentence-transformers, matplotlib) go behind a feature flag so
  optional installs stay light.
- **Regression test for every real bug.** When a class of bug bites
  (like the DuckDB connection leak), the fix ships with a test that
  would have caught it.
- **Golden path before "done".** For UI changes, hit the URL and
  verify. For pipeline changes, trigger a run or smoke-test the
  affected function.
- **Feature flags have a sunset plan.** The ADR that ships a flag
  says when it'll be flipped default-on or removed. Flags that live
  forever become permanent complexity. Removing a flag needs its own
  ADR if the feature ships default-on.
- **README is for humans; CLAUDE.md is for agents.** Don't duplicate.
  Don't put install instructions here, don't put agent norms in the
  README.
- **Small purposeful commits.** One logical change per commit;
  `feat:` / `fix:` / `docs:` / `refactor:` prefixes match the log.

## Working with the codebase (context, not rules)

This section is descriptive — patterns and gotchas that don't rise to
the level of hard rules but will bite you if you don't know them.

**Data / storage:**
- Every product's warehouse lives at
  `data/<product_id>/warehouse.duckdb`. Cross-product code (Admin >
  Tokens tracker, for example) iterates every warehouse read-only.
- DuckDB uses file-level locks that are cross-process. Long-held
  connections on the webui side crash concurrent subprocess writes.
  Use `storage.warehouse()` (which retries on lock) rather than raw
  `duckdb.connect()` in the webui. Always close via `try/finally`.
- Don't poll the warehouse from the webui while a run is in flight
  (the run detail page skips `per_run_totals` when `running` is true
  for this reason).

**Runtime:**
- Primary dev host today is Windows. Bash tool calls need PowerShell
  syntax (`$env:VAR`, backtick continuation, `$null` not `/dev/null`).
  Every change must still work on macOS + Linux + Docker.
- The Docker container bind-mounts `config/`, `products/`, `data/`,
  `reports/`, `.env`. Code under `pipeline/`, `webui/`, `tests/`,
  `report_templates/` is BAKED into the image. `docker cp` overlays
  live in the container's writable layer and are wiped when the
  container is recreated. For persistent code changes:
  `docker compose build && docker compose up -d`.
- After Python module edits, restart uvicorn (or the container) —
  Python caches imported modules and won't reload overlays. Template
  edits don't need a restart.

**Observability:**
- Every stage emits `stage_start` and `stage_done` structured log
  lines with `seconds=`; the runs UI parses these. Don't break the
  shape.
- Stage-level errors bubble to `payload.errors` on the run log JSON.
  Silent exceptions in a stage leave the operator with no signal.
- The Admin > Tokens tracker at `/admin/tokens` (ADR-0020) is the
  cross-product token/cost view. New LLM call sites that don't record
  to `llm_usage` show up as gaps there.

**Pre-existing test failures:**
- There is a small number of test failures that pre-date current
  work: `test_group.py` (`products/windows/` was deleted from the
  working tree) and `test_llm_setup_wizard.py` (unrelated UI text).
  Fix them or mark them `xfail` with a reason. Not a floor to
  perpetuate.

## Architecture decisions — MANDATORY

@documents/decisions/README.md

### Before changing design or architecture

1. Check the ADR index above for any decision covering the area you're
   touching.
2. If one exists, READ the full ADR file before proposing changes.
3. If your change contradicts an accepted ADR, STOP. Say so explicitly
   and ask whether to supersede it. Do not silently work around it.

### After making a design or architecture change

You MUST record it. Trigger the `adr` skill and write a new ADR before
considering the task complete. This applies to:

- new services, dependencies, or third-party providers
- data model / schema changes beyond additive columns
- changes to agent topology or orchestration
- auth, tenancy, or compliance boundary changes
- anything that would be expensive to reverse

Trivial refactors, bug fixes, and styling do NOT need an ADR.
