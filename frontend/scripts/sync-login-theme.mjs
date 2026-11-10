// Keeps the Keycloak login theme (deploy/keycloak/themes/leyline) in step with the
// app's design system: copies the same self-hosted fonts and renders the pixel
// globe to a PNG with the very code the app draws it with (src/lib/globe.ts).
// Run `npm run theme:sync` after changing the fonts or the globe; the outputs
// are committed because Keycloak serves the theme as plain files.
//
// Needs Node 22.18+/24 (it imports TypeScript directly).
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";
import { renderDither } from "../src/lib/dither.ts";
import { renderGlobe } from "../src/lib/globe.ts";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const resources = join(here, "..", "..", "deploy", "keycloak", "themes", "leyline", "login", "resources");

// --- fonts (SIL Open Font License; see the licence files copied alongside) -------
const fonts = [
  ["@fontsource/silkscreen", "silkscreen-latin-400-normal.woff2"],
  ["@fontsource/silkscreen", "silkscreen-latin-700-normal.woff2"],
  ["@fontsource/jetbrains-mono", "jetbrains-mono-latin-400-normal.woff2"],
  ["@fontsource/jetbrains-mono", "jetbrains-mono-latin-600-normal.woff2"],
  ["@fontsource/ibm-plex-sans", "ibm-plex-sans-latin-400-normal.woff2"],
  ["@fontsource/ibm-plex-sans", "ibm-plex-sans-latin-500-normal.woff2"],
  ["@fontsource/ibm-plex-sans", "ibm-plex-sans-latin-600-normal.woff2"],
];
mkdirSync(join(resources, "fonts"), { recursive: true });
for (const [pkg, file] of fonts) {
  const root = dirname(require.resolve(`${pkg}/package.json`));
  copyFileSync(join(root, "files", file), join(resources, "fonts", file));
}
for (const pkg of ["silkscreen", "jetbrains-mono", "ibm-plex-sans"]) {
  const root = dirname(require.resolve(`@fontsource/${pkg}/package.json`));
  copyFileSync(join(root, "LICENSE"), join(resources, "fonts", `LICENSE-${pkg}.txt`));
}

// --- the globe, as a PNG (a minimal encoder: RGBA, no filtering) ------------------
function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const out = Buffer.alloc(8 + data.length + 4);
  out.writeUInt32BE(data.length, 0);
  body.copy(out, 4);
  out.writeUInt32BE(crc32(body), 8 + data.length);
  return out;
}
function png(width, height, rgba) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 6, 0, 0, 0], 8); // 8-bit RGBA
  const rows = Buffer.alloc(height * (width * 4 + 1));
  for (let y = 0; y < height; y++) {
    rows[y * (width * 4 + 1)] = 0; // filter: none
    Buffer.from(rgba.buffer, y * width * 4, width * 4).copy(rows, y * (width * 4 + 1) + 1);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(rows, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const SIZE = 112;
mkdirSync(join(resources, "img"), { recursive: true });
writeFileSync(join(resources, "img", "globe.png"), png(SIZE, SIZE, renderGlobe(SIZE, 0.6)));

// --- the dithered landscape along the bottom of the page (drawn at 4x, pixelated) ---
const DITHER_W = 480;
const DITHER_H = 48;
writeFileSync(join(resources, "img", "dither.png"), png(DITHER_W, DITHER_H, renderDither(DITHER_W, DITHER_H, "landscape", 0)));
console.log(`login theme assets written to ${resources}`);
