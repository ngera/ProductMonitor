# ADR-0028: Per-item content_type (supersedes author_intent)

- **Status:** Accepted
- **Date:** 2026-09-16
- **Area:** data / plugins
- **Supersedes conceptually:** the `RawItem.author_intent` /
  `items.author_intent` field introduced in ADR-0021's neighborhood.
  See "Collapsing the two axes" below.

## Context

`content_type` (`user_feedback` | `media_coverage`, ADR-0021) is
declared today on `SourceManifest`. It describes what a source *can*
produce. No code reads it at runtime — the digest section split,
credibility weighting, and attribution rendering all key off
`source_category` or `author_intent` instead. In practice every
first-party source is homogeneous — a single source yields one kind
of thing — so the manifest tag is inert but not misleading.

Two forces make this shape wrong going forward:

**1. Real sources are mixed.** An Ars Technica article about a
Windows bug is media coverage; the 200 comments under it are
user feedback about the same bug. Today one source (an "Ars"
scraper) would tag its whole stream as `media_coverage` and the
user-feedback signal in the comments would be silently
mis-bucketed. The same problem manifests for The Verge, Bleeping
Computer, Substack authors with comment sections, Product Hunt
launches, YouTube videos + comments, and the Reddit link-post case
(the linked article is media; the discussion is user feedback about
that media).

**2. Third-party plugins are on the horizon.** ADR-0027 shipped the
conformance kit and #11 (entry-point discovery, template repo) is
imminent. Locking in a "one content_type per source" contract now,
then breaking it in three weeks when the first mixed source ships,
forces every third-party plugin author through a v1→v2 kit
migration. Getting the contract right BEFORE we recruit contributors
is a one-time cost; getting it wrong is an every-contributor cost.

The user has confirmed no backfill of existing data is required —
warehouses + reports + products will be wiped once this ships. That
removes the biggest category of migration complexity from scope and
makes some downstream simplifications (dropping `author_intent`)
free.

## Decision (with baked-in answers from 2026-09-16 Q&A)

Source-declared, item-emitted `content_type`. Every `RawItem`
carries a required `content_type` field; the pipeline routes per-item.

### Data model changes

- **`pipeline.models.RawItem`** — new REQUIRED field:
  ```python
  content_type: ContentType   # "user_feedback" | "media_coverage"
  ```
  No default; every yield site sets it explicitly. Sources that
  produce a single content_type set the manifest's sole declared value
  on every yielded item — trivial pass-through in most first-party
  sources.

- **`items.content_type VARCHAR NOT NULL`** — new column on the
  warehouse `items` table. Additive migration via
  `_migrate_items_columns`. No backfill script because fresh-start
  reset happens before first run under the new schema.

- **`items.author_intent` DROPPED, `RawItem.author_intent` REMOVED.**
  See "Collapsing the two axes" below.

- **`SourceManifest.content_types`** — kept. Its role is now:
  - Declares which `content_type` values are legal on items this
    source yields (conformance kit + runtime validate).
  - Feeds UI grouping on `/products/{id}/sources`.

### Collapsing the two axes (Q&A decision #4)

`author_intent` (`editorial` / `user_original` / `user_reply`) is
removed. Rationale: it overlapped with `content_type` on the
"editorial-ness" axis and with `is_reply` on the "reply-vs-top-level"
axis. The three consumers reduce cleanly:

| Old `author_intent` value | Replaced by |
|---|---|
| `editorial` | `content_type = "media_coverage"` — the piece is an authored article/statement regardless of source venue. |
| `user_original` | `content_type = "user_feedback"` + `is_reply = False`. |
| `user_reply` | `content_type = "user_feedback"` + `is_reply = True`. |

Signal we lose: **an editorial voice from a non-editorial venue** —
e.g. a Microsoft staff post inside a community forum. Under the new
model that post gets tagged `content_type = "media_coverage"` at
yield time (the MS Community source's `is_official_voice` check
promotes it), and the fact that it lives in a community forum is
implicit from the source id. Deliberate simplification; readable
attribution wins over signal we never rendered anyway.

Removals in scope:
- `pipeline/normalize.py` — `_INTENT_RULES`, `_author_intent()`,
  the `author_intent` row field.
- `items.author_intent` column — DROP in migration.
- Templates that render `item.author_intent` — key off
  `content_type` + `is_reply` instead.
- Any classify prompt text that references `author_intent` — reword
  to `content_type` semantics.

