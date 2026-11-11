# ADR-0008: Drop-in plugins require explicit `--trust-plugins-dir` opt-in

- **Status:** Accepted (implemented in `daad888`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** infra (security)
- **Plan reference:** [POST_V1_PLAN.md §4.14](../POST_V1_PLAN.md)

## Context

[ADR-0001](0001-plugin-discovery.md) accepts three plugin discovery
paths: built-in, drop-in `plugins/*.py`, and entry-point (pip-installed).

Drop-in and entry-point plugins execute arbitrary Python at startup.
That's the entire point: they extend the pipeline.

The threat: an attacker who can write files to `plugins/` (or convince
a user to copy a file there) can run arbitrary code as the pipeline
process. For a personal tool, this is a common security posture — the
user controls the machine. But we're planning to publish
`PLUGIN_AUTHORS.md` and encourage third-party plugin distribution. That
turns "copy this file to `plugins/`" into a real supply chain vector.

Entry-point plugins have marginally better provenance (a package was
`pip install`ed), but a malicious PyPI package is still possible. This
is a widespread problem; no personal-tool project fully solves it.

## Options considered

1. **No mitigation.** Drop-in `plugins/*.py` loaded automatically at
   startup. Simplest; most dangerous.

2. **Explicit trust flag.** Startup accepts `--trust-plugins-dir=<path>`.
   Without it, drop-in scan is skipped. Entry-point plugins still load
   (they were explicitly `pip install`ed, which is user intent). Users
   who want drop-in must consciously opt in.

3. **Sandbox drop-in plugins.** Load in a subprocess with restricted
   permissions (e.g., seccomp, subprocess with limited env). Actually
   safe. But: complex, cross-platform pain, and breaks the "plugin is
   just Python" ergonomics.

4. **Marker file requirement.** `plugins/.trusted` marker file
   required; if absent, scan is skipped. Same effect as flag but
   in-tree rather than CLI. More discoverable.

## Decision

Option 2. Startup CLI accepts `--trust-plugins-dir=<path>`. Without
it, `plugins/*.py` scan is skipped. Entry-point plugins still load
(user consciously installed them).

The `PLUGIN_AUTHORS.md` doc explicitly warns:

> **Security note:** Any plugin dropped into `plugins/` executes as your
> pipeline user with your `.env` credentials. Only add plugins from
> sources you trust. To enable drop-in scanning, start the webui or
> pipeline with `--trust-plugins-dir=./plugins`. Without the flag,
> only built-in and pip-installed plugins are loaded.

For scripts that expect drop-in plugins to just work (like local dev),
users add `TRUST_PLUGINS_DIR=./plugins` to their `.env` — same effect
without repeating the flag.

## Rationale

- **Explicit consent.** A user who runs `python -m webui.app` without
  the flag can't be tricked by a plugin someone dropped in — the code
  simply doesn't load.
- **Documentation matches behavior.** We can honestly say "drop-in
  plugins require opt-in" instead of hoping users read the security
  warning.
- **Doesn't break entry-point plugins.** Users who `pip install`
  something did so consciously; those plugins load without the flag.
- **Sandbox alternative (option 3) is over-engineering.** Cross-platform
  sandboxing is hard; the value doesn't justify the complexity for a
  personal tool.

## Consequences

**Makes easy:**
- Defensible security story for `PLUGIN_AUTHORS.md`.
- Users who want the safety can just not set the flag.
- Users who want convenience add the flag once.

**Makes hard or expensive to reverse:**
- Some users will forget the flag and wonder why their plugin isn't
  loading. Startup message must clearly say: "plugins/ dir found but
  ignored — pass --trust-plugins-dir to enable."
- CI / scripts running the pipeline need to add the flag consistently.
  Mitigation: `TRUST_PLUGINS_DIR` env var alternative documented.

**Locks us into:**
- The flag as the trust signal. If we later add a real sandbox, the
  flag becomes redundant but remains for backward compat.

## Revisit if

- We ship real sandboxing for drop-in plugins. Flag can then default
  to on (safe) and be renamed or removed.
- Users routinely miss the flag and complain. Consider default `on`
  with an in-tree `.trusted` marker file requirement (option 4).
- We remove the drop-in path entirely and go entry-point-only. Then
  the flag is meaningless.
