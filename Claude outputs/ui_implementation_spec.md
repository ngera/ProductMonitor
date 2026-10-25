# ProductMonitor UI — implementation spec

Theme: **Obsidian Console**. Companion to `documents/ui_mockups/*.html`.
Written 2026-09-18. Same relationship as `report_v2_design.md` ↔ `report_v2_mockup.html`.

---

## 0. How to use this document

The mockups in `documents/ui_mockups/` are the visual source of truth. They are static
HTML with a fixed 1280px frame, inline styles and hard-coded sample data — deliberately,
so they can be opened in a browser with no build step.

**Open them first.** This document gives the token values, component CSS and data
contract; the mockups give layout, copy tone and spacing. Where the two disagree, the
mockup wins for appearance and this document wins for naming and data.

Start at `documents/ui_mockups/index.html`, which links them all.

| Mockup | Width | Replaces / informs |
|---|---|---|
| `products.html` | 1280 | `webui/templates/index.html` |
| `product_dashboard.html` | 1280 | the Summary tab in `webui/templates/product.html` |
| `wizard_sources.html` | 1280 | `webui/templates/wizard/step_sources.html` (+ `_stepper.html`) |
| `admin_features.html` | 1280 | `webui/templates/admin_features.html` (+ `_admin_subnav.html`) |
| `products_light.html` | 1280 | same template, light theme |
| `product_dashboard_light.html` | 1280 | same template, light theme |
| `products_mobile.html` | 390 | same template, §8 breakpoints |
| `product_dashboard_mobile.html` | 390 | same template, §8 breakpoints |
| `wizard_sources_mobile.html` | 390 | same template, §8 breakpoints |
| `tokens.html` | 1280 | reference sheet — no template |
| `components.html` | 1280 | reference sheet — the pieces every template should reuse |

There is one template per row of pages: the light and mobile variants are **the same
Jinja template** rendered under a different `data-theme` or a narrower viewport. They are
separate files here only because a static mockup cannot demonstrate a media query or a
CSS variable swap.

The inline styles in the mockups are an artifact of how they were drawn. **Do not port
them as inline styles.** Section 3 below is the class layer they collapse into.

---

## 1. Scope

In scope: `webui/templates/**`, a new `webui/static/app.css`, a new icon sprite, and the
view-layer additions in section 6 needed to feed the new panels.

Out of scope: pipeline stages, report templates (`report_templates/`), the digest, the
DuckDB schema *except* the token-accounting table in §6.4.

Constraints that do not change:
- Jinja2 server-side rendering. **No build step, no JS framework, no npm.**
- Forms stay `POST` + redirect. No XHR except the existing `/api/refresh` poll.
- Everything must work offline (`product-monitor demo`), so **no CDN for fonts** — see §2.4.
- The whole UI must stay usable at 1280px and degrade sanely below it.

---

## 2. Design tokens

### 2.1 The CSS block

This goes at the top of `webui/static/app.css`, verbatim. Nothing below it, and no
template, may contain a raw hex value.

```css
:root {
  color-scheme: dark;

  /* surfaces */
  --bg:            #0B0E13;
  --surface:       #131821;   /* cards, header bar is --surface-raised */
  --surface-raised:#10141B;   /* top bar, sidebars */
  --surface-2:     #1A212C;   /* tags, secondary buttons, bar tracks */
  --inset:         #0F141C;   /* wells inside a card: quotes, code, mini-charts */

  /* lines */
  --line:          #252D3A;   /* card borders, table header rule */
  --line-soft:     #1E2530;   /* row dividers inside a card */
  --line-strong:   #2C3646;   /* input borders, secondary button borders */

  /* ink */
  --fg:            #E8ECF3;
  --fg-2:          #9AA6B8;   /* body secondary, descriptions */
  --fg-3:          #7C8A9E;   /* captions, column labels, units */
  --fg-4:          #55606F;   /* disabled */

  /* accent */
  --accent:        #3DDC97;
  --accent-hover:  #6FE9B5;
  --accent-ink:    #04231A;   /* text on an accent fill */

  /* status ink */
  --pos:           #3DDC97;
  --warn:          #F5B544;
  --neg:           #FF7A7A;
  --info:          #6BA6FF;
  --idle:          #55606F;

  /* status tinted surfaces: bg / border / ink */
  --pos-bg:   #12211B;  --pos-line:  #1E4436;  --pos-ink:  #6FE9B5;
  --warn-bg:  #241D0F;  --warn-line: #4A3A18;  --warn-ink: #F5B544;
  --neg-bg:   #2A1414;  --neg-line:  #5A2626;  --neg-ink:  #FF7A7A;
  --info-bg:  #101A26;  --info-line: #1E3A52;  --info-ink: #6BA6FF;

  /* banner variant of the warn tint — larger area, so a softer fill */
  --warn-banner-bg:   #1A1710;
  --warn-banner-line: #45361B;
  --warn-banner-ink:  #F5D9A0;  /* the bold lead-in */
  --warn-banner-body: #C9B58A;  /* the rest of the sentence */

  /* chart: categorical, pipeline stages */
  --cat-1:      #3987e5;  /* relevance */
  --cat-2:      #d95926;  /* classify  */
  --cat-3:      #199e70;  /* digest    */
  --cat-muted:  #5B6B83;  /* "setup / other" — not part of the validated set, see §7.1 */

  /* chart: status fills (larger areas than status ink, so stepped separately) */
  --chart-ok:   #35BE86;
  --chart-slow: #E0A23C;
  --chart-bad:  #E4595E;

  /* chart furniture */
  --chart-grid:  var(--line-soft);
  --chart-zero:  #3B4557;
  --chart-track: #1D2430;

  /* shape */
  --r-tag: 5px;  --r-ctl: 8px;  --r-card: 12px;  --r-pill: 999px;

  /* type */
  --font-ui:   "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
}

:root[data-theme="light"] {
  color-scheme: light;

  --bg:            #F4F6F5;
  --surface:       #FFFFFF;
  --surface-raised:#FFFFFF;
  --surface-2:     #F0F3F2;
  --inset:         #E9EDEC;

  --line:          #DDE3E1;
  --line-soft:     #EBEFEE;
  --line-strong:   #C9D2CF;

  --fg:            #10171C;
  --fg-2:          #4B5A63;
  --fg-3:          #64727B;
  --fg-4:          #8A959B;

  --accent:        #0A8158;
  --accent-hover:  #086A48;
  --accent-ink:    #FFFFFF;

  --pos:           #0A8158;
  --warn:          #96590A;
  --neg:           #C0392B;
  --info:          #1B63C4;
  --idle:          #8A959B;

  --pos-bg:   #E7F5EF;  --pos-line:  #BFE3D4;  --pos-ink:  #07603F;
  --warn-bg:  #FDF6E7;  --warn-line: #EBD9AE;  --warn-ink: #8A5606;
  --neg-bg:   #FDECEC;  --neg-line:  #F0C4C0;  --neg-ink:  #A32F23;
  --info-bg:  #EAF2FC;  --info-line: #C7DCF5;  --info-ink: #15529F;

  --warn-banner-bg:   #FDF6E7;
  --warn-banner-line: #EBD9AE;
  --warn-banner-ink:  #5C4415;
  --warn-banner-body: #7A5C22;

  --cat-1:      #2a78d6;
  --cat-2:      #eb6834;
  --cat-3:      #1baf7a;
  --cat-muted:  #6E7A87;

  --chart-ok:   #158A60;
  --chart-slow: #B8730C;
  --chart-bad:  #C0392B;

  --chart-zero:  #B6C0BD;
  --chart-track: #E9EDEC;
}
```

