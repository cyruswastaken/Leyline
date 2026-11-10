import { api } from "../../lib/api";
import { twoStep } from "../../lib/confirm";
import { byId, clear, el } from "../../lib/dom";
import { pixelIcon } from "../../lib/pixel-icon";
import type { ConversationSummary } from "../../lib/types";

export interface SidebarHandlers {
  open(id: string): void;
  /** Called after the active conversation was deleted. */
  deletedActive(): void;
}

/**
 * The conversation list: titles from the server (rendered as text), open on
 * click, and a two-step delete (the button asks to be confirmed, then deletes)
 * instead of a native dialog.
 */
export class Sidebar {
  private readonly list = byId<HTMLUListElement>("conversation-list");
  private active: string | null = null;

  constructor(private readonly handlers: SidebarHandlers) {}

  setActive(id: string | null): void {
    this.active = id;
  }

  async refresh(): Promise<void> {
    const response = await api("/api/conversations");
    if (!response.ok) return;
    const { conversations } = (await response.json()) as { conversations: ConversationSummary[] };
    clear(this.list);
    if (conversations.length === 0) {
      this.list.appendChild(el("li", { class: "label px-2 py-3", text: "No conversations yet" }));
      return;
    }
    for (const conversation of conversations) this.list.appendChild(this.row(conversation));
  }

  private row(conversation: ConversationSummary): HTMLLIElement {
    const isActive = conversation.id === this.active;
    const open = el("button", {
      class: `flex-1 min-w-0 truncate px-3 py-2 text-left text-sm hover:bg-white/5 ${isActive ? "text-lime" : "text-text"}`,
      text: conversation.title,
      attrs: { type: "button", title: conversation.title, ...(isActive ? { "aria-current": "true" } : {}) },
    });
    open.addEventListener("click", () => this.handlers.open(conversation.id));

    const remove = el(
      "button",
      {
        class: "btn btn-quiet btn-sm btn-icon border-transparent text-muted hover:text-danger",
        attrs: { type: "button", "aria-label": `Delete conversation: ${conversation.title}` },
      },
      [pixelIcon("trash", { size: 14 })],
    );
    twoStep(remove, async () => {
      const response = await api(`/api/conversations/${conversation.id}`, { method: "DELETE" });
      if (response.ok && conversation.id === this.active) this.handlers.deletedActive();
      else await this.refresh();
    });

    return el("li", { class: `flex items-center border-l-2 ${isActive ? "border-lime bg-white/5" : "border-transparent"}` }, [
      open,
      remove,
    ]);
  }
}
