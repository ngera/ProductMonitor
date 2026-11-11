# ADR-0031: Dual-tagged sources list once under primary content_type

- **Status:** Accepted
- **Date:** 2026-09-17
- **Area:** frontend
- **Supersedes:** ADR-0021 UI rule that dual-tagged plugins appear in both
  content_type sections (manifest `content_types` taxonomy unchanged)

## Context

ADR-0021 grouped sources by content type and placed plugins tagged with
both `user_feedback` and `media_coverage` (Product Hunt, Microsoft Tech
Community) into *both* wizard pick sections and Edit Sources taxonomy
sections. Operators saw "Product Hunt" twice on Step 3 pick sources and
assumed a bug. Checkboxes were JS-synced, but the duplicate row still
looked like two different sources.

## Options considered

1. **Keep dual listing** — preserves a literal reading of ADR-0021;
   operators learn "appears in both means covers both."
   Pros: no ADR change. Cons: ongoing confusion; duplicate rows.
2. **Drop one content_type from Product Hunt's manifest** — collapses the
   duplicate for that plugin only.
   Pros: local. Cons: lies about what the plugin emits (ADR-0028: posts
   are media_coverage, comments are user_feedback); doesn't fix Microsoft
   Community.
3. **List each plugin once under its primary content_type; chips show
   every type** — primary = first of `user_feedback` / `media_coverage`
   present on the manifest.
   Pros: one checkbox, taxonomy still visible, applies to all dual-tagged
   plugins. Cons: Media Coverage section no longer repeats the same plugin
   row (membership is via chip instead).

## Decision

Option 3. Wizard Step 3 pick list and Edit Sources taxonomy grouping place
each plugin card once under `primary_content_type`. Content-type chips on
the row/card still enumerate the full `content_types` list. Manifest
declarations and per-item `content_type` on RawItems are unchanged.

## Rationale

Operators pick *sources*, not taxonomy membership. Duplicate rows cost
more in confusion than they buy in section completeness. Chips preserve
the ADR-0021 signal that a source spans both kinds of content.

## Consequences

- Product Hunt / Microsoft Community appear once (under User Feedback when
  that type is listed first on the manifest).
- Sibling-checkbox sync JS on the wizard pick step is no longer required.
- Connections page grouping (by `source_category`) is unaffected.

## Revisit if

Operators ask to browse Media Coverage as a complete inventory that
includes every dual-tagged plugin as a full row again.
