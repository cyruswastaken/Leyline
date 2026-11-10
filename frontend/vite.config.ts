import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

// One entry per page. Flask reads dist/.vite/manifest.json to find the hashed
// files (see src/leyline/frontend.py), so entry names must match the templates.
const entries = ["landing", "chat", "tickets", "admin", "monitoring", "error"];

export default defineConfig({
  plugins: [tailwindcss()],
  // Flask serves the build under /static/dist/.
  base: "/static/dist/",
  build: {
    outDir: "../src/leyline/static/dist",
    emptyOutDir: true,
    // A visible file name (not .vite/manifest.json) so Python package data includes it.
    manifest: "manifest.json",
    target: "es2022",
    rollupOptions: {
      input: Object.fromEntries(entries.map((name) => [name, `src/entries/${name}.ts`])),
    },
  },
  server: { host: "localhost", port: 5173, strictPort: true, cors: true },
  test: { environment: "jsdom", include: ["src/**/*.test.ts"] },
});
