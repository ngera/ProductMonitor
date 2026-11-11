# ADR-0006: Every new capability ships behind a feature flag, off by default

- **Status:** Accepted (implemented in `daad888`, `c6d3945`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** infra
- **Plan reference:** [POST_V1_PLAN.md §4.13](../POST_V1_PLAN.md), D11

## Context

V0.2 of the plan lumped 8 features into 3 phases with tight
interdependencies. Critical review flagged this as waterfall-shaped:
if any feature slips or has bugs, everything downstream stalls, and
users see nothing.

Industry standard for this shape of work is progressive delivery:
every capability ships behind a feature flag, off by default, users
opt in. Late items don't block early ones. Bugs are contained.
Rollback is a config flip.

For a personal-tool codebase with a small user base, we don't need
GrowthBook / LaunchDarkly / etc. A YAML file suffices.

## Options considered

1. **No feature flags.** Ship features when they're ready. Any
   incomplete or buggy feature is visible to all users. Simplest;
   worst UX during transitions.

2. **Static feature flags in `config/features.yaml`, off by default.**
   Users opt in explicitly. Product-level overrides via
   `products/<pid>/features.yaml`. Backwards-compat by default.

3. **Runtime-toggled flags with a UI.** `/admin/features` page lets
   users flip flags without editing YAML. More UX polish; adds real
   complexity (persistence, defaults, migration on flag rename).

## Decision

Option 2. YAML file with defaults, product-level overrides. Every
`§4.x` feature in the Post-V1 plan gets a flag:

```yaml
# config/features.yaml
features:
  wizard_enabled:              false
  evals_enabled:               false
  snippet_candidates_enabled:  false
  prompt_suggestions_enabled:  false
  rationale_enabled:           false
  token_monitor_enabled:       false
  scrapecreators_enabled:      false
  trust_plugins_dir:           false   # see ADR-0008
```

Defaults: all off. Users flip to `true` after reading each feature's
doc.

Product-level override syntax:
```yaml
# products/windows-media-platform/features.yaml
features:
  evals_enabled: true            # I have snippets, I want evals
```

Flag checks in code use a simple helper:
```python
if features.enabled("evals_enabled", product_id):
    ...
```

## Rationale

- **Removes waterfall risk.** Ship each feature independently; late
  items don't block early ones.
- **Zero risk to existing users.** All V1 behavior stays exactly the
  same until a user opts in.
- **Debug story.** "Turn X off and see if the bug goes away" is a
  first-class debugging move.
- **Doesn't overreach.** A UI-driven flag system is overkill for a
  personal tool. YAML is auditable, diffable, and version-controllable.

## Consequences

**Makes easy:**
- Ship features incrementally. Each PR can add a feature at `off`,
  land it, then a follow-on PR flips it to `on` by default once
  proven stable.
- Users can experiment: turn on evals for one product, keep it off
  for another.
- Emergency rollback: flip flag to `false`, redeploy config.

**Makes hard or expensive to reverse:**
- Flag proliferation is a maintenance burden. Mitigation policy:
  once a feature has been stable for 3 months, delete its flag
  (always-on). Documented as a rule in `SECURITY.md` / dev process.
- Every code site that checks a flag needs a fallback path — two
  code paths per feature. Fixed once the flag is removed.

**Locks us into:**
- `features.yaml` as the flag config location. Trivial to reverse.

## Revisit if

- Flag count exceeds 20 (we currently plan 8). Might warrant a real
  flag system (LaunchDarkly, GrowthBook OSS).
- We ship a multi-tenant version where flags need per-tenant
  granularity. Product-level overrides may not scale.
- Users routinely ask for a UI to flip flags (evidence: repeated asks).
  Then move to Option 3.
