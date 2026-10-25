# Console Kit — the spec

Implementation reference for `theme.css`. `README.md` is the quick start; this is what to
read before changing anything or building a new component.

---

## 1. Principles

**The token layer is the whole system.** Application code never writes a raw colour, radius
or spacing value. That one rule is what makes the theme swap, the light mode and the rebrand
free. It is also the test: if a component needs anything beyond a token swap to look right
in the other theme, a hard-coded value has leaked in.

**Light is stepped, never inverted.** The light palette is chosen for a light ground, not
computed from the dark one. Inverting produces muddy greys and accents that fail contrast.

**Status is never colour alone.** Every pill carries a word, every chart a label, every
alert a severity tag. Assume a grayscale screenshot in a ticket, and assume a reader with a
colour-vision deficiency.

**State the consequence, not the error.** A writing rule with components attached.
"HTTP 403" is a log line. "HTTP 403, so 118 records are missing from this period" is a
banner.

**Every number carries a comparison.** `1,284` says nothing. `1,284, up 12% from 1,146`
says something. `.stat` has nowhere to put a number without one, on purpose.

**Nothing shrinks on a phone.** Body text stays 13px at every width. Layouts stack and
controls grow.

**Charts are elements where they can be.** Bars are divs — they reflow, theme and print for
free. SVG is for real axes only.

---

## 2. Tokens

Full values live in `theme.css`; this is the map of what each group is *for*.

### Surfaces — three levels, no more

| Token | Role |
|---|---|
| `--bg` | Page ground |
| `--surface` | Cards and panels — the default ground for content |
| `--surface-raised` | Top bar, sidebars, sticky footers |
| `--surface-2` | Tags, secondary buttons, bar tracks. A step up from surface, never a card |
| `--inset` | Wells inside a card: quotes, code, mini charts. A step down |

A fourth level reads as clutter rather than depth. Depth comes from a 1px border, never a
shadow.

### Lines and ink

`--line-soft` (row dividers) · `--line` (card borders) · `--line-strong` (input and
secondary-button borders).

`--fg` → `--fg-2` (descriptions) → `--fg-3` (captions, column labels, units) → `--fg-4`.
**`--fg-4` is for disabled controls only.** It does not clear AA and must never carry text
a user needs to read.

### Accent

One brand colour: `--accent`, `--accent-hover`, `--accent-ink` (text on an accent fill),
`--accent-bg` / `--accent-line` (accent-tinted surface). See § 6.

### Status — reserved

`--pos` `--warn` `--neg` `--info` `--idle`, each with a `-bg` / `-line` / `-ink` triple.
**Never reuse a status colour as a chart series or a decorative accent**, or the reader
stops trusting what the colour means.

`--warn-banner-*` is a softer warn fill, because a banner covers far more area than a pill
and the pill fill is too hot at that size.

### Shape, space, type

Radii 5 / 8 / 12 / pill (`--r-tag` `--r-ctl` `--r-card` `--r-pill`). Spacing `--sp-1`…`6`
on a 4px grid. `--font-ui` and `--font-mono` are platform stacks — see § 7.

---

## 3. Density

Two modes on the same tokens: `compact` (default) and `comfortable`
(`<html data-density="comfortable">`). Only control height, padding, page gutter and two
font sizes change — no component has a density-specific rule.

Use `comfortable` for lower-frequency or less technical audiences. If you are reaching for
it on every screen, this may be the wrong system for that product.

---

## 4. Components

Class contracts. Every one is styled on a real element and needs no JavaScript.

| Component | Classes | The rule that matters |
|---|---|---|
| Button | `.btn` + `--primary` / `--secondary` / `--ghost` / `--danger`, `--sm`, `--icon`, `--block`, `--busy` | One primary per view. A slow action swaps its label for `.spinner` + a present-tense verb. |
| Status pill | `.pill` + `--ok` / `--warn` / `--bad` / `--info` / `--idle` | Always contains a word. The dot is reinforcement, not the signal. |
| Tag | `.tag` + `--warn` / `--bad` | Counts and facts, not status. Different shape from a pill on purpose. |
| Panel | `.panel`, `.panel__head`, `.panel__note`, `.panel__foot` | The foot is the one-sentence reading of the content. A chart without one asks a question instead of answering it. |
| Stat | `.statstrip` / `.kpis` + `.kpi`, `.stat__label` / `__value` / `__delta` / `__sub` | Every value carries a comparison. Set `--kpi-cols` to change the grid. |
| Table | `.dt`, `.dt--stack`, `.num`, `.dt__lead`, `data-label` | No zebra striping. Below 600px the same markup becomes a block list. |
| Field | `.field` + `--sm` / `--md` / `--invalid`, `.field__label` / `__input` / `__help` / `__error` | Width belongs to the field, never the form. Errors replace help text rather than stacking. |
| Switch | `.switch`, `.switch__track` | A real checkbox. Never rebuild it from divs. |
| Banner | `.banner` + `--ok` / `--info` / `--warn` / `--bad` | Bold lead-in = what happened. Rest = what it costs the reader. |
| Alert list | `.alert`, `.alert__tag` (+`--bad`), `__msg`, `__meta` | Render the panel even when empty. Its absence is information too. |
| Tabs | `.tabs`, `.tab` + `aria-current` | Scrolls sideways below 900px; never wraps to a second row. |
| Empty state | `.empty`, `.empty__step`, `.empty__mark` + `--done` / `--todo` | A checklist that gets people to a first result, not "No data yet". |
| Bar | `.bar`, `.bar__seg` / `__track` / `__fill`, `.bar--empty` | 2px gap between segments; radius on the outer ends only. |
| Layout | `.topbar`, `.page`, `.row`, `.stack`, `.grid--2/3/4/auto` | Spacing comes from `gap`, never per-element margins. |

