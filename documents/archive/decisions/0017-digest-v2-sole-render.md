# ADR-0017: Digest v2 replaces per-area and per-comments pages as the sole report artifact

- **Status:** Accepted
- **Date:** 2026-07-27
- **Area:** frontend / infra

## Context

The current report ([pipeline/render.py](../../pipeline/render.py))
produces three artifact types per run under `reports/<product>/<week>/`:

- `index.html` — landing page with area cards and rollup numbers
- `area_<area>.html` — per-taxonomy-area drill-down
- `comments_<area>.html` — per-item comment listing per area

The digest v2 design ([report_v2_design.md](../report_v2_design.md))
replaces this structure with:

- `index.html` — digest with nine thematic sections
- `{positive,negative,bugs,features}_details.html` — per-section detail
  pages grouped by persistent-issue-id (see [ADR 0016](0016-persistent-issue-stage.md))
- `data/trend_*.png` — matplotlib charts

The per-area / per-comments pages don't fit the new flow. Sections are
now content-type-driven (bugs, features, positive/negative sentiment,
churn) rather than taxonomy-area-driven. Drill-down is per-section-and-
persistent-issue, not per-area.

Constraints:
- Old runs' URLs are bookmarked and linked externally; breaking them is
  hostile.
- The old templates and their render code are ~500 lines. Maintaining
  them alongside the digest doubles the surface area.
- Users have not requested the taxonomy-area drill-down as an ongoing
  need (confirmed across the 9-round digest v2 design conversation).
- Persistent-issue-ids are forward-only ([ADR 0016](0016-persistent-issue-stage.md)),
  so retroactively regenerating old runs as digests would produce
  lower-fidelity output than the current per-area pages.

## Options considered

1. **Remove old per-area/per-comments render entirely for new runs;
   leave existing artifacts frozen at their URLs.**
   - Pros: minimum code; single artifact per new run; old links keep
     working; no migration risk.
   - Cons: users who preferred the area drill-down lose it.

2. **Keep old pages as a legacy "raw data" view, linked from the digest
   footer.**
   - Pros: escape hatch for users who miss it.
   - Cons: two render paths to maintain forever; two sources of truth
     for the same run; users default to whichever is more prominent.

3. **Repurpose the old templates into the new per-section detail pages.**
   - Pros: reuse existing render.py infrastructure.
   - Cons: templates change substantially; less "reuse" than "rewrite
     inside the same file"; couples the digest architecture to render.py's
     existing structure.

4. **Regenerate every old run on upgrade to the new layout.**
   - Pros: consistent look across history.
   - Cons: long background job; costs headline LLM tokens for every
     historical item; risks partial failure leaving mixed-format runs;
     the persistent-issue stage is forward-only per ADR 0016, so old
     runs' digests would still be lower-fidelity.

## Decision

**Option 1** (for new runs) + **freeze old runs at their existing URLs.**

- `pipeline/render.py`'s `_render_area` and `_render_comments` and their
  templates ([report_templates/area.html.j2](../../report_templates/area.html.j2),
  [report_templates/comments.html.j2](../../report_templates/comments.html.j2))
  are deleted in Slice 3 of the digest v2 rollout.
- The old `index.html` template stays as legacy for pre-v2 runs (already
  written to disk; not regenerated). New runs write the digest v2
  `index.html` instead — a different template file, same URL slot.
- Existing report directories are untouched. Users navigating to a
  pre-v2 run URL see the old layout.
- Runs list page shows a visual break at the v2 cutover so users know
  why old and new runs look different.

The entire behavior is gated by the `digest_v2_enabled` feature flag
per [ADR 0006](0006-feature-flags-off-by-default.md). Flag off = current
render pipeline runs unchanged.

## Rationale

- **Single artifact per run** simplifies both the codebase and the mental
  model. When a user clicks a run link they get exactly one report, not
  a choice of "new digest" or "legacy view."
- **Freezing old runs is honest.** They were generated under the old
  design assumptions. Retroactively "upgrading" them would produce
  low-fidelity digests (per [ADR 0016](0016-persistent-issue-stage.md)
  the persistent-issue stage is forward-only, so old runs would show only
  current-week counts even in the new layout — misleading).
- **Two render paths is a maintenance trap.** Option 2 sounds friendly
  but creates a permanent obligation to keep both templates working. The
  same trap that made the wizard v1/v2 split painful.
- **Repurposing (Option 3) is worse than a clean rewrite.** The old
  templates carry assumptions (per-area drill-down, single-week counts,
  no persistent-issue grouping) that would fight the new design.

## Consequences

**Easy:**
- Slice 3 is a template rewrite, not a template negotiation.
- Onboarding new users to the report — one layout to explain.
- Deleting dead code — `_render_area`, `_render_comments`, their
  templates, `weekly_rollup.top_group_keys_json` (still used by
  `_render_area`, becomes unreferenced), the `ITEM_DISPLAYING_TEMPLATES`
  attribution-validator list.

**Hard / expensive:**
- **Users who preferred the taxonomy-area drill-down lose it.**
  Mitigation: the digest's Bugs / Features / etc. sections show
  canonical issues clustered by persistent-issue-id, which is arguably
  a better lens than "area" for most reading. If demand returns we can
  add an area filter chip to detail pages as a v2 refinement.
- **Old and new runs look different.** The visual break in the runs list
  will confuse users for a few weeks after the upgrade. A short banner
  on the runs list explains it.
- **Some legacy code paths become dead once old runs age out** of
  practical use. We may want to garbage-collect the pre-v2
  `report_templates/` in a future cleanup pass.
- **Acceptance gate (`_check_acceptance_gate` in render.py) needs to
  survive.** Slice 3 relocates the gate check into the digest build path
  or a shared helper; deleting render.py wholesale would silently drop
  it.

## Revisit if

- Users request per-area drill-down as an ongoing need (a filter chip
  on the detail pages is cheaper than reviving the old templates).
- We need to regenerate old runs to compare pre-v2 and post-v2 metrics
  during a release retrospective — could add a one-shot
  `feedback-monitor rebuild-report <run_id>` command (per
  [ADR 0011](0011-console-entry-point.md)'s subcommand model).
- The digest v2 layout itself needs replacing — this ADR covers
  removing the old pages, not locking in the new ones. A future ADR
  would supersede *this one* with the new render.
