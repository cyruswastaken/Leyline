import { ask, expect, sources, test } from "./support";

const QUESTION = "How do I connect to the corporate VPN?";

test("stream an answer, stop one, rate it, escalate and resolve", async ({ as }) => {
  const alice = await as("alice.employee");

  // A full answer, with its citation and source.
  const answer = await ask(alice, QUESTION);
  await expect(answer).toContainText("Contact the helpdesk if the problem continues");
  await expect(sources(answer)).toContainText("Corporate VPN Setup Guide");

  const helpful = answer.getByRole("button", { name: "Helpful", exact: true });
  await helpful.click();
  await expect(helpful).toHaveAttribute("aria-pressed", "true");

  // Stop a second answer part-way: what arrived stays, and the composer comes back.
  await alice.locator("#message-input").fill(QUESTION);
  await alice.locator("#send-button").click();
  const partial = alice.locator("#messages .msg").last();
  await expect(partial).toContainText("Open the approved");
  await alice.locator("#stop-button").click();
  await expect(partial).toContainText("Stopped before finishing");
  await expect(partial).not.toContainText("Contact the helpdesk");
  await expect(alice.locator("#message-input")).toBeEnabled();

  // Escalate: the draft holds the person's own question, never the assistant's text.
  await alice.locator("#escalate-button").click();
  const description = alice.locator("#escalate-description");
  await expect(description).toHaveValue(new RegExp("corporate VPN"));
  await expect(description).not.toHaveValue(/Open the approved/);
  const subject = `E2E VPN ${Date.now()}`;
  await alice.locator("#escalate-subject").fill(subject);
  await alice.getByRole("button", { name: "{Send to support}" }).click();
  await expect(alice).toHaveURL(/\/tickets\?id=/);
  await expect(alice.locator("#detail").getByRole("heading", { name: subject })).toBeVisible();

  // A support agent takes it and resolves it.
  const sue = await as("sue.agent");
  await sue.goto("/tickets");
  await sue.locator("#queue").getByRole("button", { name: subject }).click();
  const detail = sue.locator("#detail");
  await detail.getByRole("button", { name: "{Assign to me}" }).click();
  await expect(detail).toContainText("Assigned to");
  await detail.getByRole("textbox", { name: "Comment" }).fill("On my way.");
  await detail.getByRole("button", { name: "{Comment}" }).click();
  await expect(detail.getByRole("list", { name: "History" })).toContainText("On my way.");
  await detail.getByRole("combobox", { name: "Status" }).selectOption("resolved");
  await expect(detail.locator(".badge").first()).toHaveText(/resolved/i);

  // The requester sees the outcome.
  await alice.reload();
  await expect(alice.locator("#detail").locator(".badge").first()).toHaveText(/resolved/i);
  await expect(alice.locator("#detail")).toContainText("On my way.");
});