### 2.2 Contrast — measured, not estimated

Every ink/surface pair above clears WCAG AA 4.5:1 on its own surface. The tightest:

| Pair | Ratio |
|---|---|
| `--fg-3` on `--surface` (dark) | 5.07 |
| `--fg-3` on `--inset` (dark) | 5.26 |
| `--fg-3` on `--surface` (light) | 4.96 |
| `--accent` on `--surface` (light) | 4.89 |
| `--accent-ink` on `--accent` (light) | 4.89 |
| `--warn` on `--surface` (light) | 5.63 |
| `--neg-ink` on `--neg-bg` (dark) | 6.88 |

**If you change a colour, re-measure.** Three pairs in the *current* stylesheet are
already just under and should not be carried forward: `.wv2-step.pending` (4.04:1),
warn text on `#fffbea` (3.55:1), `#1e8449` on `#e6f4ea` (4.15:1).

### 2.3 Rules that carry the density

- `font-variant-numeric: tabular-nums` on `body`. Every numeric column then aligns.
- Numbers right-aligned, labels left, units in `--fg-3`.
- Spacing on a 4px grid. Card padding 15–18px. Section gap 16–20px.
- Body 13px / captions 11.5px / column labels 11px uppercase `0.06em` / page title 25px.
- Big numbers are mono, 19px (strip), 26px (KPI tile), 30px (dashboard KPI), `-0.025em`.
- Status is **never colour alone** — always a word or an icon beside it.

### 2.4 Fonts — no webfont

**Decision: platform stacks, no webfont, no font files in the repo.** The mockups were
originally drawn with IBM Plex loaded from Google Fonts; they have all been switched to
the stacks below, so what you see is what ships.

```css
--font-ui:   system-ui, -apple-system, Segoe UI, sans-serif;
--font-mono: ui-monospace, Consolas, monospace;
```

Note these are written **without quotes**, on purpose. Several values in the mockups sit
inside `style="…"` attributes, and a quoted family name (`"Segoe UI"`) closes the
attribute early and silently breaks the element. Unquoted multi-word family names are
valid CSS; keep them unquoted anywhere a stack could end up in an attribute.

Why not vendor IBM Plex:

- A localhost tool must not make a network request for a font. That rules out the CDN.
- Vendoring it means ~6 woff2 files (roughly 400–500 KB) in the repo, plus a
  `[tool.setuptools.package-data]` entry that has to actually land in the wheel — and
  packaging is already a known weak point here (`claude/review_fresh_2026-09-15.md` §2.3
  found four directories that are probably silently omitted). Install weight is the
  project's biggest flagged adoption tax; this is not the place to add to it.
- The design does not lean on Plex. Its character comes from the dark palette, the
  density and the mono numerals. **Monospace gives tabular figures for free**, which is
  the one typographic property the tables and KPI tiles actually depend on.

If you later want exact Plex fidelity, it is a contained change: drop the woff2 files in
`webui/static/fonts/`, add `@font-face` blocks at the top of `app.css`, and put
`"IBM Plex Sans"` / `"IBM Plex Mono"` at the front of the two stacks. Nothing else moves.
Do it as its own commit so the weight is easy to revert.

---

## 3. Component layer

All of this lives in `webui/static/app.css` below the tokens. Every per-template
`<style>` block is deleted as its content is covered here.

### 3.1 Duplicates to collapse first

| Today | Becomes |
|---|---|
| `.setup-card` (`product.html`) **and** `.sched-card` (`runs_list.html`) — byte-identical | `.panel` |
| `.p-tabs`/`.p-tab` (`product.html`) **and** `.admin-subnav`/`-tab` (`_admin_subnav.html`) | `.tabs`/`.tab` |
| ad-hoc coloured `<div>`s in `index.html`, `runs_list.html`, `step_describe.html`, `admin_features.html`, `product.html` | `.banner.banner--{ok,info,warn,neg}` |
| `.tag` (`base.html`) and the inline `<span class="tag" style="background:…">` overrides | `.tag` + `.tag--{pos,warn,neg}` |

### 3.2 Buttons

Four tiers. Today every `.btn` is accent-filled, which is why "Past runs →" shouts as
loudly as "Trigger a run →". One primary action per view.

