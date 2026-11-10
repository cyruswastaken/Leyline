/** Thrown when the session has ended; the browser is already being redirected. */
export class SignedOutError extends Error {
  constructor() {
    super("signed out");
    this.name = "SignedOutError";
  }
}

export function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? "";
}

/** The page the user is on, so sign-in can bring them back to it. */
export function signInUrl(): string {
  return `/login?next=${encodeURIComponent(window.location.pathname)}`;
}

/**
 * fetch with the CSRF header and a JSON content type. A 401 means the session
 * expired: send the user to sign in again instead of showing a broken page.
 */
export async function api(path: string, init: RequestInit = {}): Promise<Response> {
  // Uploads must not carry a JSON content type: the browser adds the multipart one.
  const isForm = init.body instanceof FormData;
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      ...(isForm ? {} : { "Content-Type": "application/json" }),
      "X-CSRF-Token": csrfToken(),
      ...(init.headers as Record<string, string> | undefined),
    },
  });
  if (response.status === 401) {
    window.location.assign(signInUrl());
    throw new SignedOutError();
  }
  return response;
}

export function postJson(path: string, body: unknown, init: RequestInit = {}): Promise<Response> {
  return api(path, { ...init, method: "POST", body: JSON.stringify(body) });
}

/** The server's `{error}` message, or a generic one. */
export async function errorMessage(response: Response, fallback = "Something went wrong."): Promise<string> {
  const data = (await response.json().catch(() => ({}))) as { error?: unknown };
  return typeof data.error === "string" ? data.error : fallback;
}
