import { errorMessage, postJson } from "../lib/api";
import { byId } from "../lib/dom";

/**
 * The support-ticket dialog. Opened from a conversation it is prefilled with a
 * draft the server builds from the user's own questions only; opened from the
 * tickets page it starts blank. Agents see exactly what is submitted here.
 */
export function initTicketDialog(openButtonId: string, getConversationId: () => string | null = () => null): void {
  const dialog = byId<HTMLDialogElement>("escalate");
  const subject = byId<HTMLInputElement>("escalate-subject");
  const description = byId<HTMLTextAreaElement>("escalate-description");
  const error = byId("escalate-error");

  byId(openButtonId).addEventListener("click", async () => {
    error.textContent = "";
    subject.value = "";
    description.value = "";
    const conversationId = getConversationId();
    if (conversationId) {
      const response = await postJson(`/api/conversations/${conversationId}/escalation-draft`, {});
      if (response.ok) {
        const draft = (await response.json()) as { subject: string; description: string };
        subject.value = draft.subject;
        description.value = draft.description;
      }
    }
    dialog.showModal();
    subject.focus();
  });

  byId("escalate-cancel").addEventListener("click", () => dialog.close());

  byId("escalate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    const response = await postJson("/api/tickets", { subject: subject.value, description: description.value });
    if (!response.ok) {
      error.textContent = await errorMessage(response, "Could not create the ticket.");
      return;
    }
    const ticket = (await response.json()) as { id: string };
    dialog.close();
    window.location.assign(`/tickets?id=${encodeURIComponent(ticket.id)}`);
  });
}
