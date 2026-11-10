import { postJson } from "../../lib/api";
import { append, el } from "../../lib/dom";
import { renderMarkdown } from "../../lib/markdown";
import { pixelIcon } from "../../lib/pixel-icon";
import type { SourceCitation } from "../../lib/sse";
import type { StoredMessage } from "../../lib/types";

export interface BotMessage {
  wrap: HTMLElement;
  bubble: HTMLElement;
}

const WRAP = "msg flex flex-col gap-2 max-w-[44rem]";

export function userMessage(text: string): HTMLElement {
  return el("div", { class: `${WRAP} ml-auto` }, [
    el("div", {
      class: "border border-lime/60 bg-lime/5 px-4 py-3 whitespace-pre-wrap overflow-wrap-anywhere",
      text,
    }),
  ]);
}

export function botMessage(): BotMessage {
  const bubble = el("div", { class: "prose-answer border border-line bg-panel px-4 py-3" });
  const wrap = el("div", { class: WRAP }, [
    el("div", { class: "label marker", text: "Leyline" }),
    bubble,
  ]);
  return { wrap, bubble };
}

export function errorMessage(text: string): HTMLElement {
  return el("div", { class: `${WRAP}` }, [
    el("div", {
      class: "border border-danger/70 bg-danger/10 px-4 py-3 text-danger",
      text,
      attrs: { role: "alert" },
    }),
  ]);
}

/** "Document > Section" headings already include the title, so do not repeat it. */
export function sourceTitle(c: SourceCitation): string {
  if (c.available === false || !c.heading) return c.title;
  return c.heading.startsWith(c.title) ? c.heading : `${c.title} › ${c.heading}`;
}

function renderSources(wrap: HTMLElement, messageId: string, citations: SourceCitation[]): void {
  const shown = citations.filter((c) => c.n);
  if (shown.length === 0) return;
  const list = el("ol", { class: "grid gap-1 list-none p-0 m-0", attrs: { "aria-label": "Sources" } });
  for (const c of shown) {
    const consulted = c.cited === false;
    const row = el(
      "li",
      {
        class: `flex items-start gap-2 border border-line px-3 py-2 text-sm ${consulted ? "text-muted" : "bg-panel-raised"}`,
        attrs: { id: `src-${messageId}-${c.n}` },
      },
      [
        pixelIcon("book", { size: 14 }),
        el("span", { class: "font-mono text-lime shrink-0", text: `{${c.n}}` }),
        el("span", { text: sourceTitle(c) }),
        consulted ? el("span", { class: "label ml-auto shrink-0", text: "consulted" }) : null,
      ],
    );
    list.appendChild(row);
  }
  wrap.appendChild(list);
  wrap.addEventListener("click", (event) => {
    const chip = (event.target as HTMLElement).closest<HTMLElement>(".cite");
    if (!chip) return;
    const target = document.getElementById(`src-${messageId}-${chip.dataset.n}`);
    if (!target) return;
    target.scrollIntoView({ block: "nearest" });
    target.classList.add("bg-lime/20");
    window.setTimeout(() => target.classList.remove("bg-lime/20"), 1200);
  });
}

function rateButton(kind: "up" | "down", label: string): HTMLButtonElement {
  const button = el("button", {
    class: "btn btn-quiet btn-sm btn-icon aria-pressed:border-lime aria-pressed:text-lime",
    attrs: { type: "button", "aria-label": label, "aria-pressed": "false" },
  });
  button.appendChild(pixelIcon(kind, { size: 14 }));
  return button;
}

function renderFeedback(
  wrap: HTMLElement,
  messageId: string,
  current: StoredMessage["feedback"],
  announce: (text: string) => void,
): void {
  const up = rateButton("up", "Helpful");
  const down = rateButton("down", "Not helpful");
  const sync = (rating: number): void => {
    up.setAttribute("aria-pressed", String(rating === 1));
    down.setAttribute("aria-pressed", String(rating === -1));
  };
  sync(current?.rating ?? 0);

  const send = async (rating: 1 | -1, reason: string | null): Promise<void> => {
    const response = await postJson(`/api/messages/${messageId}/feedback`, { rating, reason });
    if (response.ok) {
      sync(rating);
      announce("Thanks for the feedback");
    } else {
      announce("Could not save feedback");
    }
  };

  up.addEventListener("click", () => void send(1, null));
  down.addEventListener("click", () => {
    if (wrap.querySelector(".reason-form")) return;
    void send(-1, null);
    const field = el("input", {
      class: "field",
      attrs: { type: "text", maxlength: "500", placeholder: "What was wrong? (optional)", "aria-label": "Reason" },
    });
    const submit = el("button", { class: "btn btn-sm", text: "{Send}", attrs: { type: "submit" } });
    const form = el("form", { class: "reason-form flex gap-2" }, [field, submit]);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void send(-1, field.value.trim() || null).then(() => form.remove());
    });
    wrap.appendChild(form);
    field.focus();
  });
  append(wrap, [el("div", { class: "flex items-center gap-2" }, [up, down])]);
}

export interface FinishedMessage {
  id: string;
  content: string;
  citations: SourceCitation[];
  status?: StoredMessage["status"];
  feedback?: StoredMessage["feedback"];
}

/** Final rendering of an answer: Markdown with citation chips, sources, feedback. */
export function finishBotMessage(
  parts: BotMessage,
  data: FinishedMessage,
  announce: (text: string) => void,
): void {
  parts.bubble.classList.remove("streaming");
  renderMarkdown(parts.bubble, data.content, data.citations);
  renderSources(parts.wrap, data.id, data.citations);
  const status = data.status ?? "complete";
  if (status !== "complete") {
    parts.wrap.appendChild(
      el("div", { class: "label", text: status === "cancelled" ? "Stopped before finishing" : "Interrupted" }),
    );
    return;
  }
  renderFeedback(parts.wrap, data.id, data.feedback ?? null, announce);
}
