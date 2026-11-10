// Fails the build when a page ships more JavaScript or CSS than its budget.
// Sizes are gzipped, per entry, including the shared chunks it imports (fonts
// are separate files loaded on demand and are not counted here).
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "leyline", "static", "dist");
const manifest = JSON.parse(readFileSync(join(dist, "manifest.json"), "utf-8"));

const JS_BUDGET_KB = 30;
const CSS_BUDGET_KB = 15;

const gzipKb = (file) => gzipSync(readFileSync(join(dist, file))).length / 1024;

function collect(name, seen = new Set()) {
  if (seen.has(name) || !manifest[name]) return { js: new Set(), css: new Set() };
  seen.add(name);
  const item = manifest[name];
  const js = new Set([item.file]);
  const css = new Set(item.css ?? []);
  for (const imported of item.imports ?? []) {
    const sub = collect(imported, seen);
    sub.js.forEach((f) => js.add(f));
    sub.css.forEach((f) => css.add(f));
  }
  return { js, css };
}

let failed = false;
console.log(`${"entry".padEnd(14)} ${"js gz".padStart(9)} ${"css gz".padStart(9)}`);
for (const [name, item] of Object.entries(manifest)) {
  if (!item.isEntry) continue;
  const { js, css } = collect(name);
  const jsKb = [...js].reduce((sum, f) => sum + gzipKb(f), 0);
  const cssKb = [...css].reduce((sum, f) => sum + gzipKb(f), 0);
  const over = jsKb > JS_BUDGET_KB || cssKb > CSS_BUDGET_KB;
  failed ||= over;
  console.log(
    `${name.replace("src/entries/", "").replace(".ts", "").padEnd(14)} ${(jsKb.toFixed(1) + " KB").padStart(9)} ${(cssKb.toFixed(1) + " KB").padStart(9)}${over ? "  OVER BUDGET" : ""}`,
  );
}
console.log(`budgets: ${JS_BUDGET_KB} KB JavaScript, ${CSS_BUDGET_KB} KB CSS per page (gzip)`);
process.exit(failed ? 1 : 0);
