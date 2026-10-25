# Console Kit

A dark-first, dense UI theme for data tools, admin panels and internal dashboards.
Plain CSS. No build step, no framework, no webfont, no network request.

Extracted from a shipped monitoring UI and made product-agnostic, so it can be dropped into
the next project unchanged.

---

## Quick start

Copy this folder into your project (`static/`, `public/`, wherever you serve assets from),
then:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <!-- FIRST and synchronous, or every navigation flashes the wrong theme -->
  <script src="/static/theme.js"></script>
  <link rel="stylesheet" href="/static/theme.css">
  <link rel="stylesheet" href="/static/accents.css">
</head>
<body>
  …
  <!-- inline the sprite once, at the end of body -->
</body>
</html>
```

Open `gallery.html` in a browser to see everything, and `starter.html` for a correct page
skeleton to copy.

That is the whole install. If you want a different brand colour, set
`<html data-accent="teal">` — see *Rebranding* in `THEME.md`.

---

## Files

| File | What it is | Size |
|---|---|---|
| `theme.css` | **The kit.** Tokens (light + dark), density modes, component layer, responsive rules, accessibility and print. | ~25 KB |
| `accents.css` | Four alternate accent hues (teal, blue, amber, violet), each validated in both modes. | ~3 KB |
| `theme.js` | Theme / density / accent switching. Writes localStorage *and* a cookie so a server can stamp the first paint. ~60 lines, no dependencies. | ~3 KB |
| `icons.svg` | 24 stroke icons as an inline sprite. `<use href="#i-plus">`. | ~3 KB |
| `starter.html` | A correct page skeleton — topbar, page head, banner, KPI row, panel. Copy and fill. | — |
| `gallery.html` | Every component, live, with theme / density / accent switchers and a live token table. | — |
| `gallery.standalone.html` | The same gallery with everything inlined — one file, no dependencies. Good for sharing or hosting. | — |
| `THEME.md` | **The spec.** Principles, token reference, component contracts, responsive rules, rebranding, checklist. | — |
| `tools/check-contrast.mjs` | Contrast + palette checker. Run it after touching any colour. | — |
| `build.mjs` | Regenerates the sprite inlining and the standalone gallery. `node build.mjs` | — |

---

## Server-rendered apps

`theme.js` writes a `ck-theme` cookie alongside localStorage. Read it server-side and stamp
the root element yourself:

```jinja
<html lang="en" data-theme="{{ request.cookies.get('ck-theme', '') }}">
```

An empty value means "follow the OS", which is the correct default — the CSS handles it.
Doing this removes the flash entirely rather than merely shortening it.

---

## Verifying a change

```bash
node tools/check-contrast.mjs          # every token pair, both themes, plus the accents
node tools/check-contrast.mjs "#3DDC97" "#131821"   # one pair
```

It exits non-zero on a failure, so it drops straight into CI. `RELIEF` is not a failure —
it marks the three light-mode chart hues that sit below 3:1 and therefore require visible
labels or a table view alongside. See `THEME.md` § Charts.

---

## Conventions this kit assumes

- **No raw hex in application code.** Every colour, radius and spacing value comes from a
  token. `grep -rE "#[0-9a-fA-F]{3,6}"` over your templates should return only the sprite.
- **Semantic HTML.** The kit styles real elements — `<button>`, `<input>` + `<label>`,
  `<table>`. It never replaces them and needs no JavaScript to render.
- **One primary button per view.**
- **Status always carries a word**, never colour alone.
- **Every number carries a comparison.**

The reasoning behind each of those is in `THEME.md`.