```css
.btn {
  display: inline-flex; align-items: center; gap: 7px;
  height: 36px; padding: 0 15px; border-radius: var(--r-ctl);
  font: 500 13px/1 var(--font-ui); cursor: pointer;
  border: 1px solid transparent; text-decoration: none; white-space: nowrap;
}
.btn--primary   { background: var(--accent); color: var(--accent-ink); font-weight: 600; }
.btn--primary:hover { background: var(--accent-hover); }
.btn--secondary { background: var(--surface-2); border-color: var(--line-strong); color: var(--fg); }
.btn--ghost     { background: transparent; color: var(--fg-2); }
.btn--danger    { background: var(--neg-bg); border-color: var(--neg-line); color: var(--neg); font-weight: 600; }
.btn[disabled]  { background: var(--surface-2); border-color: var(--line); color: var(--fg-4); cursor: default; }
.btn--sm        { height: 30px; padding: 0 12px; font-size: 12.5px; }
```

**Busy state.** `step_describe.html` already swaps the label for a spinner on submit.
Promote that to `app.css` + one shared snippet and apply it to every POST that can take
more than a second: trigger run, save tuning, redraft profile, calibrate.

```css
.btn--busy { opacity: .85; cursor: wait; }
.btn--busy .spinner {
  width: 12px; height: 12px; border: 2px solid currentColor;
  border-right-color: transparent; border-radius: 50%;
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .btn--busy .spinner { animation: none; } }
```

### 3.3 Status pills and tags

```css
.pill {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 9px; border-radius: var(--r-pill);
  font: 600 11.5px/1.4 var(--font-ui); border: 1px solid;
}
.pill::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.pill--ok   { background: var(--pos-bg);  border-color: var(--pos-line);  color: var(--pos-ink); }
.pill--warn { background: var(--warn-bg); border-color: var(--warn-line); color: var(--warn-ink); }
.pill--bad  { background: var(--neg-bg);  border-color: var(--neg-line);  color: var(--neg-ink); }
.pill--idle { background: var(--surface-2); border-color: var(--line-strong); color: var(--fg-2); }
.pill--idle::before { display: none; }

.tag {
  display: inline-block; padding: 3px 8px; border-radius: var(--r-tag);
  background: var(--surface-2); color: var(--fg-2);
  font: 400 11px/1.4 var(--font-mono);
}
.tag--warn { background: var(--warn-bg); border: 1px solid var(--warn-line); color: var(--warn-ink); }
.tag--neg  { background: var(--neg-bg);  border: 1px solid var(--neg-line);  color: var(--neg-ink); }
```

Pill text is always a word: `Healthy` / `Degraded` / `Failing` / `Paused` / `Offline`.

### 3.4 Panels and cards

```css
.panel {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--r-card); padding: 15px 18px 17px;
}
.panel__head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 3px; }
.panel__head h2 { margin: 0; font: 600 14px/1.3 var(--font-ui); }
.panel__note { margin: 0 0 14px; font-size: 11.5px; color: var(--fg-3); }
.panel__foot {
  margin: 11px 0 0; padding-top: 11px; border-top: 1px solid var(--line-soft);
  font-size: 12px; line-height: 1.5; color: var(--fg-2);
}
```

`.panel__foot` is the one-sentence reading of the panel above it — "youtube is 4th by
volume but 1st by tokens". Every chart panel in the mockups has one. It is not decoration:
it is what makes the chart answer a question instead of posing one.

### 3.5 Banners

```css
.banner {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 11px 15px; border: 1px solid; border-left-width: 3px;
  border-radius: var(--r-ctl); font-size: 12.5px; line-height: 1.45;
}
.banner__icon { flex-shrink: 0; margin-top: 1px; width: 16px; height: 16px; }
.banner--ok   { background: var(--pos-bg);        border-color: var(--pos-line);         border-left-color: var(--pos); }
.banner--info { background: var(--info-bg);       border-color: var(--info-line);        border-left-color: var(--info); }
.banner--warn { background: var(--warn-banner-bg);border-color: var(--warn-banner-line); border-left-color: var(--warn); }
.banner--neg  { background: var(--neg-bg);        border-color: var(--neg-line);         border-left-color: var(--neg); }
```

Jinja macro, so the icon and tone never drift:

```jinja
{% macro banner(tone, lead, body='', action_href='', action_label='') %}
  <div class="banner banner--{{ tone }}">
    {{ icon(_BANNER_ICON[tone], class='banner__icon') }}
    <span style="flex-grow:1"><strong>{{ lead }}</strong>{% if body %} {{ body }}{% endif %}</span>
    {% if action_href %}<a href="{{ action_href }}">{{ action_label }} →</a>{% endif %}
  </div>
{% endmacro %}
```

### 3.6 Stat strip and KPI tiles

```css
/* horizontal strip — install-wide facts, Products page */
.statstrip { display: flex; background: var(--surface); border: 1px solid var(--line);
             border-radius: 10px; overflow: hidden; }
.statstrip > div { flex-grow: 1; padding: 12px 18px; border-right: 1px solid var(--line); }
.statstrip > div:last-child { border-right: none; }

/* tile grid — per-product KPIs, dashboard */
.kpis { display: grid; gap: 14px; grid-template-columns: repeat(var(--kpi-cols, 4), minmax(0, 1fr)); }
.kpi  { background: var(--surface); border: 1px solid var(--line);
        border-radius: 11px; padding: 14px 16px; }

.stat__label { font: 500 11px/1.3 var(--font-ui); letter-spacing: .06em;
               text-transform: uppercase; color: var(--fg-3); }
.stat__value { margin-top: 7px; font: 600 30px/1 var(--font-mono); letter-spacing: -.025em; }
.stat__value--sm { font-size: 19px; }
.stat__delta { font: 600 12.5px/1 var(--font-ui); }
.stat__sub   { margin-top: 6px; font-size: 11.5px; color: var(--fg-3); }
```

