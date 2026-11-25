import { expect, signIn, test, type Persona } from "./support";

// persona -> the pages its roles add to the navigation
const personas: [Persona, string[]][] = [
  ["alice.employee", []],
  ["sam.security", []],
  ["ada.auditor", ["Signals"]],
  ["kim.admin", ["Knowledge"]],
  ["sue.agent", []],
  ["hank.hr", []],
  ["fiona.finance", []],
];

for (const [persona, extra] of personas) {
  test(`${persona} signs in, sees only their pages and signs out`, async ({ page }) => {
    await signIn(page, persona);
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link")).toHaveText(["Ask", "Tickets", ...extra]);

    await page.locator("#signout-button").click();
    // Sign-out ends the identity provider's session too, so the app asks again.
    await expect(page.locator("#signout-button")).toHaveCount(0);
    await page.goto("/");
    await expect(page).toHaveURL(/\/welcome$/);
  });
}

test("a signed-out visitor to a protected page is sent to sign in", async ({ page }) => {
  await page.goto("/tickets");
  await expect(page.locator('input[name="username"]')).toBeVisible();
});

test("a page a role does not allow is refused", async ({ as }) => {
  const alice = await as("alice.employee");
  for (const path of ["/admin/documents", "/monitoring"]) {
    const response = await alice.goto(path);
    expect(response?.status(), path).toBe(403);
    await expect(alice.getByRole("heading", { name: "Not allowed" })).toBeVisible();
  }
});
