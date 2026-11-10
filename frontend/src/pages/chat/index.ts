import { SignedOutError, api, errorMessage as readError, postJson } from "../../lib/api";
import { byId, clear, el } from "../../lib/dom";
import { startGlobe, type GlobeHandle } from "../../lib/globe";
import { renderMarkdown } from "../../lib/markdown";
import { readEvents } from "../../lib/sse";
import type { StoredConversation } from "../../lib/types";
import { botMessage, errorMessage, finishBotMessage, userMessage } from "./messages";
import { initTicketDialog } from "../ticket-dialog";
import { Sidebar } from "./sidebar";

const SUGGESTIONS = [
  "How do I connect to the corporate VPN?",
  "I lost my laptop, what do I do?",
  "How do I reset my password?",
];

export function initChat(): void {
  const messages = byId("messages");
  const input = byId<HTMLTextAreaElement>("message-input");
  const composer = byId<HTMLFormElement>("composer");
  const sendButton = byId<HTMLButtonElement>("send-button");
  const stopButton = byId<HTMLButtonElement>("stop-button");
  const status = byId("status");
  const sidebarEl = byId("sidebar");
  const menuButton = byId<HTMLButtonElement>("menu-button");

  let conversationId: string | null = null;
  let controller: AbortController | null = null;
  let globe: GlobeHandle | null = null;

  const announce = (text: string): void => {
    status.textContent = text;
  };
  const scrollDown = (): void => {
    messages.scrollTop = messages.scrollHeight;
  };

  const sidebar = new Sidebar({
    open: (id) => void openConversation(id),
    deletedActive: () => newChat(),
  });

  // --- the empty state: a pixel globe, the title and a few example questions ------
  function showWelcome(): void {
    clearMessages();
    const canvas = el("canvas", { class: "pixelated mx-auto size-44", attrs: { "aria-hidden": "true" } });
    const suggestions = SUGGESTIONS.map((text) => {
      const button = el("button", { class: "btn btn-quiet text-left normal-case tracking-normal", text, attrs: { type: "button" } });
      button.addEventListener("click", () => {
        input.value = text;
        composer.requestSubmit();
      });
      return button;
    });
    const welcome = el("div", { class: "mx-auto flex max-w-xl flex-col items-center gap-5 py-10 text-center", data: { welcome: "1" } }, [
      canvas,
      el("p", { class: "display text-5xl sm:text-6xl", text: "Ask/", attrs: { "aria-hidden": "true" } }),
      el("p", { class: "max-w-md text-muted", text: "Answers from the documents you are allowed to see, with the sources shown." }),
      el("div", { class: "flex flex-col gap-2 w-full" }, suggestions),
    ]);
    messages.appendChild(welcome);
    globe = startGlobe(canvas);
  }

  function clearMessages(): void {
    globe?.stop();
    globe = null;
    clear(messages);
  }

  function add(node: HTMLElement): void {
    if (messages.querySelector("[data-welcome]")) clearMessages();
    messages.appendChild(node);
    scrollDown();
  }

  // --- conversations --------------------------------------------------------------
  function closeSidebar(): void {
    sidebarEl.removeAttribute("data-open");
    menuButton.setAttribute("aria-expanded", "false");
  }

  function newChat(): void {
    controller?.abort();
    conversationId = null;
    sidebar.setActive(null);
    showWelcome();
    void sidebar.refresh();
    input.focus();
    closeSidebar();
  }

  async function openConversation(id: string): Promise<void> {
    controller?.abort();
    const response = await api(`/api/conversations/${id}`);
    if (!response.ok) {
      add(errorMessage("That conversation is no longer available."));
      void sidebar.refresh();
      return;
    }
    const data = (await response.json()) as StoredConversation;
    conversationId = data.id;
    sidebar.setActive(data.id);
    clearMessages();
    for (const message of data.messages) {
      if (message.role === "user") {
        messages.appendChild(userMessage(message.content));
      } else if (message.status === "error") {
        messages.appendChild(errorMessage(message.content));
      } else {
        const parts = botMessage();
        finishBotMessage(parts, message, announce);
        messages.appendChild(parts.wrap);
      }
    }
    scrollDown();
    void sidebar.refresh();
    closeSidebar();
    input.focus();
  }

  // --- sending a question ---------------------------------------------------------
  function setBusy(busy: boolean): void {
    sendButton.hidden = busy;
    stopButton.hidden = !busy;
    input.disabled = busy;
    if (!busy) input.focus();
  }

  function autoGrow(): void {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }

  async function sendMessage(text: string): Promise<void> {
    add(userMessage(text));
    input.value = "";
    autoGrow();
    setBusy(true);

    const parts = botMessage();
    parts.bubble.textContent = "Thinking…";
    add(parts.wrap);
    announce("Thinking");

    controller = new AbortController();
    let streamed = "";
    let finished = false;
    try {
      const response = await postJson(
        "/api/chat/stream",
        { message: text, conversation_id: conversationId },
        { signal: controller.signal },
      );
      if (!response.ok) {
        parts.wrap.remove();
        add(errorMessage(await readError(response)));
        return;
      }
      for await (const event of readEvents(response)) {
        if (event.type === "meta") {
          conversationId = event.conversation_id;
          sidebar.setActive(conversationId);
        } else if (event.type === "delta") {
          streamed += event.text;
          parts.bubble.classList.add("streaming");
          renderMarkdown(parts.bubble, streamed);
          scrollDown();
        } else if (event.type === "done") {
          finished = true;
          finishBotMessage(parts, { id: event.message_id, content: event.content, citations: event.citations }, announce);
          announce("Answer complete");
        } else if (event.type === "error") {
          finished = true;
          parts.wrap.remove();
          add(errorMessage(event.error || "The assistant is temporarily unavailable."));
        }
      }
      if (!finished) {
        parts.bubble.classList.remove("streaming");
        parts.bubble.textContent = streamed || "The connection was interrupted.";
      }
    } catch (error) {
      if (error instanceof SignedOutError) return;
      if (error instanceof DOMException && error.name === "AbortError") {
        parts.bubble.classList.remove("streaming");
        if (streamed) {
          renderMarkdown(parts.bubble, streamed);
          parts.wrap.appendChild(el("div", { class: "label", text: "Stopped before finishing" }));
        } else {
          parts.wrap.remove();
        }
      } else {
        parts.wrap.remove();
        add(errorMessage("Unable to reach the server. Please try again."));
      }
    } finally {
      controller = null;
      setBusy(false);
      void sidebar.refresh();
    }
  }

  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (text && !controller) void sendMessage(text);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  input.addEventListener("input", autoGrow);
  stopButton.addEventListener("click", () => controller?.abort());
  byId("new-chat").addEventListener("click", newChat);
  menuButton.addEventListener("click", () => {
    const open = sidebarEl.toggleAttribute("data-open");
    menuButton.setAttribute("aria-expanded", String(open));
  });

  initTicketDialog("escalate-button", () => conversationId);
  showWelcome();
  void sidebar.refresh();
  input.focus();
}
