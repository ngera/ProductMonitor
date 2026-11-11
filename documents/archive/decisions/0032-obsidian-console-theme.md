# ADR-0032: Obsidian Console theme for the webui

- **Status:** Accepted
- **Date:** 2026-09-18
- **Area:** frontend

## Context

The webui chrome was a light, ad-hoc stylesheet embedded in `base.html`
with per-template `<style>` blocks and raw hex colours. The products list,
product dashboard, wizard, and admin pages did not share a component
vocabulary, so density, status treatment, and button hierarchy drifted.
A design pass produced static mockups under `documents/ui_mockups/` and an
implementation spec (`Claude outputs/ui_implementation_spec.md`) defining
the **Obsidian Console** theme: dark-first tokens, a light twin, shared
components, and no CDN webfonts (local-first / offline demo constraint).

Theme preference must survive server-rendered navigations without a flash
of the wrong palette. `localStorage` alone flashes on every full page
load; a cookie is visible to the server on the first paint.

## Options considered

1. **Keep the light inline stylesheet; polish pages independently** —
   Zero migration cost, but the mockups and density goals cannot be met
   without a shared token layer; duplication continues.
2. **CSS variables + `localStorage` theme toggle** — Client-only; every
   Jinja round-trip flashes the default theme until JS runs. Rejected
   for a form-post + redirect app.
3. **Obsidian Console tokens in `webui/static/app.css`, `pm_theme` cookie,
   `data-theme` on `<html>`** — Server knows the theme on render; toggle
   POSTs to `/settings/theme` and flips `dataset.theme` optimistically.
   Platform font stacks (no webfont). Matches the mockups and offline
   constraint.

## Decision

Adopt Obsidian Console as the sole webui theme. Ship tokens and components
in `webui/static/app.css`. Persist preference in the `pm_theme` cookie
(`dark` | `light`); omit `data-theme` when unset and let
`prefers-color-scheme` decide. No feature flag — this replaces the old
chrome rather than sitting beside it. Reuse existing `llm_usage` /
`cross_product_totals` for spend charts; do not add a second usage schema.

## Rationale

The mockups and spec are the visual source of truth; a cookie-backed
`data-theme` is the only approach that avoids FOUC with SSR. Platform
stacks keep `product-monitor demo` fully offline. Token accounting already
exists (ADR-0005); inventing a parallel table would violate
forward-only schema discipline for no gain.

## Consequences

- Every template must eventually drop raw hex and per-page `<style>`
  blocks; interim class aliases keep unported pages readable.
- Light and dark must both clear WCAG AA on the token pairs; changing a
  colour requires re-measuring.
- Theme cookie is single-tenant / local-only — no multi-user sync needed.

## Revisit if

- Operators need a third theme or brand-specific skins.
- Exact IBM Plex fidelity becomes worth vendoring ~500 KB of woff2.
- A multi-user / remote install appears (cookie-per-browser would then
  be insufficient).