Utilities: `.muted` `.dim` `.num` `.mono` `.grow` `.nowrap` `.sr`.

---

## 5. Responsive

Desktop-first, three breakpoints, no others.

| | ≥900px | ≤900px | ≤600px |
|---|---|---|---|
| Page gutter | 28px | 16px | 12px |
| Control height | 36px | 36px | **44px** |
| `.kpis` | 4 cols | 2 | 2 |
| `.statstrip` | flex row | 3-col grid | 2-col grid |
| `.grid--3/4` | as named | 2 cols | 1 col |
| Tabs | inline | scroll | scroll |

**Body text does not shrink.** A dense tool that shrinks its type on a small screen becomes
unusable rather than compact. Things stack; controls grow.

**Wide tables** get `.dt--stack`: the header is hidden, each row becomes a block, and
`data-label` prints before each value. One template serves both widths, so the mobile view
cannot drift from the desktop one.

**SVG charts scroll, they do not scale.** A 1188-unit viewBox rendered at 366px turns 10px
labels into 3px. Wrap in `.chart-scroll` (min-width on the svg, edge fade included) and say
so in the caption — an off-screen axis with no affordance is a chart people misread.

---

## 6. Rebranding

1. **Pick an accent.** `<html data-accent="teal|blue|amber|violet">`, or edit the five
   `--accent-*` tokens directly. Both modes need a value; they are different colours, not
   the same colour dimmed.
2. **Leave everything else alone first.** Surfaces, status and chart colours are tuned as a
   set. Swapping the accent alone gets you most of the way in five minutes.
3. **Run `node tools/check-contrast.mjs`.** Accent on surface must clear 4.5:1 — it is link
   text — and ink on accent must clear 4.5:1 in *both* modes.
4. **Avoid collisions.** Do not choose an accent that reads as a status colour in your
   product. Amber as the accent in a tool where `--warn` is load-bearing means the reader
   can no longer tell "this is a link" from "this needs attention".
5. **Only then** touch the neutrals, re-running the checker after each change.

---

## 7. Type

Platform stacks: `system-ui, -apple-system, Segoe UI, sans-serif` and
`ui-monospace, Consolas, monospace`. No webfont — no network request, no font files in the
repo, no packaging step. Monospace gives **tabular figures for free**, which is the one
typographic property the tables and stat tiles actually depend on.

**Write font stacks unquoted.** A quoted family name (`"Segoe UI"`) inside a `style="…"`
attribute closes the attribute early and silently breaks the element. Unquoted multi-word
family names are valid CSS.

To add a branded face later: put it at the front of `--font-ui`, vendor the woff2 locally,
declare `@font-face` at the top of `theme.css`. Nothing else moves. Do it as its own commit
so the weight is easy to revert.

---

## 8. Charts

Categorical hues `--cat-1 … --cat-6` are assigned in **fixed order and never cycled**. Past
six series, fold the tail into `--cat-muted` as "Other", or split into small multiples.

The set is validated for colour-vision deficiency, not eyeballed. In dark mode all six clear
3:1 against the card surface. In light mode `--cat-3`, `--cat-4` and `--cat-5` sit below 3:1
— a documented trade for a harmonious light palette. Where you use those, the **relief rule**
applies: ship visible direct labels or a table view alongside. Not optional; the checker
prints `RELIEF` to remind you.

Three more rules, being the most common chart mistakes:

- **Never a dual-axis chart.** Two measures on two scales hide exactly the relationship you
  built the chart to show. Use two charts side by side.
- **Colour follows the entity, never its rank.** A filter that changes which series are
  present must not repaint the survivors.
- **Bars are scaled against the largest value in the set**, never each normalised to 100%,
  or rows stop being comparable to each other.

A zero-value row renders as a dashed outline at full width (`.bar--empty`), not a missing
row. Absence is data.

---

## 9. Accessibility

Built in: a visible `:focus-visible` ring on every interactive element,
`prefers-reduced-motion` honoured, a `forced-colors` block that lets Windows high-contrast
through rather than fighting it, 44px touch targets below 600px, and a print stylesheet that
drops the chrome and keeps the data.

Measured, not judged: every ink/surface pair in both themes clears WCAG AA 4.5:1. The
tightest are `--fg-3` on `--inset` (4.64 light, 5.26 dark) and the mint accent on white
(4.89).

---

## 10. Checklist for a new screen

- [ ] No raw hex anywhere in the template.
- [ ] Exactly one `.btn--primary`.
- [ ] Every status has a word beside the colour.
- [ ] Every headline number has a delta or a sub-line.
- [ ] Every chart panel has a `.panel__foot` saying what it means.
- [ ] Anything the user must act on is above the fold and states its consequence.
- [ ] Both themes checked; both densities render.
- [ ] 390px: no horizontal page scroll, controls ≥44px, only tabs and SVG charts scroll.
- [ ] Keyboard: tab order follows visual order, focus visible, no `onclick` on a `div`.
- [ ] `node tools/check-contrast.mjs` passes.

---

## 11. Known gaps

- **No RTL support.** Physical properties (`border-left`, `translateX`) are used throughout.
  Converting to logical properties is mechanical but has not been done.
- **No component JS.** Tabs, disclosure menus and toasts are markup patterns here, not
  behaviours. The only script is the theme switcher.
- **Tablet portrait (~768px)** falls in the ≤900px band and should be fine, but is the one
  width nobody has specifically designed for.
- **The density scale is two steps.** A third, tighter step would need the type scale
  revisited, not just the spacing.
