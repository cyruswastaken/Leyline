import { api, errorMessage } from "../../lib/api";
import { byId, clear, el, formatTime, statusLabel } from "../../lib/dom";
import { pixelIcon } from "../../lib/pixel-icon";
import { TICKET_STATUSES, type Ticket } from "../../lib/types";
import { initTicketDialog } from "../ticket-dialog";

type Summary = Pick<Ticket, "id" | "subject" | "status" | "requester_name" | "assignee_name" | "updated_at">;

export function statusBadge(status: string): HTMLElement {
  return el("span", { class: `badge badge-${status}`, text: statusLabel(status) });
}

export function initTickets(): void {
  const isAgent = byId("main").dataset.agent === "true";
  const mine = byId("mine");
  const queueSection = byId("queue-section");
  const queue = byId("queue");
  const filter = byId<HTMLSelectElement>("queue-filter");
  const detail = byId("detail");
  const notice = byId("notice");
  let currentId: string | null = null;

  const say = (text: string): void => {
    notice.textContent = text;
  };

  function renderList(target: HTMLElement, tickets: Summary[], showRequester: boolean): void {
    clear(target);
    if (tickets.length === 0) {
      target.appendChild(el("li", { class: "px-3 py-4 text-sm text-muted", text: "Nothing here." }));
      return;
    }
    for (const ticket of tickets) {
      const active = ticket.id === currentId;
      const button = el(
        "button",
        {
          class: `block w-full border-l-2 px-3 py-3 text-left hover:bg-white/5 ${active ? "border-lime bg-white/5" : "border-transparent"}`,
          attrs: { type: "button", ...(active ? { "aria-current": "true" } : {}) },
        },
        [
          el("div", { class: "flex items-start justify-between gap-2" }, [
            el("span", { class: "font-medium", text: ticket.subject }),
            statusBadge(ticket.status),
          ]),
          el("div", {
            class: "label mt-1 normal-case tracking-normal",
            text: [showRequester ? ticket.requester_name : null, formatTime(ticket.updated_at), ticket.assignee_name]
              .filter(Boolean)
              .join(" · "),
          }),
        ],
      );
      button.addEventListener("click", () => void openTicket(ticket.id));
      target.appendChild(el("li", {}, [button]));
    }
  }

  async function refresh(): Promise<void> {
    const response = await api("/api/tickets");
    if (response.ok) renderList(mine, ((await response.json()) as { tickets: Summary[] }).tickets, false);
    if (isAgent) {
      queueSection.hidden = false;
      const status = filter.value;
      const listed = await api(`/api/agent/tickets${status ? `?status=${status}` : ""}`);
      if (listed.ok) renderList(queue, ((await listed.json()) as { tickets: Summary[] }).tickets, true);
    }
  }

  async function act(path: string, body: unknown): Promise<void> {
    const response = await api(path, { method: "POST", body: JSON.stringify(body) });
    if (!response.ok) {
      say(await errorMessage(response, "That did not work."));
      return;
    }
    say("");
    if (currentId) await openTicket(currentId);
    await refresh();
  }

  function timeline(ticket: Ticket): HTMLElement {
    const list = el("ol", { class: "mt-6 grid gap-3 list-none p-0", attrs: { "aria-label": "History" } });
    for (const event of ticket.events) {
      if (event.kind === "created") continue;
      if (event.kind === "comment") {
        list.appendChild(
          el("li", { class: `border px-4 py-3 ${event.by_requester ? "border-line bg-panel" : "border-lime/50 bg-lime/5"}` }, [
            el("div", { class: "label mb-2", text: `${event.by_requester ? "Requester" : "Support"} · ${event.actor_name} · ${formatTime(event.created_at)}` }),
            el("div", { class: "whitespace-pre-wrap overflow-wrap-anywhere", text: event.body }),
          ]),
        );
      } else {
        list.appendChild(
          el("li", { class: "label normal-case tracking-normal", text: `${event.actor_name}: ${event.body} · ${formatTime(event.created_at)}` }),
        );
      }
    }
    return list;
  }

  function controls(ticket: Ticket): HTMLElement {
    const row = el("div", { class: "mt-6 flex flex-wrap items-center gap-2" });
    const button = (text: string, handler: () => void, quiet = true): HTMLButtonElement => {
      const node = el("button", { class: `btn btn-sm ${quiet ? "btn-quiet" : ""}`, text, attrs: { type: "button" } });
      node.addEventListener("click", handler);
      row.appendChild(node);
      return node;
    };
    if (isAgent) {
      button("{Assign to me}", () => void act(`/api/agent/tickets/${ticket.id}/assign`, { assign: true }), false);
      if (ticket.assignee_name) button("{Unassign}", () => void act(`/api/agent/tickets/${ticket.id}/assign`, { assign: false }));
      const select = el("select", { class: "field w-auto", attrs: { "aria-label": "Status" } });
      for (const status of TICKET_STATUSES) {
        const option = el("option", { text: statusLabel(status), attrs: { value: status } });
        option.selected = status === ticket.status;
        select.appendChild(option);
      }
      select.addEventListener("change", () => void act(`/api/tickets/${ticket.id}/status`, { status: select.value }));
      row.appendChild(select);
    } else {
      if (ticket.status === "resolved") button("{Reopen}", () => void act(`/api/tickets/${ticket.id}/status`, { status: "open" }));
      if (ticket.status !== "closed") button("{Close ticket}", () => void act(`/api/tickets/${ticket.id}/status`, { status: "closed" }));
    }
    return row;
  }

  async function openTicket(id: string): Promise<void> {
    const response = await api(`/api/tickets/${id}`);
    clear(detail);
    if (!response.ok) {
      detail.appendChild(el("p", { class: "text-muted", text: "That ticket is not available." }));
      return;
    }
    const ticket = (await response.json()) as Ticket;
    currentId = ticket.id;
    detail.appendChild(
      el("div", { class: "flex flex-wrap items-start justify-between gap-3" }, [
        el("h2", { class: "text-xl font-semibold", text: ticket.subject }),
        statusBadge(ticket.status),
      ]),
    );
    detail.appendChild(
      el("p", {
        class: "label mt-2 normal-case tracking-normal",
        text: `Opened by ${ticket.requester_name} on ${formatTime(ticket.created_at)} · ${ticket.assignee_name ? `Assigned to ${ticket.assignee_name}` : "Unassigned"}`,
      }),
    );
    detail.appendChild(el("div", { class: "mt-4 whitespace-pre-wrap border border-line bg-bg-deep p-4 overflow-wrap-anywhere", text: ticket.description }));
    detail.appendChild(timeline(ticket));

    if (ticket.status !== "closed") {
      const area = el("textarea", { class: "field", attrs: { rows: "3", maxlength: "2000", placeholder: "Add a comment", "aria-label": "Comment" } });
      const send = el("button", { class: "btn btn-primary self-end", text: "{Comment}", attrs: { type: "submit" } });
      const form = el("form", { class: "mt-6 flex flex-col gap-2" }, [area, send]);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (area.value.trim()) void act(`/api/tickets/${ticket.id}/comments`, { body: area.value });
      });
      detail.appendChild(form);
    }
    detail.appendChild(controls(ticket));
    void refresh();
  }

  filter.addEventListener("change", () => void refresh());
  initTicketDialog("new-ticket");

  const wanted = new URLSearchParams(window.location.search).get("id");
  void refresh().then(() => {
    if (wanted) return openTicket(wanted);
    detail.appendChild(el("div", { class: "flex flex-col items-center gap-3 py-16 text-center text-muted" }, [
      pixelIcon("ticket", { size: 32 }),
      el("p", { text: "Select a ticket to see its history." }),
    ]));
    return undefined;
  });
}
