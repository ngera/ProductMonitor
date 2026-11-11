# ADR-0021: Source taxonomy — source_category + content_types on SourceManifest

- **Status:** Superseded in part by ADR-0031 (dual-list UI rule only;
  manifest taxonomy fields remain)
- **Date:** 2026-08-05
- **Area:** data / frontend

## Context

The source plugin surface had grown to 13 first-party plugins plus a curated
media catalog with ~16 entries. The UI grouped them ad-hoc:

- **Edit Sources page** — one card per registered plugin, with all curated
  media feeds squished into a single "Media Coverage Sources (rss)" card
  carrying 6+ streams as an opaque textarea.
- **Wizard sources step** — one "Suggested" panel + one "Other ready" panel,
  no notion of "these are all news feeds" vs "these need OAuth".
- **Connections page** — three hardcoded groups (`rss_feeds`,
  `scrapecreators`, `custom_auth`) defined in `webui/app.py` with
  hand-maintained plugin_id sets that drifted from reality (HN was in
  `rss_feeds` even though it uses an HTTP API, not RSS).

Three concrete problems this created:

1. Each media publication (The Verge, Ars Technica, PC Gamer…) was
   effectively invisible in the UI — the operator saw one "Media Coverage"
   card and had to remember which feed URLs it wrapped.
2. Users had no visual cue that Reddit RSS is user-comment content while
   Wired RSS is media coverage — both went under an "RSS" umbrella.
3. Adding a new plugin required editing the hardcoded `SOURCE_GROUPS` set
   or having the plugin silently default into `custom_auth`.

The operator wants sources organized primarily by *what kind of content
they carry* (user feedback vs. media coverage), with a secondary grouping
by *how they're set up* (RSS feed vs. custom API vs. third-party scraper).

## Options considered

1. **Bake taxonomy into each `SourceManifest`** — add `source_category`
   (setup mechanism) + `content_types` (list, one or both) as manifest
   fields. UI reads them at render time. Third-party plugins declare their
   own values.
   Pros: single source of truth, drop-in plugins get first-class treatment.
   Cons: schema change; existing plugin manifests need one-time edits.
2. **Central override table in `config/source_taxonomy.yaml`** — plugins
   declare defaults; operator can override globally.
   Pros: overridable without redeploy.
   Cons: two sources of truth, more surface area, unnecessary flexibility
   (no operator has asked to override).
3. **Keep it in `webui/app.py` hardcoded** — expand `SOURCE_GROUPS` to
   cover the new content-type distinction.
   Pros: no schema change.
   Cons: continues the drift problem; third-party plugins can't declare
   their own semantics.

## Decision

Option 1. Two additive fields on `SourceManifest`:

```python
source_category: Literal["rss_feed", "custom_source", "third_party_scraper"]
                = "custom_source"
content_types: list[Literal["user_feedback", "media_coverage"]]
                = ["user_feedback"]
```

Each first-party plugin declares both explicitly. `media_sources.yaml`
catalog entries also carry a `content_types` list (defaults to
`["media_coverage"]`).

UI grouping rules:

- **Edit Sources page** — content_type primary (User Feedback / Media
  Coverage sections), source_category secondary (RSS Feeds / Custom
  Sources / Third-party Scrapers sub-headers). Plugins tagged with both
  content_types appear **once** under their primary content_type, with
  chips for every type (ADR-0031; was dual-listed before that).
- **Wizard sources step** — same primary/secondary grouping; one
  checkbox per plugin (ADR-0031).
- **Connections page** — source_category primary (setup mechanism is
  what matters here since Connections is about credentials, not usage).

The generic `rss` plugin is **excluded** from the Edit Sources card list.
Each media catalog entry becomes its own card (identity = feed URL). This
turns "1 rss card wrapping 16 feed streams" into "16 individual feed
cards" — the operator sees each publication as its own toggleable source.
Custom RSS URLs not in the catalog can only be added via the raw YAML
editor (deferred until asked for).

Backing storage on the product's `sources.yaml` is unchanged: catalog
picks materialize as streams on a single `rss` source instance,
preserving stream names + cursor identity across saves.

## Rationale

Content type is the axis users think in ("I want to hear from users on
Reddit" vs "I want to track what Wired writes"), so it belongs at the top
of the Edit Sources visual hierarchy. Setup mechanism is what matters at
the credentials layer, so Connections groups by it. Both are declared on
the manifest so drop-in plugins pick their placement without touching
webui code.

The `custom_source` + `["user_feedback"]` defaults on the schema mean
existing third-party plugins keep working without edits — they just land
in the "Custom Sources / User Feedback" cell, which is the most common
placement and unlikely to surprise anyone.

Excluding the `rss` plugin from Edit Sources cards is the specific
change the operator asked for: media publications listed separately, not
lumped under a generic "rss" label. Media catalog entries had already
been curated in `config/media_sources.yaml`; promoting each to a
first-class UI card was mostly a display-layer change.

## Consequences

**Easier now:**
- Adding a new plugin — the manifest declares its own taxonomy, no
  central `SOURCE_GROUPS` edit needed.
- Third-party (drop-in) plugins get the same first-class treatment as
  built-ins.
- Operators can see each media publication as a distinct enable/disable
  toggle rather than editing a shared textarea of feed URLs.

**Harder / more work:**
- Any custom (non-catalog) RSS feed added by an operator via the raw
  YAML editor is invisible in the new UI; the catalog is now the
  canonical way to list media feeds. If we later want a "Add custom
  feed" UX, it's a follow-up.
- The Edit Sources save path now merges two card kinds (plugin cards
  with streams; catalog cards with per-URL toggles) into the same
  `sources.yaml` shape client-side. The server endpoint is unchanged,
  but any format regression requires updating the JS in
  `webui/templates/sources_form.html`.
- Plugins tagged with both `user_feedback` and `media_coverage` (hn,
  microsoft_community, producthunt) render TWICE on Edit Sources and in
  the wizard. JS keeps their checkboxes in sync; users see the same
  card in both content-type sections. This is intentional per the "by
  content_type primarily" grouping choice, but is denser than a strict
  one-appearance model would be.

**Migration & compat:**
- All first-party plugin manifests were updated in one pass (see
  `sources/*.py`). Existing product `sources.yaml` files need no
  changes.
- `config/media_sources.yaml` entries picked up an optional
  `content_types:` field (default `["media_coverage"]`); pre-existing
  entries load unchanged.
- The `WizardV2Draft.stream_suggestions` field on wizard drafts remains
  in the schema but is no longer populated (ADR-0021 landed alongside
  the removal of LLM stream-suggestion generation; see the same commit).

## Revisit if

- Operators start asking to add custom RSS URLs not in the catalog
  frequently → resurrect the generic `rss` card as an "Add custom feed"
  escape hatch on Edit Sources, or ship a UI for editing
  `config/media_sources.yaml`.
- A third content_type emerges (e.g. `paid_reviews` for App Store /
  Play Store) — expand the `ContentType` literal + section headers.
- Rendering the same plugin twice on Edit Sources becomes user-hostile
  (feedback: "why does HN appear in both sections?") — switch to
  source_category primary + a content_type filter chip, or force each
  plugin into exactly one content_type.
