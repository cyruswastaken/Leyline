/** Events sent by POST /api/chat/stream. */
export interface SourceCitation {
  n: number;
  title: string;
  heading?: string | null;
  available?: boolean;
  cited?: boolean;
  document_id?: string;
}

export type ChatEvent =
  | { type: "meta"; conversation_id: string }
  | { type: "delta"; text: string }
  | { type: "done"; message_id: string; content: string; citations: SourceCitation[] }
  | { type: "error"; reason: string; error: string };

/**
 * Parses a server-sent-event body into events. Frames are separated by a blank
 * line and may be split across network chunks at any byte, so incomplete frames
 * are buffered. Frames without a data line (comments, keep-alives) are skipped.
 */
export async function* readEvents(response: Response): AsyncGenerator<ChatEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let split: number;
      while ((split = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const event = parseFrame(frame);
        if (event) yield event;
      }
    }
  } finally {
    // Closing early (the user pressed Stop) cancels the request on the server.
    await reader.cancel().catch(() => undefined);
  }
}

export function parseFrame(frame: string): ChatEvent | null {
  const line = frame.split("\n").find((l) => l.startsWith("data: "));
  if (!line) return null;
  try {
    return JSON.parse(line.slice(6)) as ChatEvent;
  } catch {
    return null;
  }
}
