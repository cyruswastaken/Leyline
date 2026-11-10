import { describe, expect, it } from "vitest";
import { needsPolling, parseGroups, statusOf, type DocumentRow } from "./index";

const version = (status: string, extra: object = {}) => ({
  id: "v1",
  version: 1,
  status,
  filename: "a.md",
  chunk_count: 3,
  ...extra,
});

const doc = (over: Partial<DocumentRow>): DocumentRow => ({
  id: "d",
  title: "T",
  groups: [],
  latest_version: null,
  current_version: null,
  needs_reindex: false,
  ...over,
});

describe("statusOf", () => {
  it("covers every state a document can be in", () => {
    expect(statusOf(doc({}))).toEqual({ label: "no file", tone: "closed" });
    expect(statusOf(doc({ latest_version: version("failed") })).tone).toBe("failed");
    expect(statusOf(doc({ latest_version: version("pending") })).label).toBe("queued");
    expect(statusOf(doc({ latest_version: version("pending", { job: { attempts: 2 } }) })).label).toBe("retrying");
    expect(statusOf(doc({ latest_version: version("processing") })).label).toBe("indexing");
    const live = version("indexed");
    expect(statusOf(doc({ latest_version: live, current_version: live })).label).toBe("indexed");
    expect(statusOf(doc({ latest_version: live, current_version: live, needs_reindex: true })).label).toBe("needs re-index");
  });
});

describe("needsPolling", () => {
  it("polls only while something is queued or indexing", () => {
    expect(needsPolling([doc({ latest_version: version("pending") })])).toBe(true);
    expect(needsPolling([doc({ latest_version: version("processing") })])).toBe(true);
    expect(needsPolling([doc({ latest_version: version("indexed") }), doc({})])).toBe(false);
  });
});

describe("parseGroups", () => {
  it("splits on commas, trims and drops empties", () => {
    expect(parseGroups(" staff, hr ,, *")).toEqual(["staff", "hr", "*"]);
    expect(parseGroups("")).toEqual([]);
  });
});
