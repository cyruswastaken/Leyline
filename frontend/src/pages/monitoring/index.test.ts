import { describe, expect, it } from "vitest";
import { eventTone, formatDetails, percent, type SecurityEvent } from "./index";

const event = (over: Partial<SecurityEvent>): SecurityEvent => ({
  timestamp: "2026-11-10T06:00:00+00:00",
  event_type: "X",
  user_id: "u",
  status_code: 200,
  reason: "r",
  ...over,
});

describe("eventTone", () => {
  it.each([
    ["NORMAL_REQUEST", "indexed"],
    ["SECURITY_RULE_TRIGGERED", "failed"],
    ["PROMPT_LEAK_BLOCKED", "failed"],
    ["RATE_LIMIT_EXCEEDED", "open"],
    ["RETRIEVAL", "in_progress"],
    ["SOMETHING_NEW", "closed"],
  ])("%s is %s", (type, tone) => {
    expect(eventTone(type)).toBe(tone);
  });
});

describe("formatDetails", () => {
  it("summarises a retrieval", () => {
    const text = formatDetails(
      event({
        event_type: "RETRIEVAL",
        details: { selected_source: "VPN guide", returned_count: 2, candidate_count: 5, top_score: 0.03, latency_ms: 18 },
      }),
    );
    expect(text).toBe("source: VPN guide \u00b7 results: 2/5 \u00b7 score: 0.03 \u00b7 18 ms");
  });

  it("handles a retrieval with nothing selected", () => {
    expect(formatDetails(event({ event_type: "RETRIEVAL", details: { selected_source: null } }))).toContain("source: none");
  });

  it("prints other details as key: value pairs and stays plain text", () => {
    const text = formatDetails(event({ details: { score: 4, blocked: true, nested: { a: 1 }, html: "<b>x</b>" } }));
    expect(text).toBe('score: 4 \u00b7 blocked: true \u00b7 nested: {"a":1} \u00b7 html: <b>x</b>');
  });

  it("is empty without details", () => {
    expect(formatDetails(event({}))).toBe("");
  });
});

describe("percent", () => {
  it("scales to the peak and never exceeds 100 or divides by zero", () => {
    expect(percent(5, 10)).toBe(50);
    expect(percent(20, 10)).toBe(100);
    expect(percent(3, 0)).toBe(0);
  });
});
