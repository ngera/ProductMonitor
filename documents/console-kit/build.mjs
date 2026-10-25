/* Inlines the icon sprite into the HTML files, and produces a fully
   self-contained gallery (no external files) for sharing or hosting.
   Run:  node build.mjs   */
import { readFileSync, writeFileSync } from 'node:fs';

const sprite = readFileSync('icons.svg', 'utf8').replace(/^<!--[\s\S]*?-->\s*/, '');
const css    = readFileSync('theme.css', 'utf8');
const accents= readFileSync('accents.css', 'utf8');
const js     = readFileSync('theme.js', 'utf8');

for (const f of ['gallery.html', 'starter.html']) {
  const out = readFileSync(f, 'utf8').replace('<!--ICONS-->', sprite);
  writeFileSync(f, out);
  console.log('sprite inlined ->', f);
}

const standalone = readFileSync('gallery.html', 'utf8')
  .replace('<script src="./theme.js"></script>', `<script>\n${js}\n</script>`)
  .replace('<link rel="stylesheet" href="./theme.css">', `<style>\n${css}\n</style>`)
  .replace('<link rel="stylesheet" href="./accents.css">', `<style>\n${accents}\n</style>`);
writeFileSync('gallery.standalone.html', standalone);
console.log('wrote gallery.standalone.html', standalone.length, 'bytes');
