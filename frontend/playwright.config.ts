import { defineConfig, devices } from "@playwright/test";

// Browser tests against the running stack (compose.e2e.yaml at the repository root
// says how to start it). E2E_BROWSER_CHANNEL=msedge uses an installed browser
// instead of Playwright's own download; CI installs Chromium.
const channel = process.env.E2E_BROWSER_CHANNEL;

export default defineConfig({
  testDir: "./e2e",
  // The journeys share one database (documents, tickets), so they run one at a time.
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    // localhost, not 127.0.0.1: the identity provider's redirect URIs name it.
    baseURL: "http://localhost:8000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], ...(channel ? { channel } : {}) } }],
});
