import { ask, expect, expectAccessible, test } from "./support";

test("public pages and the sign-in page", async ({ page }) => {
  await page.goto("/welcome");
  await expectAccessible(page, "welcome");

  await page.goto("/login?next=/");
  await expect(page.locator('input[name="username"]')).toBeVisible();
  await expectAccessible(page, "the identity provider's sign-in page");
});

test("error pages", async ({ page, as }) => {
  await page.goto("/auth/callback");
  await expect(page.getByRole("heading", { name: "Sign-in failed" })).toBeVisible();
  await expectAccessible(page, "sign-in failed");

  const alice = await as("alice.employee");
  await alice.goto("/no-such-page");
  await expect(alice.getByRole("heading", { name: "Page not found" })).toBeVisible();
  await expectAccessible(alice, "page not found");
});

test("chat: empty, answered, escalation dialog, then the ticket", async ({ as }) => {
  const alice = await as("alice.employee");
  await expectAccessible(alice, "chat, empty");

  await ask(alice, "How do I connect to the corporate VPN?");
  await expectAccessible(alice, "chat, with an answer");

  await alice.locator("#escalate-button").click();
  await expect(alice.locator("#escalate")).toBeVisible();
  await expectAccessible(alice, "escalation dialog");

  await alice.locator("#escalate-subject").fill(`E2E accessibility ${Date.now()}`);
  await alice.getByRole("button", { name: "{Send to support}" }).click();
  await expect(alice.locator("#detail").getByRole("heading")).toBeVisible();
  await expectAccessible(alice, "tickets, one open");

  const sue = await as("sue.agent");
  await sue.goto("/tickets");
  await sue.locator("#queue").getByRole("button").first().click();
  await expect(sue.locator("#detail").getByRole("combobox", { name: "Status" })).toBeVisible();
  await expectAccessible(sue, "tickets, agent view");
});

test("knowledge administration", async ({ as }) => {
  const kim = await as("kim.admin");
  await kim.goto("/admin/documents");
  await expect(kim.locator("#documents tr").first()).toBeVisible();
  await expectAccessible(kim, "knowledge");
});

test("signals", async ({ as }) => {
  const ada = await as("ada.auditor");
  await ada.goto("/monitoring");
  await expect(ada.locator("#events-body tr").first()).toBeVisible();
  await expectAccessible(ada, "signals");
});
