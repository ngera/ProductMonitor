# ADR-0019: Rich competitor schema — {name, aliases, color}

- **Status:** Accepted
- **Date:** 2026-07-29
- **Area:** data / infra / frontend

## Context

report_v2_design.md §7.2 specifies competitors as rich objects:

```yaml
competitors:
  - name: macOS
    aliases: [mac, apple silicon, os x]
    color: "#a2a2a2"
```

Two competitor-touching features on the digest need those fields:

- Aliases feed the Competition section's mention-count match so *"Mac
  crashed on me"* counts toward *macOS* (§4.7).
- Explicit `color` pins the sentiment trend line's color across runs so
  *"the blue line is ChromeOS"* stays true even as competitors are added
  or removed (§4.2, §7.2).

Until now, `Product.competitors` was `list[str]` — only the name. The
mention-count query used a single `LIKE '%name%'`, so a Mac-only post
wouldn't count toward macOS unless the user wrote the full name; the
trend-chart competitor lines got colors from a hard-coded 4-entry
palette in `pipeline/digest/charts.py` that reshuffled if a user
reordered their competitors list.

## Options considered

1. **Name only, hint from context.** Cheapest — keep the string list,
   have the classifier's summary field carry the fuzzy match. Rejected:
   the design explicitly picks rich objects; also relies on the LLM
   catching aliases it may not know about.
2. **Name + aliases, auto color.** Compromise — pin an alias list per
   competitor but derive color from a palette-index. Rejected: same
   reshuffle problem as today; users complained about "why did my
   competitor's line change color mid-quarter?"
3. **Rich objects with all three fields.** What we did.

## Decision

`Product.competitors` becomes `list[dict[str, Any]]` with each entry
shaped `{name: str, aliases: list[str], color: str | None}`.

Backward compat: `pipeline.product._clean_competitor_list` accepts
either plain strings OR dicts and normalizes to the rich shape at load
time. Existing `products/<id>/product.yaml` files with `competitors:
[X, Y]` continue to load without a migration — each string is auto-
lifted to `{name: X, aliases: [], color: null}`.

Effective color falls back to a small palette when `color` is null, via
`pipeline.product.competitor_color(competitor, index)`. Same palette
`charts.py` used before, so pre-migration products render with the same
line colors they had, only now stable across runs.

Downstream consumers updated in one pass:

- **Text search** (`pipeline/digest/sections.py::competition_rows` and
  `pipeline/digest/charts.py::_competitor_sentiment_per_bucket`) — the
  single `LIKE '%name%'` becomes `(LIKE ? OR LIKE ? OR ...)` across the
  competitor's name + every declared alias, all lowercased.
- **Chart lines** (`pipeline/digest/charts.py::build_charts`) — the
  palette-index color source is replaced with `competitor_color(comp, i)`,
  which prefers the explicit color and only falls back to the palette
  when unset. Labels use `competitor_display_name(comp)`.
- **Prompt summarizers** (`pipeline/taxonomy_proposal.py`,
  `pipeline/stream_suggestions.py`) — extract `.name` via
  `competitor_display_name` so they don't emit `{'name':...}` when the
  LLM's context lists competitors.
- **WebUI** — `/products/<id>/profile` gets a structured competitor
  editor (parallel `<input name="competitor_name">`, `competitor_aliases`,
  `competitor_color` arrays) instead of the plain-line chip field.
  `/products/<id>/report` shows the competitor list read-only with a
  color swatch per row and a link back to the profile page.

`pipeline/wizard_v2.WizardV2Draft.competitors` retains a permissive
`list[Any]` — the wizard itself still collects plain names from the
LLM, and materialization pipes them through `validate_facts` where
`_clean_competitor_list` does the lift. This keeps
`webui/templates/wizard/_chips.html` (which renders each entry as
`{{ v }}`) working during the wizard step without a shape change to the
chip partial.

## Rationale

The design spec is explicit and the two payoffs (alias-driven matching,
stable colors) are both real user complaints, not hypothetical. The
migration cost is bounded because we chose to auto-lift plain strings
at load time rather than forcing every existing `product.yaml` to be
rewritten — that keeps this change zero-friction for the one existing
product on disk and any hand-authored YAMLs.

The alternative — separate `competitor_aliases.yaml` and
`competitor_colors.yaml` files — was rejected as ceremony. All three
attributes live on the same object because they are all "how do I
identify and display this competitor" facts.

## Consequences

Easy now:
- Users can pin colors and add aliases inline without a schema fight.
- Competition-section mention counts pick up more real signal (e.g.
  `Mac` now counts toward `macOS` if aliases are set).
- Chart line colors stay stable across product edits.

Harder now:
- Consumers reading `product.competitors` need to know each element is
  a dict, not a string. Existing helpers (`competitor_display_name`,
  `competitor_color`) hide this from most callers but any hand-written
  string comparison against the list is a latent bug.
- The chip-based wizard step for competitors still collects names only.
  Aliases and colors are configured post-materialization on
  `/products/<id>/profile`. A user who expects to set colors during the
  wizard is surprised.

Locked in:
- The `list[dict]` shape is the on-disk schema — any future migration
  (e.g. adding `logo_url`) is an additive dict key.
- `competitor_color`'s 4-entry palette is the fallback contract until
  a subsequent ADR replaces it.

## Revisit if

- The wizard grows a "configure competitors" step and users want to set
  colors/aliases there instead of the profile page.
- Competitor identity graduates to a first-class table with FK-backed
  mention counts (would supersede the text-search approach in
  `competition_rows`).
- Aliases start colliding across competitors and precision falls below
  a usable threshold — at which point a controlled vocabulary or
  per-item entity matching becomes cheaper than heuristics.
