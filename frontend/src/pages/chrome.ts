import { csrfToken } from "../lib/api";

/** Behaviour shared by every signed-in page: the sign-out button. */
export function initChrome(): void {
  const button = document.getElementById("signout-button");
  button?.addEventListener("click", async () => {
    try {
      const response = await fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken() },
      });
      const data = (await response.json()) as { redirect?: string };
      window.location.assign(data.redirect ?? "/");
    } catch {
      window.location.assign("/");
    }
  });
}
