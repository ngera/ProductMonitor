# Architecture Decision Records

Design decisions with weight, recorded so future readers (or future
you) can see *why* something was built the way it was — not just what
was built. Format follows [TEMPLATE.md](TEMPLATE.md).

## When to write an ADR

Per [CLAUDE.md](../../CLAUDE.md):

- New services, dependencies, or third-party providers
- Data model / schema changes beyond additive columns
- Changes to agent topology or orchestration
- Auth, tenancy, or compliance boundary changes
- Anything that would be expensive to reverse

Trivial refactors, bug fixes, and styling do NOT need an ADR.

## Naming

`NNNN-slug-with-hyphens.md`, numbered sequentially. Once accepted, never
renumber — supersede via a new ADR with a `Supersedes: ADR-NNNN` header.

## Index

### Accepted (implemented)

Every ADR below moved from Proposed to Accepted on 2026-07-16 after
the corresponding Post-V1 plan section shipped. `Implemented in` links
to the primary commit that landed the decision.

| # | Title | Area | Plan reference | Implemented in |
|---|---|---|---|---|
| [0001](0001-plugin-discovery.md) | Plugin discovery supports both drop-in files and entry points | infra | [POST_V1_PLAN §4.1](../POST_V1_PLAN.md) | `daad888`, `ec6cdbe` |
| [0002](0002-assistant-llm-global-connection.md) | Assistant LLM is a separate global connection, not per-product | infra | [POST_V1_PLAN §4.8](../POST_V1_PLAN.md) | `d907145` |
| [0003](0003-rationale-separate-pass.md) | Report rationale runs as a separate on-demand pass, not during classify | data | [POST_V1_PLAN §4.7](../POST_V1_PLAN.md) | `f07157d` |
| [0004](0004-golden-set-time-based-split.md) | Golden set is a time-based split of the snippet pool | data | [POST_V1_PLAN §4.10](../POST_V1_PLAN.md) | `9690f70` |
| [0005](0005-token-attribution-contextvars.md) | Token attribution via contextvars, warehoused in llm_usage | infra | [POST_V1_PLAN §4.11](../POST_V1_PLAN.md) | `d907145` |
| [0006](0006-feature-flags-off-by-default.md) | Every new capability ships behind a feature flag, off by default | infra | [POST_V1_PLAN §4.13](../POST_V1_PLAN.md) | `daad888`, `c6d3945` |
| [0007](0007-structured-outputs-and-prompt-caching.md) | Structured LLM outputs everywhere, prompt caching where supported | infra | [POST_V1_PLAN §4.15](../POST_V1_PLAN.md) | `d907145` |
| [0008](0008-plugin-trust-flag.md) | Drop-in plugins require explicit `--trust-plugins-dir` opt-in | infra (security) | [POST_V1_PLAN §4.14](../POST_V1_PLAN.md) | `daad888` |
| [0009](0009-end-to-end-eval-scope.md) | Evals scope is end-to-end classify output only, not per-stage | data | [POST_V1_PLAN §4.10](../POST_V1_PLAN.md) | `9690f70` |
| [0010](0010-replay-llm-endpoint.md) | `replay://` LLM endpoint scheme promotes the POST_V1 §4.12 mock adapter | infra | [first_run_solution.md §3](../first_run_solution.md) | pending |
| [0011](0011-console-entry-point.md) | Single `feedback-monitor` console entry point with subcommand dispatch | infra | [first_run_solution.md §3.3](../first_run_solution.md) | pending |
| [0012](0012-first-run-and-wizard-defaults.md) | First-run UX: welcome screen, keyless-first sources, wizard LLM chooser | frontend/infra | [first_run_solution.md §4](../first_run_solution.md) | pending |
| [0013](0013-product-facts-schema.md) | Product facts (aliases/goals/competitors/scope) as first-class product.yaml fields | data | [wizard_redesign_plan.md §1](../wizard_redesign_plan.md) | pending |
| [0014](0014-wizard-v2-four-screen-flow.md) | Wizard v2 — 4-screen "react-don't-author" flow with grounded taxonomy | frontend/infra | [wizard_redesign_plan.md §§3-5](../wizard_redesign_plan.md) | pending |
| [0015](0015-advanced-fold-and-optional-extras.md) | Product page "Advanced" fold + optional `extras.py` | frontend/infra | [wizard_redesign_plan.md §6](../wizard_redesign_plan.md) | pending |
| [0016](0016-persistent-issue-stage.md) | Persistent-issue stage clusters week_groups across time via local embeddings | data/infra | [report_v2_design.md §5.1](../report_v2_design.md) | pending |
| [0017](0017-digest-v2-sole-render.md) | Digest v2 replaces per-area and per-comments pages as the sole report artifact | frontend/infra | [report_v2_design.md](../report_v2_design.md) | pending |
| [0018](0018-remove-vendor-concept.md) | Remove vendor as a first-class concept (config, wizard, schema, reports) | data/infra/frontend | — | 2026-07-28 |
| [0019](0019-rich-competitor-schema.md) | Competitors become rich `{name, aliases, color}` objects | data/frontend | [report_v2_design.md §7.2](../report_v2_design.md) | 2026-07-29 |
| [0020](0020-admin-telemetry-defaults-on.md) | Read-only admin telemetry surfaces default ON (narrow exception to ADR-0006) | infra/frontend | — | 2026-07-30 |
| [0021](0021-source-taxonomy.md) | Source taxonomy — source_category + content_types on SourceManifest | data/frontend | — | 2026-08-05 |
| [0022](0022-http-retry-with-backoff.md) | Centralized retry-with-backoff for source HTTP calls | infra | — | 2026-09-15 |
| [0023](0023-concurrent-fetch.md) | Bounded concurrent stream fetch (ThreadPoolExecutor + two-tier semaphore) | infra | — | 2026-09-15 |
| [0024](0024-canonical-url-dedup.md) | Cross-source canonical-URL dedup at normalize | data | — | 2026-09-15 |
| [0025](0025-run-notifications-via-webhook.md) | Run notifications via outbound webhook | infra | — | 2026-09-15 |
| [0026](0026-webui-router-split.md) | Split webui/app.py into routers + services (incremental) | frontend/infra | — | 2026-09-15 |
| [0029](0029-remove-kb-grouping.md) | Remove hardcoded KB from grouping and shared extract | data | — | 2026-09-16 |
| [0030](0030-google-play-service-account-auth.md) | Google Play reviews via service-account auth | infra/data | — | 2026-09-17 |
| [0031](0031-dual-tagged-sources-list-once.md) | Dual-tagged sources list once under primary content_type | frontend | — | 2026-09-17 |
| [0032](0032-obsidian-console-theme.md) | Obsidian Console theme + pm_theme cookie for the webui | frontend | [ui_implementation_spec.md](../../Claude%20outputs/ui_implementation_spec.md) | 2026-09-18 |
| [0033](0033-resume-from-stage.md) | Resume cancelled/failed runs from a pipeline stage | infra/frontend | — | 2026-09-19 |
| [0034](0034-themes-ui-and-context-relevance-gate.md) | Themes UI + context-derived relevance pre-gate | frontend/data | — | 2026-09-20 |
| [0035](0035-one-press-section-and-classify-gates.md) | One press section; empty LLM answers are failures; classify retry cap | data/frontend | — | 2026-11-11 |
| [0036](0036-local-only-webui-requests.md) | Web UI refuses foreign hosts and cross-site writes | infra/security | — | 2026-11-11 |

### Proposed (in Post-V1 plan, not yet built)

*None — first-run wave (0010–0012) landed in the "minutes to first
report" cut alongside every prior ADR. Wizard-redesign wave (0013–…) is
landing behind the `wizard_v2_enabled` flag.*

### Superseded

| # | Title | Superseded by |
|---|---|---|
| [0021](0021-source-taxonomy.md) (dual-list UI rule only) | Source taxonomy dual listing in both content_type sections | [0031](0031-dual-tagged-sources-list-once.md) |