Every tile carries a comparison in `.stat__sub` or `.stat__delta`. A bare count is not
allowed: "1,284" means nothing, "1,284 · ▲12% vs 1,146 last week" does.

### 3.7 Tables

```css
.dt { width: 100%; border-collapse: collapse; }
.dt th {
  text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line-strong);
  font: 600 10.5px/1.3 var(--font-ui); letter-spacing: .07em;
  text-transform: uppercase; color: var(--fg-3);
}
.dt td { padding: 10px; border-bottom: 1px solid var(--line-soft); font-size: 12.5px; }
.dt tr:last-child td { border-bottom: none; }
.dt .num { text-align: right; font-family: var(--font-mono); }
.dt th:first-child, .dt td:first-child { padding-left: 0; }
.dt th:last-child,  .dt td:last-child  { padding-right: 0; }
```

No zebra striping. The row rule does the work.

### 3.8 Form fields

```css
.field { display: block; margin-bottom: 14px; }
.field__label { display: block; margin-bottom: 5px; font: 500 12.5px/1.3 var(--font-ui); }
.field__input {
  width: 100%; box-sizing: border-box; height: 36px; padding: 0 11px;
  background: var(--inset); border: 1px solid var(--line-strong);
  border-radius: 7px; font: 400 13px/1 var(--font-ui); color: var(--fg);
}
.field__input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.field__help  { margin: 6px 0 0; font-size: 11.5px; color: var(--fg-3); }
.field__error { margin: 6px 0 0; font-size: 11.5px; color: var(--neg); }
.field--invalid .field__input { border-color: var(--neg); }
```

Delete `form.stacked { max-width: 480px }`. Width belongs to the field, not the form —
that cap is why the admin forms feel cramped. Use `.field--sm { max-width: 200px }` /
`.field--md { max-width: 480px }` / full width per field.

### 3.9 Toggle switch

Admin's feature flags are checkboxes today. Keep the real `<input type="checkbox">` for
form submission and accessibility; style it as a switch.

```css
.switch { position: relative; width: 40px; height: 22px; flex-shrink: 0; }
.switch input { position: absolute; inset: 0; opacity: 0; margin: 0; cursor: pointer; }
.switch__track {
  display: block; width: 100%; height: 100%; border-radius: var(--r-pill);
  background: var(--surface-2); border: 1px solid var(--line-strong); transition: background .12s;
}
.switch__track::after {
  content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px;
  border-radius: 50%; background: var(--fg-4); transition: transform .12s, background .12s;
}
.switch input:checked + .switch__track { background: var(--accent); border-color: var(--accent); }
.switch input:checked + .switch__track::after { transform: translateX(18px); background: var(--accent-ink); }
.switch input:focus-visible + .switch__track { outline: 2px solid var(--accent); outline-offset: 2px; }
```

### 3.10 Empty states

```css
.empty { border: 1px dashed var(--line-strong); border-radius: 10px; padding: 18px 20px; }
.empty__step { display: flex; align-items: center; gap: 10px; padding: 7px 0; }
.empty__mark { width: 18px; height: 18px; border-radius: 50%; flex-shrink: 0;
               display: inline-flex; align-items: center; justify-content: center; }
.empty__mark--done { background: var(--pos-bg); color: var(--accent); }
.empty__mark--todo { border: 1px solid var(--line-strong); }
```

A first-run dashboard shows the checklist, not "No data yet." See `components.html`.

### 3.11 Icons

No icon font, no CDN. One inline SVG sprite in `base.html` before `</body>`:

```jinja
{% macro icon(name, class='') %}<svg class="icon {{ class }}" aria-hidden="true"><use href="#i-{{ name }}"></use></svg>{% endmacro %}
```

```css
.icon { width: 15px; height: 15px; fill: none; stroke: currentColor;
        stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
```

Names used by the mockups: `plus`, `play`, `doc`, `chevron-left`, `chevron-right`,
`check`, `x`, `alert-triangle`, `info`, `search`, `sun`, `moon`, `sliders`, `activity`,
`list`, `toggle`, `key`, `spark`. Lift the paths straight out of the mockups — they are
already 24×24 stroke icons.

Every icon-only control needs `aria-label`. Every decorative icon gets `aria-hidden`.

---

## 4. Theme toggle

```html
<html lang="en" data-theme="{{ theme }}">
```

- Server renders `data-theme` from a `pm_theme` cookie; absent → omit the attribute and
  let `prefers-color-scheme` decide via a `@media` block that mirrors `[data-theme="light"]`.
- The toggle button in the header posts to `/settings/theme` (form POST + redirect back,
  in keeping with the rest of the app) **and** flips `document.documentElement.dataset.theme`
  optimistically so there is no flash.
- Do not store theme in `localStorage` alone — it would flash the wrong theme on every
  server-rendered page load.

Light is a first-class theme, not an afterthought: see `products_light.html` and
`product_dashboard_light.html`. Both were produced by swapping only the token block, which
is the test that the token layer is complete — **if a page needs any other change to look
right in light mode, a hard-coded colour has leaked into a template.** Use that as the
check when you port each page.

---

## 5. Page specs

### 5.1 Products — `index.html`

Order, top to bottom (see `products.html`):

1. **Page head** — title, one-line description, `Run all now` (secondary), `New product` (primary).
2. **Stat strip** — Products (+ N draft) · Scheduled n/m · Last run · Streams healthy n/m ·
   Tokens 30d · Spend 30d. `Streams healthy` and `Spend` take `--warn` ink when off-nominal.
3. **Needs attention** — §5.5.
4. **Live products** — one `.card` each: name, description, status pill, three figures
   (items/wk, sentiment, 30d cost), source/area tags with a `.tag--neg` for failures,
   and a footer line `ran 2h ago · next Mon 06:00` plus a `Report` link.
   The "In progress" drafts block collapses to a single link in the section head
   (`1 unfinished draft — Linear, step 3 of 5 →`); the full draft cards move to `/wizard`.