The MS Community source specifically ships with a per-item
content_type decision: `is_official_voice` → `media_coverage`, else
`user_feedback`. Its manifest declares both content_types.

### Credibility weighting (Q&A decision #1)

New GLOBAL defaults, applied when neither the source nor the operator
sets a per-content-type override:

```python
# pipeline/score.py
_DEFAULT_CREDIBILITY_BY_CONTENT_TYPE = {
    "media_coverage": 0.5,
    "user_feedback": 0.8,
}
```

Resolution chain at score time (highest priority first):

1. **Operator per-instance per-type override** in `sources.yaml`:
   ```yaml
   sources:
     - id: ars
       type: ars_scraper
       credibility_weight_by_content_type:
         media_coverage: 0.5
         user_feedback: 0.8
   ```
2. **Operator per-instance single weight**: `credibility_weight: X`
   applies to any content_type not covered above.
3. **Global default for the item's content_type** from
   `_DEFAULT_CREDIBILITY_BY_CONTENT_TYPE`.

No manifest-level defaults (rejected option b): plugin authors don't
have to think about credibility numbers unless they want to. The
globals give correct out-of-box signal.

### Section routing (Q&A decision #2)

Both:

- **Area sections unchanged.** Bugs / features / positive / negative /
  workaround still contain items from all sources regardless of
  `content_type`. A media article about a bug lands in Bugs alongside
  user reports. This preserves the "one place to see everything about
  bug X" reading pattern.
- **New "Press Coverage" top-of-digest summary block.** Above the
  area sections, lists the top N `media_coverage` items of the week
  (ranked by score). Reader sees a compact "here's what the press
  wrote about the product" block before drilling into
  bugs/features/etc. `N` defaults to 5, configurable via
  `digest.press_coverage_top_n` in `app.yaml`.
- No double-count in item totals. The header shows total items across
  the digest; the press block sits above the areas without inflating
  counts. Media items appear TWICE in the report (once at the top,
  once inside their area section), and the template makes that
  obvious via a small "featured above" marker.

Requires:
- `report_templates/digest_v2.html.j2` — new section block above the
  existing area rendering.
- `pipeline/digest/sections.py` — a `press_coverage_items(week_id,
  top_n)` helper that returns the top-N media_coverage items.
- Config surface: `digest.press_coverage_top_n` (default 5),
  `digest.press_coverage_enabled` (default true; operator can hide
  the block for a product that has no media sources).

### Fresh-start reset (Q&A decision #3)

Two-part safety net:

- **`scripts/reset_for_content_type_migration.py`** — dry-run by
  default (`--commit` to actually delete). Removes:
  - `data/<pid>/warehouse.duckdb`
  - `data/<pid>/state.sqlite`
  - `data/<pid>/raw/`
  - `data/<pid>/run_logs/`
  - `reports/<pid>/`

  Keeps `products/<pid>/` (config). Re-runs `init_db` for each
  product afterward so the new schema is live. Follows the same
  dry-run-by-default pattern as `scripts/prune_data.py`.

- **Runtime detection in `pipeline/run.py`** — startup check queries
  the warehouse schema; if `items` lacks the `content_type` column
  OR still has `author_intent`, the run refuses to start and prints:
  ```
  ERROR: warehouse schema is pre-ADR-0028. Reset before continuing:
    python scripts/reset_for_content_type_migration.py --commit
  ```
  No auto-deletion. The refusal-to-run guard is what makes forgetting
  the reset non-catastrophic — you'd otherwise run against a
  schema-incompatible warehouse and see confusing errors deep in
  normalize.

### Conformance kit assertions (ADR-0027 extension)

Two new contract tests in `sources/testkit/conformance.py`:

1. `test_every_item_has_content_type` — every yielded `RawItem` has
   `content_type` set. Structurally enforced by the required field,
   but the test surfaces a friendly error if a plugin subclasses
   `RawItem` and provides a default.

2. `test_content_type_in_declared_manifest` — every yielded
   `content_type` value is in `MANIFEST.content_types`. Catches
   sources that lie about their capabilities.

3. `test_multi_content_type_source_actually_yields_both` — if
   manifest declares two content_types, the fixture-driven run must
   yield at least one item of each declared type. `pytest.skip()`
   for sources whose default fixture only produces one type
   (author extends the fixture to exercise the other).

### The Reddit link-post subtlety (unchanged)

