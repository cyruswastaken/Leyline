import type { SourceCitation } from "./sse";

export interface ConversationSummary {
  id: string;
  title: string;
  updated_at: string;
}

export interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "complete" | "error" | "cancelled";
  citations: SourceCitation[];
  feedback: { rating: 1 | -1; reason: string | null } | null;
}

export interface StoredConversation {
  id: string;
  title: string;
  messages: StoredMessage[];
}

export interface TicketEvent {
  kind: "created" | "comment" | "status_changed" | "assigned";
  actor_name: string;
  body: string;
  created_at: string;
  by_requester: boolean;
}

export interface Ticket {
  id: string;
  subject: string;
  status: "open" | "in_progress" | "resolved" | "closed";
  requester_name: string;
  assignee_name: string | null;
  created_at: string;
  updated_at: string;
  description: string;
  events: TicketEvent[];
}

export const TICKET_STATUSES = ["open", "in_progress", "resolved", "closed"] as const;
