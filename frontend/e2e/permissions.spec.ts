import { ask, expect, sources, test } from "./support";

const RESTRICTED = "Internal Network Architecture Overview";
const QUESTION = "internal network architecture segments administrative interfaces";

test("a Security-only document is cited for Security and invisible to staff", async ({ as }) => {
  const sam = await as("sam.security");
  await expect(sources(await ask(sam, QUESTION))).toContainText(RESTRICTED);

  const alice = await as("alice.employee");
  const answer = await ask(alice, QUESTION);
  await expect(answer).not.toContainText(RESTRICTED);
});

test("upload, ask, cite, revoke, delete, ask again", async ({ as }) => {
  const marker = `e2eplatypus${Date.now()}`;
  const title = `E2E note ${marker}`;

  const kim = await as("kim.admin");
  await kim.goto("/admin/documents");
  await kim.locator("#file").setInputFiles({
    name: "note.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(`# Note\n\nThe ${marker} procedure is documented here.`),
  });
  await kim.getByLabel("Title").fill(title);
  await kim.getByLabel("Groups allowed to read", { exact: true }).fill("staff");
  await kim.getByRole("button", { name: "{Upload}" }).click();

  const row = kim.locator("#documents tr").filter({ hasText: title });
  await expect(row.locator(".badge")).toHaveText("indexed", { timeout: 60_000 });

  const alice = await as("alice.employee");
  await expect(sources(await ask(alice, marker))).toContainText(title);

  // Take the group away: access ends at the very next question...
  const groups = row.getByRole("textbox", { name: `Groups allowed to read ${title}` });
  await groups.fill("hr");
  await row.getByRole("button", { name: "{Save}" }).click();
  await expect(kim.getByRole("status")).toContainText("Access updated");
  await expect(await ask(alice, marker)).not.toContainText(title);

  // ...and the source of the earlier answer is no longer named when it is reopened.
  await alice.reload();
  await alice.locator("#conversation-list").getByRole("button", { name: marker, exact: true }).last().click();
  await expect(alice.locator("#messages")).toContainText("Source no longer available");
  await expect(alice.locator("#messages")).not.toContainText(title);

  const hank = await as("hank.hr");
  await expect(sources(await ask(hank, marker))).toContainText(title);

  // Deleting asks to be confirmed, then the document is gone for everyone.
  await row.getByRole("button", { name: "Delete" }).click();
  await row.getByRole("button", { name: "{Delete?}" }).click();
  await expect(kim.getByRole("status")).toContainText("no longer searchable");
  await expect(row).toHaveCount(0);
  await expect(await ask(hank, marker)).not.toContainText(title);
});
