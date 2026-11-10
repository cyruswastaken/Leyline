// Accessibility audits run in a real browser: this copies axe-core into the
// (git-ignored) build output so a page can load it from its own origin without
// loosening the Content-Security-Policy. See docs/FRONTEND.md. Never commit or
// ship the copy; `npm run build` wipes it.
import { copyFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const target = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "leyline", "static", "dist", "axe.js");
copyFileSync(require.resolve("axe-core/axe.min.js"), target);
console.log(`copied axe-core to ${target}`);