5. **Token usage and cost by product** — §7.1.
6. **Source health across all products** — §7.2.

First-run (`first_run` truthy) keeps today's welcome branch, restyled with `.empty`.

### 5.2 Product dashboard — Summary tab in `product.html`

Order is fixed (see `product_dashboard.html`):

1. **Run in progress** — `.banner--warn` variant with the 9-stage strip. Rendered only
   when a run is live; otherwise omitted entirely (the tabs sit directly above the KPIs).
2. **KPI row**, `--kpi-cols: 4` — Fetched, Kept, Sentiment, Source health.
3. **Weighted sentiment over time** — §7.3.
4. **Volume by source** and **Token usage by source**, side by side — §7.4.
5. **Recent runs** — §5.4.
6. **Needs attention**, scoped to this product — §5.5.

Tab changes: `Summary · Configuration · Sources · Runs · Items · Advanced`. The current
`Run Setup` tab is one schedule form and two paragraphs — fold it into Configuration and
give the freed slot to Sources.

Also on this page: the danger zone asks for a typed slug **and** a `confirm()` dialog.
Keep the typed slug, drop the dialog.

### 5.3 Wizard — `wizard/step_sources.html` + `_stepper.html`

`_stepper.html` becomes a vertical rail (`wizard_sources.html`, left column): per-step
label plus a completion detail (`7 aliases · 4 competitors`, `6 of 9 selected`), a
"Step 3 of 5 · about 6 min left" header, and a "Why this matters" note pinned to the
bottom. Steps are `done` / `current` / `pending`; `aria-current="step"` on the current one.

Source cards: real `<label>` wrapping a real `<input type="checkbox">`, a signal badge,
a one-line stat, and a live sample quote in an `.inset` well. Unavailable sources render
`disabled` at `opacity: .55` with a `NEEDS KEY` tag.

Sticky action bar at the bottom of the main column: Back (ghost) · estimate
(`~430 items/week · $0.38 per run`) · Skip (secondary) · Continue (primary).

### 5.4 Recent runs table

Columns, in order: **Report · Started · Duration · Items · Tokens · Cost · Status.**

- Report cell is a link to the digest; a failed run shows `No report — failed at classify`
  in `--fg-3` instead.
- Started is `Sep 15, 06:00` in mono, `--fg-2`.
- Duration takes `--warn` ink when above 1.5× the median.
- Status is a `.pill`.
- Five rows on the dashboard, `All runs →` in the panel head.

### 5.5 Needs attention

Same component on both pages; scope differs.

Each row: a fixed-width mono severity tag, the message, a timestamp, an action link.

```
[SOURCE ]  reddit — 2 of 3 streams returning HTTP 403.  Affects Notion and Cursor;
           roughly 118 items missing from this week's digests.   2026-09-16 06:04  Inspect
```

Tag vocabulary and tone: `SOURCE`, `RUNNER`, `COST`, `LATENCY`, `QUALITY`. Critical rows
use `--neg-bg`/`--neg`, everything else `--warn-bg`/`--warn`.

**The rule that makes this section worth building: state the consequence, not the error.**
"HTTP 403" is not an alert. "HTTP 403, so ~118 items are missing from this week's digest"
is. Every message is `<strong>what happened.</strong> what it costs you.`

Conditions to emit (all computable from existing state plus §6):

| Tag | Condition |
|---|---|
| `SOURCE` | a stream's last ≥2 fetches failed, or returned 0 items where its trailing mean is > 0 |
| `RUNNER` | `.running` marker older than 2× the shortest configured cadence |
| `COST` | product 30-day spend up > 50% against the prior 30 days |
| `LATENCY` | a source's p95 fetch above 5s over the last 7 runs |
| `QUALITY` | an area's item count below 60% of its 4-week mean |

Empty state: `Nothing needs attention.` in `--fg-3`, panel still rendered. Do not hide it —
its absence is information.

### 5.6 Admin — `admin_features.html` + `_admin_subnav.html`

`_admin_subnav.html` becomes a left sidebar grouped `Connections` / `Install`, with a
count or status dot per item. Feature toggles become `.switch` rows with the help text
always visible rather than behind a `?` popover — there are eight flags, not eighty.
Right rail: **Install health** (assistant LLM, warehouse size, run marker age, disk free)
and **Spend, last 30 days** with the per-stage split.

Keep the unsaved-changes count and the `config/features.yaml` provenance line.

---

## 6. Data contract

Names below are the existing ones from the templates unless marked **new**.

### 6.1 `index.html` context

Existing: `products[]`, `drafts_v2[]`, `first_run`, `has_demo`.

Each `products[i]` gains — **new**:
```
status          "ok" | "degraded" | "failing" | "offline"
items_last_week int
items_delta_pct float | None
sentiment       float | None      # weighted, most recent complete week
n_sources_ok    int
n_sources_total int
last_run_at     datetime | None
last_run_status str | None
next_run_at     datetime | None
tokens_30d      int
cost_30d        float
```

New top-level context:
```
install_stats   { n_products, n_drafts, n_scheduled, last_run_at,
                  streams_ok, streams_total, tokens_30d, cost_30d }
attention[]     { severity: "critical"|"warning", tag, lead, body, at, href, action }
token_by_product[]  { product_id, display, relevance, classify, digest, total, cost }
token_other     { label: "Wizard setup", total, cost }
source_health   { by_type: [{ type, ok, slow, failing, paused }],
                  fetch_success_14d: [{ date, pct }] }
```

### 6.2 `product.html` Summary context

`webui/services/runs.py::product_dashboard_summary` keeps `latest_run`,
`latest_report_url`, `totals`, `run_status_counts`, `by_area`, `trend`,
`warehouse_available`, and gains — **new**:

```
live_run        None | { stage, stage_index, stage_total, stages[],
                         items_done, items_total, elapsed_s, eta_s,
                         tokens_so_far, cost_so_far }
kpis            { fetched, fetched_delta_pct, fetched_prev,
                  kept, kept_pct, holdout_precision,
                  sentiment, sentiment_delta, sentiment_rank_note,
                  sources_ok, sources_total, sources_failing, sources_slow }
by_source[]     { source_type, label, items, tokens, cost, status }
recent_runs[]   { week_id, report_url|None, started_at, duration_s,
                  items, tokens, cost, status, failed_stage|None }
attention[]     same shape as §6.1, scoped to this product
```

`trend` already carries `week_id`, `item_count`, `avg_sentiment` — enough for §7.3.
Mark the current week `partial: true` so the chart can grey its bar.

### 6.3 Source health

`webui/source_health.py` already exists and is **not surfaced anywhere in the UI today**.
Expose two functions:

```python
def product_source_health(product_id) -> list[StreamHealth]
def install_source_health() -> InstallHealth   # by_type + fetch_success_14d
```

`StreamHealth`: `source_type, label, status, last_fetch_at, last_http_status,
items_last_run, p95_ms, paused`.

### 6.4 Token accounting — the one genuinely new piece of data

Nothing today records tokens per run, per stage or per source, so §7.1 and §7.4 have
nothing to read. This is the only schema change in scope.

New warehouse table:

```sql
CREATE TABLE IF NOT EXISTS llm_usage (
  run_id        VARCHAR NOT NULL,
  product_id    VARCHAR NOT NULL,
  stage         VARCHAR NOT NULL,   -- relevance | classify | digest | wizard
  source_type   VARCHAR,            -- NULL for stage-level calls (digest, wizard)
  model         VARCHAR NOT NULL,
  prompt_tokens BIGINT NOT NULL,
  output_tokens BIGINT NOT NULL,
  cost_usd      DOUBLE  NOT NULL,
  at            TIMESTAMP NOT NULL
);
```

- Write from **one place**: `pipeline/llm.py::LLMClient._call`, which already sees the
  usage payload from both the OpenAI-compatible and Anthropic paths. Pass `stage`,
  `product_id`, `source_type` down through the call context rather than threading a new
  argument through every stage.
- Batch the inserts per stage. `storage.warehouse()` opens and closes a connection per
  call — do not add a per-LLM-call insert to that (see `claude/architecture_review_2026-09-15.md`
  P1-2).
- `cost_usd` needs a price table. Put it in `config/app.yaml` keyed by model id, with
  `0.0` for local/recorded models so the demo product shows `$0.00` honestly.
- Backfill is not required. The charts render an empty state until there is data.

---

## 7. Chart specs

All charts are **server-rendered inline SVG or plain divs**. No chart library, no canvas.
matplotlib stays for the digest only. Every chart gets a `role="img"` and an `aria-label`
that states the finding in words, and a `.panel__foot` sentence.

### 7.1 Token usage and cost by product (Products page)

Horizontal stacked bars, one row per product, plus a header row and a neutral
"Wizard setup" row.

- Columns: `Product | Tokens (bar) | Total | Cost | Rate $/M`.
- Bar track spans the column; segment widths are `stage_tokens / max_product_total`, so
  rows are comparable to each other, not each normalised to 100%.
- Segments in fixed order relevance → classify → digest, `--cat-1/2/3`, **2px gap**
  between segments, 4px radius on the outer ends only.
- Legend in the panel head, four swatches, including the muted "setup".
- **Cost is a column, never the bar length.** The whole point is that they diverge: in the
  mockup Cursor uses 28% fewer tokens than Notion and costs 47% more.
- `Rate $/M` takes `--warn` ink above $3/M and `--neg` above $6/M.

Colour note: `--cat-1/2/3` were validated as a set against `--surface` — worst all-pairs
CVD ΔE 9.4, normal-vision ΔE 20.9, all ≥3:1. `--cat-muted` is a deliberate grey that sits
*outside* that set; it is never adjacent to a stage segment (it is its own row) and its
identity is carried by the row label. In light mode `--cat-3` sits at 2.82:1, which is
below the 3:1 fill floor — the relief is the visible `Total`/`Cost`/`Rate` columns on
every row. Keep them.

### 7.2 Source health across all products (Products page)

Two sub-panels side by side.

**Streams by status** — one stacked bar per source type, widths `count / total_streams`,
`--chart-ok` / `--chart-slow` / `--chart-bad`, 2px gaps. Right-hand label spells the
counts out (`1 ok · 2 failing`) — colour never carries it alone. The source-type label
takes status ink when the type is not fully healthy.

**Fetch success, last 14 days** — daily bar chart.
- y scale **80–100%**, not 0–100. The interesting range is the top fifth.
- Dashed `--warn` line at the 95% target, labelled.
- Bar fill by band: ≥95% `--chart-ok`, 88–95% `--chart-slow`, <88% `--chart-bad`;
  today's bar in `--neg` to mark it.
- Axis labels at first / middle / last day only.
- Caption under the plot states the finding.

Height is the primary encoding here, colour is secondary — which is what lets the status
fills sit in the CVD warn band without being ambiguous.

### 7.3 Weighted sentiment over time (dashboard)

Single series, so **no legend** — the title names it.

- viewBox `0 0 1188 200`, `width="100%" height="200"`, no `preserveAspectRatio="none"`
  (it would distort the text).
- Left gutter 56px for axis labels; plot x from 56 to 1176; 12 points evenly spaced.
- y domain −0.10 → +0.25 mapped to y 150 → 20. Gridlines at +0.20, +0.10, 0.00, −0.10;
  the zero line is dashed in `--chart-zero`, the others solid `--chart-grid`.
- A `--fg-2` rectangle at 6% opacity covers ±0.05 as the "neutral" band.
- Line: 2px `--accent`, round joins. Area below: same colour at 10% opacity.
- Markers ≥8px: the last point in `--accent` and every negative week in `--neg`, each with
  a 2px `--surface` ring so it reads against the line.
