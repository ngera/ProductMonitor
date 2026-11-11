# ADR-0001: Plugin discovery supports both drop-in files and entry points

- **Status:** Accepted (implemented in `daad888`, `ec6cdbe`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** infra
- **Plan reference:** [POST_V1_PLAN.md §4.1](../POST_V1_PLAN.md), D13

## Context

V1 registers sources via hardcoded try/except imports in
`sources/__init__.py`. Metadata (env vars, form fields, help text) lives
in `webui/app.py` as `CONNECTION_META` and `SOURCE_TYPE_META` dicts.
Adding a source means editing 3+ files.

[LOCAL_V1_PLAN §2](../LOCAL_V1_PLAN.md#2) originally planned for
`SourceManifest` + `importlib.metadata` entry points but shipped a
tactical shortcut. This ADR resolves the discovery mechanism now that
we're building the manifest system for real.

Stated project goal: "publish information for others to add plugins as
well." That means third parties need a real distribution story.

## Options considered

1. **Drop-in `plugins/*.py` only.** Simplest for hobby users. Zero
   packaging burden on plugin authors. But: distribution story is "share
   a `.py` file over GitHub gist" — awkward for real ecosystems, hard
   to version, no dependency isolation.

2. **`importlib.metadata` entry points only.** Standard Python
   pattern. Enables `pip install customer-feedback-plugin-X`. But:
   forces plugin authors to write `pyproject.toml`, publish to PyPI or
   git, understand entry-point declarations. Excludes casual hackers.

3. **Both.** Same `MANIFEST` schema. Drop-in scan for casual hackers;
   entry points for polished distribution. ~50 LOC extra in discovery.

## Decision

Both mechanisms, same schema. Registry scans:

1. `sources/*.py` (built-in)
2. `plugins/*.py` and `plugins/*/plugin.py` (drop-in, opt-in via
   `--trust-plugins-dir` — see [ADR-0008](0008-plugin-trust-flag.md))
3. `importlib.metadata.entry_points(group='customer_feedback.sources')`
   (pip-installable)

Each source of plugins produces the same `SourceManifest` objects; the
registry treats them uniformly downstream.

## Rationale

- Drop-in-only breaks the stated distribution goal. Third parties
  can't reasonably ship plugins without a pip story.
- Entry-points-only excludes the exact users this project was built
  for (hobbyists running the tool on their laptops who want to try a
  quick modification).
- The engineering cost of supporting both is small (~50 LOC in
  discovery); the maintenance cost is zero since both paths hit the
  same schema.

The manifest schema is the interface; the discovery paths are
implementations. Diverging discovery paths would be a real cost;
diverging *schemas* would not, and there's only one schema.

## Consequences

**Makes easy:**
- Third parties can `pip install customer-feedback-tiktok-scraper`
  and have it work.
- Hobbyists can drop a `.py` file in `plugins/` and iterate.
- Zero core code changes when adding a new plugin.

**Makes hard or expensive to reverse:**
- Two discovery paths to maintain. If Python entry-point machinery
  changes in a future release, we have to adapt.
- Manifest schema is now a public contract with versioning
  requirements (see `manifest_schema_version` in
  [POST_V1_PLAN §4.1](../POST_V1_PLAN.md)). Adding fields is fine
  (backward-compat default); removing or renaming fields is a breaking
  change requiring `manifest_schema_version` bump.

**Locks us into:**
- The `MANIFEST` at module level pattern. Deviating (e.g. dynamic
  registration) would confuse plugin authors.

## Revisit if

- Python's entry-point discovery is deprecated or gains major new
  semantics (`importlib.metadata` has been stable, low risk).
- We hit a real case where drop-in and entry-point plugins need
  different capabilities (currently: identical treatment).
- Plugin ecosystem grows to hundreds of plugins and we need a more
  discoverable registry (e.g., a web-based directory) — that layers
  above this ADR, doesn't supersede it.
