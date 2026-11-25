import { readFileSync } from "node:fs";
import { AxeBuilder } from "@axe-core/playwright";
import { expect, test as base, type Browser, type Locator, type Page } from "@playwright/test";

interface RealmUser {
  username: string;
  credentials: { value: string }[];
}

// The demo accounts and their throwaway passwords come from the local realm file,
// so they are written down in one place only.
const realm = JSON.parse(
  readFileSync(new URL("../../deploy/keycloak/leyline-realm.json", import.meta.url), "utf-8"),
) as { users: RealmUser[] };

export type Persona =
  | "alice.employee"
  | "sam.security"
  | "ada.auditor"
  | "kim.admin"
  | "sue.agent"
  | "hank.hr"
  | "fiona.finance";

function passwordOf(username: Persona): string {
  const value = realm.users.find((u) => u.username === username)?.credentials[0]?.value;
  if (!value) throw new Error(`no demo password for ${username} in the realm file`);
  return value;
}

/** Signs in through the real Keycloak page and waits to be back in the app. */
export async function signIn(page: Page, username: Persona): Promise<void> {
  await page.goto("/login?next=/");
  await page.locator('input[name="username"]').fill(username);
  await page.locator('input[name="password"]').fill(passwordOf(username));
  await page.locator('[type="submit"]').click();
  await page.waitForURL("http://localhost:8000/**");
  await expect(page.locator("#signout-button")).toBeVisible();
}

/**
 * `as(persona)` opens a signed-in page in its own browser context, so one test can
 * hold several people at once. Every page also records Content-Security-Policy
 * violations, which fail the test: the UI must work under its own policy.
 */
export const test = base.extend<{ as: (username: Persona) => Promise<Page>; cspViolations: string[] }>({
  cspViolations: [
    async ({}, use) => {
      const violations: string[] = [];
      await use(violations);
      expect(violations, "Content-Security-Policy violations").toEqual([]);
    },
    { auto: true },
  ],
  page: async ({ page, cspViolations }, use) => {
    watchCsp(page, cspViolations);
    await use(page);
  },
  as: async ({ browser, cspViolations }, use) => {
    const opened: Awaited<ReturnType<Browser["newContext"]>>[] = [];
    await use(async (username) => {
      const context = await browser.newContext({ baseURL: "http://localhost:8000" });
      opened.push(context);
      const page = await context.newPage();
      watchCsp(page, cspViolations);
      await signIn(page, username);
      return page;
    });
    await Promise.all(opened.map((context) => context.close()));
  },
});

export { expect };

function watchCsp(page: Page, violations: string[]): void {
  page.on("console", (message) => {
    const text = message.text();
    // Only the app's own pages: the identity provider has its own policy.
    if (page.url().startsWith("http://localhost:8000") && text.includes("Content Security Policy")) {
      violations.push(`${page.url()}: ${text}`);
    }
  });
}

/** Asks one question in a new conversation and returns the finished answer. */
export async function ask(page: Page, question: string): Promise<Locator> {
  await page.getByRole("button", { name: "{New chat}" }).click();
  await page.locator("#message-input").fill(question);
  await page.locator("#send-button").click();
  // The composer is disabled while an answer streams and comes back when it ends.
  await expect(page.locator("#send-button")).toBeVisible();
  await expect(page.locator("#message-input")).toBeEnabled();
  const answer = page.locator("#messages .msg").last();
  await expect(answer).not.toContainText("Thinking");
  // A real reply, not an error box: otherwise "the answer does not mention X"
  // would pass for a broken app.
  await expect(answer).toContainText("Leyline");
  await expect(answer.getByRole("alert")).toHaveCount(0);
  return answer;
}

export function sources(answer: Locator): Locator {
  return answer.getByRole("list", { name: "Sources" });
}

/** The accessibility audit: no serious or critical violation on this page. */
export async function expectAccessible(page: Page, what: string): Promise<void> {
  // Audit the finished page, not a half-faded one.
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      document
        .getAnimations()
        .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)
        .map((a) => a.finished.catch(() => undefined)),
    );
  });
  const results = await new AxeBuilder({ page }).analyze();
  const bad = results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(bad, `accessibility violations on ${what}`).toEqual([]);
}