- Direct-label the last value and the lowest dip only. Never a label on every point.
- x labels every other week.

If `trend` has fewer than 3 weeks, render the empty state instead of a 2-point line.

### 7.4 Volume by source / Token usage by source (dashboard)

Two separate single-series horizontal bar charts, side by side. **Not one dual-axis
chart** — two measures on two scales is the single most common charting mistake and it
would hide exactly the finding these two exist to show.

- Volume bars use `--cat-1`; token bars use `--cat-2` (the second single-hue context).
- Each sorted descending by its own measure. The orders differ, and that *is* the
  insight: youtube is 4th by volume and 1st by tokens.
- A source with 0 items renders as a dashed `--neg-line` outline at full width, value in
  `--neg` — an empty track, not a missing row.
- `.panel__foot` on each: the volume one names what dropped, the token one names the
  tokens-per-item outlier.

### 7.5 Run-in-progress strip (dashboard)

Nine equal segments, 7px tall, 3px gaps, `--r-pill`. Complete stages `--warn`, the
current stage at a muted `--warn` (`#8A6423` dark), pending `--chart-track`. Stage names
in 10px mono below, current one bold in `--warn`.

Poll `/api/refresh` (the route exists) every 3s while a run is live; swap the strip's
HTML fragment. Stop polling on completion and reload once so the KPIs update.

---

## 8. Responsive

Mockups: `products_mobile.html`, `product_dashboard_mobile.html`,
`wizard_sources_mobile.html` — all 390px, the narrowest phone worth designing for.

The app is desktop-first. Everything above describes the ≥1200px layout; this section is
the set of overrides below it. There are three breakpoints and no others.

```css
/* --bp-md: 900px   tablet / small laptop / half-screen window */
/* --bp-sm: 600px   phone                                      */
```

### 8.1 Global

| | ≥1200px | ≤900px | ≤600px |
|---|---|---|---|
| `main` padding | 26px 28px | 20px 16px | 16px 12px |
| `.panel` padding | 15px 18px 17px | 14px 16px | 13px 14px |
| section gap | 20px | 16px | 14px |
| page title | 25px | 23px | 21px |
| `.btn` height | 36px | 36px | **44px** |
| `.btn--sm` height | 30px | 30px | **36px** |

Body copy, captions and column labels do **not** shrink. 13px stays 13px — a dense data
tool that shrinks its text on a phone is unreadable, and the layout has room if things
stack.

Touch targets are the real constraint: every control reaches 44px below 600px, checkbox
inputs go from 16px to 20px, and the icon-only header buttons become 36×36 with the tap
area padded to 44.

### 8.2 Component overrides

**Header.** Below 600px the nav links and the `local · 127.0.0.1` status pill are removed
from the bar and the bar becomes: logo · spacer · theme toggle · menu button. The menu is
a `<details>` disclosure — no JS, no framework:

```html
<details class="navmenu">
  <summary aria-label="Menu">{{ icon('list') }}</summary>
  <nav>…the same links…</nav>
</details>
```

`summary::-webkit-details-marker { display: none }` and absolutely position the `nav`
below it. On the dashboard the logo is replaced by a back chevron to the products list —
a phone has no breadcrumb room and no browser chrome worth relying on.

**Stat strip.** `.statstrip` is `display: flex` at desktop. Below 900px it becomes
`display: grid; grid-template-columns: repeat(3, 1fr)`, below 600px `repeat(2, 1fr)`.
Cell borders switch from `border-right` only to `border-right` + `border-bottom`, with
`:nth-child` rules clearing the right edge and the last row. Values drop 19px → 17px.

**KPI grid.** `--kpi-cols: 4` → `2` at 900px and stays 2 at 600px. Not 1: two columns of
23px numbers read better than one column of four tall cards, and it keeps Fetched/Kept
and Sentiment/Sources paired the way you actually compare them.

**Product cards.** `repeat(auto-fill, minmax(260px, 1fr))` already reflows 3 → 2 → 1. At
600px the three figures inside each card become a `repeat(3, 1fr)` grid so they stay on
one line, and the description is clamped to two lines.

**Two-column chart rows** (volume + tokens by source) stack to one column at 900px. On the
Products page the source-health sub-panels do the same.

**Tabs.** `.tabs` gets `overflow-x: auto; scrollbar-width: none` and the items
`white-space: nowrap` below 900px. Add a 30px gradient fade on the right edge so it is
visible that there is more. Do not wrap tabs to a second line.

**Needs attention rows.** Desktop puts tag · message · timestamp · action on one line.
Below 600px the message comes first on its own line, and tag · timestamp · action sit
underneath it. The message is the part that matters; the metadata is not worth the
horizontal budget.

**Recent runs table.** Seven columns cannot survive 390px. Below 600px `.dt--stack`
turns each row into a block: the report link and status pill on line one, then the
remaining values as a wrapped run of mono text (`Sep 15, 06:00 · 4m 12s · 312 items ·
214k · $0.41`). Keep the `<table>` element and drive it with CSS
(`display: block` on `tr`/`td`, `::before` labels from `data-label` where a value is
ambiguous alone) so the desktop table and the phone list are one template.

**Token usage rows.** The desktop row is `name | bar | total | cost | rate`. Below 600px:
name and total on line one, the bar full-width on line two, cost and rate as a mono run
on line three. Same data, no horizontal scroll.

**Horizontal bar charts** (volume by source, tokens by source, streams by status) are
built from divs, so they reflow on their own. Shrink the label gutter from 96px to 78px
and the value column from 42px to 34px below 600px.

**Run-in-progress strip.** The nine segments stay — at 390px each is still ~40px wide.
The nine stage names below do not; hide them below 600px and keep only the line already
above the bar (`stage 5/9 · classify · 268/402 items`).

