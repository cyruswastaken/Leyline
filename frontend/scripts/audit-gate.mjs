// Fails on any high or critical advisory in the frontend's dependency tree, except the
// advisories listed below. Each exception names an advisory that has NO patched release yet
// and affects build tooling only (nothing shipped to browsers; `npm audit --omit=dev` is
// checked separately and has no exceptions). Remove an entry as soon as a fix exists.
import { spawnSync } from "node:child_process";

const EXCEPTIONS = {
  // picomatch <= 4.0.7 (newest release): ReDoS through crafted glob patterns. Reached only
  // through vite, vitest and tinyglobby, which match globs written in this repository.
  "GHSA-fc5q-39xc-vjpf": "picomatch ReDoS, no patched version, build tooling only",
};

const run = spawnSync("npm", ["audit", "--json"], {
  encoding: "utf8",
  shell: process.platform === "win32",
  maxBuffer: 64 * 1024 * 1024,
});
let report;
try {
  report = JSON.parse(run.stdout);
} catch {
  console.error("audit-gate: could not read the output of `npm audit --json`");
  console.error(run.stderr || run.stdout);
  process.exit(2);
}
if (report.error) {
  console.error(`audit-gate: npm audit failed: ${report.error.summary ?? report.error.code}`);
  process.exit(2);
}

const failures = new Map();
const excepted = new Set();
for (const [name, entry] of Object.entries(report.vulnerabilities ?? {})) {
  for (const via of entry.via ?? []) {
    if (typeof via === "string") continue; // a dependency of a vulnerable package
    if (via.severity !== "high" && via.severity !== "critical") continue;
    const id = String(via.url ?? "").split("/").pop() ?? "";
    if (id in EXCEPTIONS) {
      excepted.add(id);
      continue;
    }
    failures.set(`${name}: ${via.title} (${via.url})`, via.severity);
  }
}

for (const id of excepted) console.log(`audit-gate: allowed ${id}: ${EXCEPTIONS[id]}`);
if (failures.size > 0) {
  for (const [text, severity] of failures) console.error(`audit-gate: ${severity} ${text}`);
  process.exit(1);
}
console.log("audit-gate: no high or critical advisories outside the documented exceptions");
