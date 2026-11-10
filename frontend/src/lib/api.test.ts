import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignedOutError, api, csrfToken, errorMessage, postJson } from "./api";

function setToken(value: string | null): void {
  document.head.replaceChildren();
  if (value === null) return;
  const meta = document.createElement("meta");
  meta.name = "csrf-token";
  meta.content = value;
  document.head.appendChild(meta);
}

beforeEach(() => setToken("tok-123"));

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("api", () => {
  it("sends the CSRF token and a JSON content type", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await postJson("/api/x", { a: 1 });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/x");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"a":1}');
    expect(init.credentials).toBe("same-origin");
    expect(init.headers).toMatchObject({ "X-CSRF-Token": "tok-123", "Content-Type": "application/json" });
  });

  it("leaves the content type to the browser for file uploads", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const form = new FormData();
    form.append("file", new Blob(["x"]), "a.txt");
    await api("/upload", { method: "POST", body: form });
    const headers = (fetchMock.mock.calls[0] as [string, RequestInit])[1].headers as Record<string, string>;
    expect(headers["Content-Type"]).toBeUndefined();
    expect(headers["X-CSRF-Token"]).toBe("tok-123");
  });

  it("lets callers add headers without losing the token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await api("/x", { headers: { "X-Extra": "1" } });
    expect((fetchMock.mock.calls[0] as [string, RequestInit])[1].headers).toMatchObject({
      "X-CSRF-Token": "tok-123",
      "X-Extra": "1",
    });
  });

  it("sends the user to sign in again on 401, then fails the call", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 401 })));
    const assign = vi.fn();
    vi.stubGlobal("location", { pathname: "/tickets", assign });
    await expect(api("/api/tickets")).rejects.toBeInstanceOf(SignedOutError);
    expect(assign).toHaveBeenCalledWith("/login?next=%2Ftickets");
  });

  it("passes other error statuses through for the caller to handle", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 429 })));
    expect((await api("/x")).status).toBe(429);
  });
});

describe("csrfToken", () => {
  it("is empty when the page has no token", () => {
    setToken(null);
    expect(csrfToken()).toBe("");
  });
});

describe("errorMessage", () => {
  it("prefers the server message and falls back otherwise", async () => {
    expect(await errorMessage(new Response('{"error":"Too many requests."}'))).toBe("Too many requests.");
    expect(await errorMessage(new Response("not json"), "fallback")).toBe("fallback");
    expect(await errorMessage(new Response('{"error":5}'), "fallback")).toBe("fallback");
  });
});