Deliberately called out because it will confuse contributors.

- A Reddit link post to `arstechnica.com/foo` — the LINK POST
  itself (submitter's title, discussion) is `user_feedback`. The
  linked article is a separate document; if we also fetch Ars, that
  article shows up as `media_coverage` under `ars`. Canonical-URL
  dedup (ADR-0024) collapses only URL-identical items — different
  URLs, different items. Correct.
- Reddit stays `content_types=["user_feedback"]` — it doesn't
  produce self-contained articles.
- A future scraper that fetches article + comments from a news site
  declares `content_types=["media_coverage", "user_feedback"]` and
  yields per-item. That is the case this ADR unlocks.

## Rationale

- Reflects reality — one API call, mixed items.
- Correct for scoring (per-content-type credibility).
- Correct for the digest (press coverage as its own reading block).
- Right time to change the contract (no downstream plugin authors
  yet; kit v2 is free).
- Cheap given the fresh-start reset.
- Collapsing `author_intent` removes an inert axis; loses a signal
  we never rendered.

## Consequences

**Easier:**
- Writing a mixed source is one plugin, one fetch loop, N+1 items.
- Report distinguishes "press wrote" vs "users said" via the
  top-of-digest press block.
- Third-party plugin contract stays stable through #11.
- One axis (`content_type`) to reason about at attribution time.

**Harder:**
- Every first-party source updated in one commit (~10 files, one
  line each — trivial). `author_intent` removal is a separate
  same-wave commit that touches normalize.py + templates.
- `sources.yaml` config surface grows an optional
  `credibility_weight_by_content_type` map. Optional.
- New digest template section + config keys.
- Reset script + startup guard — operator has a mandatory manual
  step (running the reset) between pulling this change and running
  the pipeline again.

**Not changed:**
- `SourceManifest.content_types` shape.
- `source_category` — stays UI-grouping only.
- Persistent-issue clustering (canonical-URL-based).
- `RawItem.url` required per §13.
- `is_reply` field (subsumes user_original/user_reply distinction).

## Implementation sequence

1. **ADR-0028 accepted** (this file). ✔
2. **Schema + models:**
   - `RawItem.content_type: ContentType` required.
   - `items.content_type VARCHAR NOT NULL` — migration.
   - `items.author_intent` — DROP migration.
   - Remove `RawItem.author_intent` field.
3. **Reset infrastructure:**
   - `scripts/reset_for_content_type_migration.py` (dry-run default).
   - `pipeline/run.py` startup schema check + refusal message.
4. **First-party source updates** — all ~10 sources set
   `content_type` on every yield. Most set the manifest's sole
   declared value; MS Community sets per-item based on
   `is_official_voice`.
5. **Remove `author_intent` code paths:**
   - `pipeline/normalize.py` — drop `_INTENT_RULES`, `_author_intent`.
   - Templates — replace `author_intent` reads with
     `content_type` + `is_reply`.
   - Classify prompts — reword.
6. **Consumers:**
   - `pipeline/score.py` — global content-type defaults + resolution
     chain.
   - `pipeline/digest/sections.py` — `press_coverage_items(...)`.
   - `report_templates/digest_v2.html.j2` — top-of-digest press block.
   - `pipeline/aggregate.py` — verify area rollups unaffected (they
     are; content_type is orthogonal to area).
7. **Conformance kit** — three new tests, MS Community fixture
   exercises the mixed case.
8. **MS Community source** — declares
   `content_types=["media_coverage", "user_feedback"]`, sets per-item
   `content_type` based on `is_official_voice`. Proof point.
9. **Docs (local only per docs/ gitignore):** ADR-0028 (this),
   `PLUGIN_AUTHORS.md` update, `OPERATING.md` reset-script section.

Effort estimate: 2 days. Step 2-3 is a morning; step 4-5 is an
afternoon (mostly mechanical); step 6-8 is a day; step 9 is polish.

## Revisit if

- Two content_types isn't enough (e.g. distinguishing "primary
  reporting" from "aggregation" within `media_coverage`). Extend
  the `ContentType` Literal.
- Removing `author_intent` bites — an editorial-voice-in-user-forum
  case turns out to matter enough that content_type + source id
  can't recover it. Add it back as `voice: str` (separate axis) or
  merge into content_type sub-values.
- Press-block-plus-area-inclusion feels redundant to readers. Toggle
  `digest.press_coverage_top_n = 0` to hide the block per-product.