**Wizard.** The 268px vertical rail is dropped below 900px and replaced by a horizontal
progress header: step title, "~6 min left", a five-segment bar, and the current step's
completion detail. The "Why this matters" note moves to the end of the content. The
sticky action bar keeps `position: sticky; bottom: 0` but stacks — estimate line above,
Back (icon only, 52px) and Continue (fills the rest) below.

**Admin.** The sidebar becomes a horizontal scrolling tab strip below 900px, same
treatment as `.tabs`. The right rail (install health, spend) moves below the toggle list.

### 8.3 SVG charts

The two real SVG charts — weighted sentiment and fetch success — are the only things that
cannot simply reflow. A 1188-unit viewBox scaled into 366px renders 10px labels at about
3px.

**Do not scale them down. Scroll them.**

```css
.chart-scroll { overflow-x: auto; scrollbar-width: none; position: relative; }
.chart-scroll svg { display: block; }
@media (max-width: 600px) {
  .chart-scroll svg { width: 500px; }   /* sentiment */
  .chart-scroll svg.chart--health { width: 480px; }
}
.chart-scroll::after {          /* edge fade so the overflow is discoverable */
  content: ""; position: absolute; inset: 0 0 0 auto; width: 30px;
  background: linear-gradient(90deg, transparent, var(--surface));
  pointer-events: none;
}
```

Server-side, render the sentiment chart with a narrower viewBox (594 instead of 1188)
and fewer x labels — the phone mockup shows W27 / W31 / W35 only. Everything else about
the chart is identical, so it is one macro with a `compact` flag, not two charts.

A caption under a scrollable chart should say so plainly ("Swipe the chart") — an
off-screen axis with no affordance is a chart people misread.

### 8.4 What is deliberately not responsive

The tokens sheet and the component kit are internal reference pages. They are
desktop-only and that is fine.

---

## 9. Build order

Each step ships on its own and leaves the app working.

1. **`webui/static/app.css`** with §2 only; mount it in `base.html`; vendor the fonts.
   Nothing else changes — the old per-page CSS still wins where it overlaps.
2. **Theme toggle** (§4) and the `data-theme` plumbing. Verify both themes on every
   existing page before going further.
3. **Component layer** (§3), one component at a time, deleting the per-template `<style>`
   block it replaces. Start with `.panel`, which kills the `.setup-card` / `.sched-card`
   duplication.
4. **Icon sprite** (§3.11). Replace the `→` and `⚠` glyphs.
5. **Jinja macros** for banner, pill, stat tile, empty state.
6. **Products page** (§5.1) without the two charts — stat strip, cards, Needs attention.
   `attention[]` at this stage can emit only the SOURCE and RUNNER rules; both are
   computable today.
7. **Dashboard** (§5.2) without the token chart — run strip, KPI row, sentiment chart,
   volume by source, recent runs, Needs attention.
8. **Token accounting** (§6.4). Schema, write path, price table, backfill-free.
9. **The two token charts** (§7.1, §7.4 right-hand panel) and the COST attention rule.
10. **Wizard** (§5.3) and **Admin** (§5.6).
11. **Responsive pass** (§8). Do this last, in one go, against the finished pages — the
    breakpoint rules all live in `app.css` and touch no template except the header menu
    and the `.dt--stack` markup. Resist adding them page by page as you go; you will end
    up with three inconsistent sets.

Steps 1–5 are pure refactor and should not change a single rendered string. Verify that
with a diff of the rendered HTML before and after if you can.

---

## 10. Acceptance

- [ ] No template contains a raw hex colour. `grep -rE "#[0-9a-fA-F]{3,6}" webui/templates/`
      returns only the icon sprite.
- [ ] No template contains a `<style>` block except `base.html`'s single `<link>`.
- [ ] `.setup-card`, `.sched-card`, `.admin-subnav`, `.p-tabs` no longer exist.
- [ ] Both themes render every page; no flash of the wrong theme on load.
- [ ] Every numeric column is mono and right-aligned; digits line up between rows.
- [ ] Every status is readable with colour removed (grayscale screenshot test).
- [ ] Every icon-only button has an `aria-label`; every chart has a descriptive
      `aria-label` stating its finding.
- [ ] Keyboard: tab order follows visual order; focus is visible on every control;
      no `role`/`onclick` on a `div`.
- [ ] The app loads with **no outbound network request at all** — devtools, offline mode,
      every page. No font, no CDN, nothing.
- [ ] No quoted font family appears inside a `style="…"` attribute.
      `grep -rn 'style="[^"]*"[A-Za-z]' webui/templates/` returns nothing.
- [ ] `product-monitor demo` still renders every page with fixture data and shows
      `$0.00` spend rather than an empty chart.
- [ ] Contrast: re-measure any colour you changed. AA 4.5:1 for text, 3:1 for chart fills.
      Both themes.
- [ ] At 390px: no horizontal page scroll on any page; every control is ≥44px tall; the
      only things that scroll sideways are the tab strip and the two SVG charts, and both
      show an edge fade.
- [ ] At 900px (half-screen window): nothing overlaps, the KPI grid is 2-up, the wizard
      rail has become the horizontal progress header.
- [ ] Rotate a phone to landscape and back — the sticky wizard bar must not cover the
      last source card.

---

## 11. Known gaps

- **The `attention[]` thresholds** in §5.5 are first guesses. Expect to tune them once
  real runs produce real alerts — put them in `config/app.yaml`, not in code.
- **Per-source token attribution** assumes every LLM call can be traced back to a source.
  Digest and wizard calls cannot; they carry `source_type = NULL` and are excluded from
  §7.4 rather than bucketed into "other".
- **Tablet portrait (~768px) has no mockup.** It falls in the ≤900px band and should be
  fine, but it is the one width nobody has looked at.
- **The Admin and wizard light-mode variants have no mockup.** The token mapping is
  mechanical and the two pages use only components proven on the light Products and
  dashboard boards, so this is low risk — but check them by eye once the toggle works.
