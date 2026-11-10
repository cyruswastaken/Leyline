import { api } from "../../lib/api";
import { byId, clear, el, formatTime } from "../../lib/dom";

export interface SecurityEvent {
  timestamp: string;
  event_type: string;
  user_id: string;
  status_code: number;
  reason: string;
  details?: Record<string, unknown>;
}

export interface Summary {
  total_events: number;
  by_type: Record<string, number>;
  [key: string]: unknown;
}

type Tone = "indexed" | "open" | "failed" | "closed" | "in_progress";

/** Colour by what the event means for an auditor, using the shared badge tones. */
export function eventTone(type: string): Tone {
  switch (type) {
    case "NORMAL_REQUEST":
    case "TICKET_CREATED":
      return "indexed";
    case "SECURITY_RULE_TRIGGERED":
    case "ACCESS_DENIED":
    case "CSRF_REJECTED":
    case "PROMPT_LEAK_BLOCKED":
    case "MODEL_ERROR":
    case "EVENT_STORE_FAILED":
      return "failed";
    case "RATE_LIMIT_EXCEEDED":
    case "INJECTION_SIGNAL":
    case "NO_RESULTS_FOUND":
      return "open";
    case "RETRIEVAL":
      return "in_progress";
    default:
      return "closed";
  }
}

/** One line of plain text for an event's details (always rendered with textContent). */
export function formatDetails(event: SecurityEvent): string {
  const d = event.details;
  if (!d) return "";
  if (event.event_type === "RETRIEVAL") {
    return [
      `source: ${String(d.selected_source ?? "none")}`,
      `results: ${String(d.returned_count ?? 0)}/${String(d.candidate_count ?? 0)}`,
      `score: ${String(d.top_score ?? 0)}`,
      `${String(d.latency_ms ?? 0)} ms`,
    ].join(" \u00b7 ");
  }
  return Object.entries(d)
    .map(([key, value]) => `${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join(" \u00b7 ");
}

export function percent(value: number, total: number): number {
  return total > 0 ? Math.min(100, (value / total) * 100) : 0;
}

const HEADLINE: [string, string][] = [
  ["total_events", "Events"],
  ["normal_requests", "Answered"],
  ["retrieval_requests", "Retrievals"],
  ["no_results_found", "No results"],
  ["security_rules_triggered", "Blocked"],
  ["rate_limits_exceeded", "Rate limited"],
  ["model_errors", "Model errors"],
];

export function initMonitoring(): void {
  const cards = byId("cards");
  const bars = byId("bars");
  const body = byId("events-body");
  const typeFilter = byId<HTMLSelectElement>("type-filter");
  const limitFilter = byId<HTMLSelectElement>("limit-filter");
  const live = byId<HTMLInputElement>("live");
  const status = byId("status");
  let timer: number | undefined;
  let knownTypes = "";

  function renderSummary(summary: Summary): void {
    clear(cards);
    for (const [key, label] of HEADLINE) {
      cards.appendChild(
        el("div", { class: "card" }, [
          el("div", { class: "label", text: label }),
          el("div", { class: "display mt-2 text-3xl text-lime", text: String(summary[key] ?? 0) }),
        ]),
      );
    }

    clear(bars);
    const entries = Object.entries(summary.by_type).sort((a, b) => b[1] - a[1]);
    const peak = entries[0]?.[1] ?? 0;
    for (const [type, count] of entries) {
      const fill = el("div", { class: "h-2 bg-lime" });
      fill.style.width = `${percent(count, peak)}%`;
      bars.appendChild(
        el("li", { class: "grid grid-cols-[14rem_1fr_4rem] items-center gap-3 py-1 text-sm" }, [
          el("span", { class: "label truncate normal-case tracking-normal", text: type }),
          el("div", { class: "bg-bg-deep" }, [fill]),
          el("span", { class: "text-right font-mono", text: String(count) }),
        ]),
      );
    }

    // Keep the filter's choices in step with the types that exist.
    const types = entries.map(([type]) => type).sort();
    const signature = types.join("|");
    if (signature !== knownTypes) {
      knownTypes = signature;
      const chosen = typeFilter.value;
      clear(typeFilter);
      typeFilter.appendChild(el("option", { text: "All types", attrs: { value: "" } }));
      for (const type of types) typeFilter.appendChild(el("option", { text: type, attrs: { value: type } }));
      typeFilter.value = types.includes(chosen) ? chosen : "";
    }
  }

  function renderEvents(events: SecurityEvent[]): void {
    clear(body);
    if (events.length === 0) {
      body.appendChild(el("tr", {}, [el("td", { class: "text-muted", text: "No events.", attrs: { colspan: "6" } })]));
      return;
    }
    for (const event of events) {
      body.appendChild(
        el("tr", {}, [
          el("td", { class: "whitespace-nowrap text-muted", text: formatTime(event.timestamp) }),
          el("td", {}, [el("span", { class: `badge badge-${eventTone(event.event_type)}`, text: event.event_type })]),
          el("td", { class: "max-w-40 truncate font-mono text-xs", text: event.user_id, attrs: { title: event.user_id } }),
          el("td", { class: "font-mono", text: String(event.status_code) }),
          el("td", { class: "overflow-wrap-anywhere", text: event.reason }),
          el("td", { class: "text-sm text-muted overflow-wrap-anywhere", text: formatDetails(event) }),
        ]),
      );
    }
  }

  async function load(): Promise<void> {
    window.clearTimeout(timer);
    try {
      const query = new URLSearchParams({ limit: limitFilter.value });
      if (typeFilter.value) query.set("type", typeFilter.value);
      const [summary, events] = await Promise.all([api("/monitoring/summary"), api(`/monitoring/events?${query}`)]);
      if (!summary.ok || !events.ok) throw new Error("request failed");
      renderSummary((await summary.json()) as Summary);
      renderEvents((await events.json()) as SecurityEvent[]);
      status.textContent = `Updated ${formatTime(new Date().toISOString())}`;
    } catch {
      status.textContent = "Could not load events.";
    }
    if (live.checked) timer = window.setTimeout(() => void load(), 5000);
  }

  typeFilter.addEventListener("change", () => void load());
  limitFilter.addEventListener("change", () => void load());
  live.addEventListener("change", () => void load());
  byId("refresh").addEventListener("click", () => void load());
  void load();
}
