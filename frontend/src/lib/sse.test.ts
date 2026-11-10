import { describe, expect, it } from "vitest";
import { parseFrame, readEvents, type ChatEvent } from "./sse";

function responseFrom(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream);
}

async function collect(response: Response): Promise<ChatEvent[]> {
  const events: ChatEvent[] = [];
  for await (const event of readEvents(response)) events.push(event);
  return events;
}

const frame = (event: ChatEvent): string => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;

describe("readEvents", () => {
  it("parses a whole stream in order", async () => {
    const events = await collect(
      responseFrom([
        frame({ type: "meta", conversation_id: "c1" }) + frame({ type: "delta", text: "Hi " }),
        frame({ type: "done", message_id: "m1", content: "Hi there", citations: [] }),
      ]),
    );
    expect(events.map((e) => e.type)).toEqual(["meta", "delta", "done"]);
  });

  it("reassembles frames split at any byte", async () => {
    const text = frame({ type: "delta", text: "split é across chunks" }) + frame({ type: "meta", conversation_id: "c" });
    const bytes = new TextEncoder().encode(text);
    // one byte per chunk, including inside the multi-byte character
    const pieces = Array.from(bytes, (b) => Uint8Array.of(b));
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const piece of pieces) controller.enqueue(piece);
        controller.close();
      },
    });
    const events = await collect(new Response(stream));
    expect(events).toEqual([
      { type: "delta", text: "split é across chunks" },
      { type: "meta", conversation_id: "c" },
    ]);
  });

  it("skips comments, keep-alives and malformed frames", async () => {
    const events = await collect(
      responseFrom([": keep-alive\n\n", "event: delta\ndata: {not json\n\n", frame({ type: "delta", text: "ok" })]),
    );
    expect(events).toEqual([{ type: "delta", text: "ok" }]);
  });

  it("ignores a trailing incomplete frame", async () => {
    const events = await collect(responseFrom([frame({ type: "delta", text: "a" }), 'event: delta\ndata: {"type":"delta"']));
    expect(events).toHaveLength(1);
  });

  it("stops cleanly when the consumer breaks out early", async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new TextEncoder().encode(frame({ type: "delta", text: "x" })));
      },
      cancel() {
        cancelled = true;
      },
    });
    for await (const event of readEvents(new Response(stream))) {
      expect(event.type).toBe("delta");
      break;
    }
    expect(cancelled).toBe(true);
  });

  it("yields nothing for a response without a body", async () => {
    expect(await collect(new Response(null))).toEqual([]);
  });
});

describe("parseFrame", () => {
  it("returns null without a data line", () => {
    expect(parseFrame("event: delta")).toBeNull();
  });
});
