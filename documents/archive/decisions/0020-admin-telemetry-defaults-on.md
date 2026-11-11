# ADR-0020: Read-only admin telemetry surfaces default ON

- **Status:** Accepted
- **Date:** 2026-07-30
- **Area:** infra / frontend

## Context

[ADR-0006](0006-feature-flags-off-by-default.md) mandates every new
capability ship behind a feature flag, **off by default**. The rationale
is user-safety: a new feature that regresses or costs money shouldn't
turn on for anyone until the operator explicitly opts in.

The Admin > Tokens tracker (`admin_token_tracker_enabled`) breaks that
default. It is:

- **Read-only.** No writes to the warehouse, no LLM calls, no state
  mutation. It only reads the existing `llm_usage` table.
- **Admin-only.** Rendered under `/admin/tokens`, off the top-level
  navigation for regular users. Product owners don't see it.
- **Zero-cost when idle.** No background thread, no cache warm-up. All
  work is per-request. If nobody visits the page, nothing runs.
- **Diagnostic.** The product owner needs this data to see whether a
  scheduled run is burning unexpected tokens — hiding it behind a flag
  the same user must find and flip defeats the point.

Setting this to off-by-default would mean the operator has to (a) know
the flag exists, (b) find `config/features.yaml`, (c) flip it, and
(d) restart before they can look at their own token spend. That's
friction with no safety upside.

## Options considered

1. **Follow ADR-0006 verbatim: off by default, operator flips it.**
   Rejected — no safety win (nothing to break) and adds a setup step
   for something the operator will want on immediately.
2. **Remove the feature flag entirely.** Rejected — we still want the
   ability to disable per-product (e.g. a shared demo where you don't
   want anyone poking at token spend). Keeping the flag preserves that.
3. **New default rule for a narrow class: read-only admin telemetry
   defaults on.** What we did.

## Decision

For any new feature that meets **all four** criteria — read-only, admin
route, zero background cost, diagnostic in nature — the default flag
value is `true`. The flag itself still exists so operators can turn it
off per-environment or per-product.

`admin_token_tracker_enabled: true` in
[`config/features.yaml`](../../config/features.yaml) is the first
application.

## Rationale

ADR-0006's off-by-default rule protects against features that could
break the system, cost money, or surprise users. None of those apply to
a read-only admin diagnostic surface. Applying the rule uniformly would
force every operator to opt in to their own observability, which is
the opposite of what observability is for.

The four-criterion gate keeps the exception narrow. A feature that
_writes_ (e.g. an admin action to purge a table) fails "read-only"
and returns to the ADR-0006 default. A feature that _runs
continuously_ (e.g. a real-time streaming panel) fails "zero cost
when idle" and returns to the default. A feature reachable outside
`/admin/*` fails "admin-only".

## Consequences

Easy now:
- Operators land on `/admin/tokens` and see real data without any
  setup step.
- Future read-only admin telemetry (e.g. a scheduler-health panel, an
  eval-history dashboard) can follow the same default without needing
  another ADR.

Harder now:
- Reviewers of new feature flags need to check the four criteria
  before recommending default-on; the burden falls on the PR author
  to justify the exception in the flag's YAML comment.
- If we ever grow multi-tenancy where "admin" means one tenant vs
  another, this rule needs revisiting — the "admin-only" criterion
  currently assumes a single-tenant local install.

Locked in:
- The four criteria are the whole rule; adding a fifth is out of
  scope for this ADR (would supersede).
- The tracker page itself keeps its per-product override so tenants
  can still opt out per product.

## Revisit if

- We ship an admin surface that mutates state (would need its own
  ADR since it can't hide under this exception).
- Multi-tenancy lands — "admin" needs a sharper definition.
- Any read-only admin surface causes a real incident that a default-off
  flag would have prevented (would need to reassert ADR-0006 uniformly).
