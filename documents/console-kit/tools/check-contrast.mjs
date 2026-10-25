#!/usr/bin/env node
// Contrast + palette checker for the Console Kit.
// Usage:  node tools/check-contrast.mjs            # check the built-in token set
//         node tools/check-contrast.mjs "#RRGGBB" "#RRGGBB"   # check one pair
// No dependencies.

const lum = (h) => {
  const v = h.replace('#', '');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16) / 255);
  const f = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
export const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
};

// AA: 4.5 for body text, 3.0 for large text (>=24px) and for non-text fills.
const PASS_TEXT = 4.5, PASS_FILL = 3.0;

export function check(pairs) {
  let bad = 0;
  for (const [name, fg, bg, kind = 'text'] of pairs) {
    const r = ratio(fg, bg);
    const need = kind === 'text' ? PASS_TEXT : PASS_FILL;
    const ok = r >= need;
    // A categorical chart hue under 3:1 is allowed ONLY with relief: visible
    // direct labels or a table view. It is reported, never silently passed.
    const relief = !ok && kind === 'relief';
    if (!ok && !relief) bad++;
    const tag = ok ? 'PASS' : relief ? 'RELIEF' : 'FAIL';
    console.log(
      `${tag.padEnd(6)}${r.toFixed(2).padStart(5)}  (need ${need})  ${name}  ${fg} on ${bg}`
    );
  }
  console.log(bad ? `\n${bad} pair(s) FAILED` : '\nall pairs pass');
  return bad;
}

const DARK = {
  bg: '#0B0E13', surface: '#131821', surface2: '#1A212C', inset: '#0F141C',
  fg: '#E8ECF3', fg2: '#9AA6B8', fg3: '#7C8A9E',
  accent: '#3DDC97', accentInk: '#04231A',
  pos: '#3DDC97', warn: '#F5B544', neg: '#FF7A7A', info: '#6BA6FF',
  posBg: '#12211B', warnBg: '#241D0F', negBg: '#2A1414', infoBg: '#101A26',
  posInk: '#6FE9B5', warnInk: '#F5B544', negInk: '#FF7A7A', infoInk: '#6BA6FF',
};
const LIGHT = {
  bg: '#F4F6F5', surface: '#FFFFFF', surface2: '#F0F3F2', inset: '#E9EDEC',
  fg: '#10171C', fg2: '#4B5A63', fg3: '#5E6B74',
  accent: '#0A8158', accentInk: '#FFFFFF',
  pos: '#0A8158', warn: '#96590A', neg: '#C0392B', info: '#1B63C4',
  posBg: '#E7F5EF', warnBg: '#FDF6E7', negBg: '#FDECEC', infoBg: '#EAF2FC',
  posInk: '#07603F', warnInk: '#8A5606', negInk: '#A32F23', infoInk: '#15529F',
};
const suite = (t, mode) => [
  [`${mode} fg / bg`,            t.fg,  t.bg],
  [`${mode} fg / surface`,       t.fg,  t.surface],
  [`${mode} fg-2 / surface`,     t.fg2, t.surface],
  [`${mode} fg-3 / surface`,     t.fg3, t.surface],
  [`${mode} fg-3 / inset`,       t.fg3, t.inset],
  [`${mode} fg-2 / surface-2`,   t.fg2, t.surface2],
  [`${mode} fg-3 / surface-2`,   t.fg3, t.surface2],
  [`${mode} fg-3 / bg`,          t.fg3, t.bg],
  [`${mode} accent / surface`,   t.accent, t.surface],
  [`${mode} accent-ink / accent`,t.accentInk, t.accent],
  [`${mode} warn / surface`,     t.warn, t.surface],
  [`${mode} neg / surface`,      t.neg,  t.surface],
  [`${mode} info / surface`,     t.info, t.surface],
  [`${mode} pos-ink / pos-bg`,   t.posInk,  t.posBg],
  [`${mode} warn-ink / warn-bg`, t.warnInk, t.warnBg],
  [`${mode} neg-ink / neg-bg`,   t.negInk,  t.negBg],
  [`${mode} info-ink / info-bg`, t.infoInk, t.infoBg],
];

const ACCENTS = {
  mint:   { dark: ['#3DDC97', '#04231A'], light: ['#0A8158', '#FFFFFF'] },
  teal:   { dark: ['#2DD4BF', '#04201C'], light: ['#0B6E6E', '#FFFFFF'] },
  blue:   { dark: ['#6BA6FF', '#06142B'], light: ['#1B63C4', '#FFFFFF'] },
  amber:  { dark: ['#F5B544', '#2A1E06'], light: ['#96590A', '#FFFFFF'] },
  violet: { dark: ['#9B8CFA', '#14101F'], light: ['#5B4BC4', '#FFFFFF'] },
};

if (process.argv[2] && process.argv[3]) {
  console.log(ratio(process.argv[2], process.argv[3]).toFixed(2));
} else if (process.argv[1].endsWith('check-contrast.mjs')) {
  console.log('== token set ==');
  let bad = check([...suite(DARK, 'dark'), ...suite(LIGHT, 'light')]);
  console.log('\n== accent presets ==');
  const rows = [];
  for (const [name, v] of Object.entries(ACCENTS)) {
    rows.push([`${name} accent / dark surface`,  v.dark[0],  DARK.surface]);
    rows.push([`${name} ink / accent (dark)`,    v.dark[1],  v.dark[0]]);
    rows.push([`${name} accent / light surface`, v.light[0], LIGHT.surface]);
    rows.push([`${name} ink / accent (light)`,   v.light[1], v.light[0]]);
  }
  bad += check(rows);
  console.log('\n== chart fills (need 3.0; RELIEF = needs visible labels or a table view) ==');
  bad += check([
    ['dark  cat-1',  '#3987e5', DARK.surface,  'fill'],
    ['dark  cat-2',  '#d95926', DARK.surface,  'fill'],
    ['dark  cat-3',  '#199e70', DARK.surface,  'fill'],
    ['dark  cat-4',  '#c98500', DARK.surface,  'fill'],
    ['dark  cat-5',  '#d55181', DARK.surface,  'fill'],
    ['dark  cat-6',  '#9085e9', DARK.surface,  'fill'],
    ['dark  muted',  '#5B6B83', DARK.surface,  'fill'],
    ['light cat-1',  '#2a78d6', LIGHT.surface, 'fill'],
    ['light cat-2',  '#eb6834', LIGHT.surface, 'fill'],
    ['light cat-3',  '#1baf7a', LIGHT.surface, 'relief'],
    ['light cat-4',  '#eda100', LIGHT.surface, 'relief'],
    ['light cat-5',  '#e87ba4', LIGHT.surface, 'relief'],
    ['light cat-6',  '#4a3aa7', LIGHT.surface, 'fill'],
    ['light muted',  '#6E7A87', LIGHT.surface, 'fill'],
  ]);
  process.exit(bad ? 1 : 0);
}
