# ADR-0029: Remove hardcoded KB from grouping and shared extract

- **Status:** Accepted
- **Date:** 2026-09-16
- **Area:** data

## Context

Group-key construction preferred entity → Windows KB number → title
simhash. KB matching (`KB\d{7}`) and classify prompt hints for KB numbers
were hardcoded in core. That made grouping materially better for the
Windows origin domain than for products the README advertises ("any
product or topic"), while remaining invisible from outside. Cursor and
DoorDash almost never hit the KB tier; they fell through to title
simhash — the weakest key.

We considered generalizing tier 2 (per-product identifier patterns,
auto-detect at wizard time). Both add setup machinery or maintainer
config. The chosen direction is to remove the Windows special case
without replacing it.

## Options considered

1. **Per-product identifier patterns in product.yaml** — restores a
   strong tier 2 for Windows via config and lets other products opt in.
   Rejected: even maintainer-only yaml is extra machinery; end users
   must not get more configuration, and we chose not to auto-detect.
2. **Auto-detect patterns from the wizard minifetch corpus** — no user
   knob; system writes patterns at materialize. Rejected: abandoned
   before implementation in favor of a smaller, honest core.
3. **Document the Windows limitation only** — zero code risk. Rejected:
   leaves the product-agnostic claim false in behavior.
4. **Remove KB from grouping and shared extract/prompt path (chosen)** —
   entity → title simhash for every product. No new config. Windows may
   group worse when no feature entity is present; accepted tradeoff.

## Decision

- `compute_group_key` is entity (with product) → title simhash only.
- Remove `KB_RE` / `kb_numbers` from `pipeline.extract` and from default
  classify prompt hints.
- Stop writing meaningful `kb_numbers` on new `regex_extractions` rows
  (write empty JSON); leave the column for historical warehouses
  (forward-only — no drop).
- Delete unused `HARDWARE_TYPES` in `pipeline/group.py`.
- Drop KB F1 from eval gates / summaries.
- Keep `_group_label` support for legacy `kb:…` group keys already
  stored in old reports.
- Do **not** add identifier-pattern facts, wizard fields, or auto-detect.

CVE and build-number regex extraction remain (not used for grouping).
`windows_version_*` context columns are unchanged.

## Rationale

Honesty and simplicity beat preserving a Windows-only grouping
advantage. Removing the special case aligns runtime behavior with the
product-agnostic claim without asking operators to configure anything.

## Consequences

- Windows issues that previously merged only on shared KB numbers will
  split unless a shared feature entity is present or titles simhash-match.
- Golden-set / label-helper KB fields become inert; eval no longer gates
  on KB F1.
- Reintroducing a strong cross-item ID tier later needs a new ADR
  (patterns, embeddings, or similar) — do not quietly hardcode another
  vendor ID shape in core.

## Revisit if

Grouping quality on update-style feedback (patch IDs, ticket IDs) is
measurably worse across products and a product-agnostic approach (not
Windows-only) is designed.
