import { api, errorMessage } from "../../lib/api";
import { twoStep } from "../../lib/confirm";
import { byId, clear, el } from "../../lib/dom";
import { pixelIcon } from "../../lib/pixel-icon";

interface VersionInfo {
  id: string;
  version: number;
  status: string;
  filename: string;
  error?: string | null;
  chunk_count: number;
  job?: { attempts: number } | null;
}

export interface DocumentRow {
  id: string;
  title: string;
  groups: string[];
  latest_version: VersionInfo | null;
  current_version: VersionInfo | null;
  needs_reindex: boolean;
}

type Tone = "indexed" | "open" | "failed" | "closed";

/** What the status column says, and in which colour. */
export function statusOf(doc: DocumentRow): { label: string; tone: Tone } {
  const latest = doc.latest_version;
  if (!latest) return { label: "no file", tone: "closed" };
  if (latest.status === "failed") return { label: "failed", tone: "failed" };
  if (latest.status === "pending") {
    return { label: latest.job && latest.job.attempts > 0 ? "retrying" : "queued", tone: "open" };
  }
  if (latest.status === "processing") return { label: "indexing", tone: "open" };
  if (doc.current_version && doc.needs_reindex) return { label: "needs re-index", tone: "open" };
  if (doc.current_version) return { label: "indexed", tone: "indexed" };
  return { label: latest.status, tone: "closed" };
}

export function needsPolling(documents: DocumentRow[]): boolean {
  return documents.some((d) => d.latest_version?.status === "pending" || d.latest_version?.status === "processing");
}

export function parseGroups(text: string): string[] {
  return text
    .split(",")
    .map((g) => g.trim())
    .filter(Boolean);
}

const ACCEPT = ".pdf,.docx,.md,.txt";

export function initAdmin(): void {
  const tbody = byId("documents");
  const empty = byId("empty");
  const message = byId("message");
  const uploadForm = byId<HTMLFormElement>("upload-form");
  const fileInput = byId<HTMLInputElement>("file");
  const titleInput = byId<HTMLInputElement>("title");
  const groupsInput = byId<HTMLInputElement>("groups");
  const uploadButton = byId<HTMLButtonElement>("upload-button");
  const dropzone = byId("dropzone");
  const fileName = byId("file-name");
  let pollTimer: number | undefined;

  const say = (text: string, kind: "ok" | "error" | "" = ""): void => {
    message.textContent = text;
    message.className = `min-h-6 text-sm ${kind === "ok" ? "text-lime" : kind === "error" ? "text-danger" : "text-muted"}`;
  };

  /** Runs an admin call; failures become a visible message instead of an exception. */
  async function call(method: string, url: string, body?: BodyInit): Promise<Record<string, unknown> | null> {
    const response = await api(url, body === undefined ? { method } : { method, body });
    if (!response.ok) throw new Error(await errorMessage(response, `Request failed (${response.status})`));
    return response.status === 204 ? null : ((await response.json()) as Record<string, unknown>);
  }

  const attempt = async (work: () => Promise<void>): Promise<void> => {
    try {
      await work();
    } catch (error) {
      say(error instanceof Error ? error.message : "Request failed", "error");
    }
  };

  function row(doc: DocumentRow): HTMLTableRowElement {
    const latest = doc.latest_version;
    const live = doc.current_version;
    const status = statusOf(doc);

    const title = el("td", {}, [
      el("strong", { text: doc.title }),
      latest ? el("div", { class: "label mt-1 normal-case tracking-normal", text: latest.filename }) : null,
    ]);

    const groupInput = el("input", {
      class: "field",
      attrs: { type: "text", "aria-label": `Groups allowed to read ${doc.title}` },
    });
    groupInput.value = doc.groups.join(", ");
    const save = el("button", { class: "btn btn-quiet btn-sm", text: "{Save}", attrs: { type: "submit" } });
    const groupForm = el("form", { class: "flex gap-2" }, [groupInput, save]);
    groupForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void attempt(async () => {
        await call("PUT", `/api/admin/documents/${doc.id}/acl`, JSON.stringify({ groups: parseGroups(groupInput.value) }));
        say(`Access updated for “${doc.title}”.`, "ok");
        await refresh();
      });
    });
    const chips = el(
      "div",
      { class: "mt-2 flex flex-wrap gap-1" },
      doc.groups.length === 0
        ? [el("span", { class: "text-sm text-danger", text: "Nobody can read this document." })]
        : doc.groups.map((group) => el("span", { class: "chip", text: group })),
    );
    const readable = el("td", { class: "min-w-64" }, [groupForm, chips]);

    const state = el("td", {}, [
      el("span", { class: `badge badge-${status.tone}`, text: status.label }),
      latest?.error ? el("div", { class: "mt-1 text-sm text-danger", text: latest.error }) : null,
    ]);

    const version = el("td", {}, [
      el("div", { text: live ? `v${live.version} live` : "not live yet" }),
      live ? el("div", { class: "label mt-1", text: `${live.chunk_count} chunks` }) : null,
      latest && (!live || latest.id !== live.id)
        ? el("div", { class: "label mt-1", text: `v${latest.version} ${latest.status}` })
        : null,
    ]);

    const hiddenFile = el("input", { attrs: { type: "file", accept: ACCEPT, hidden: "" } });
    hiddenFile.addEventListener("change", () => {
      const file = hiddenFile.files?.[0];
      if (!file) return;
      void attempt(async () => {
        const data = new FormData();
        data.append("file", file);
        const result = await call("POST", `/api/admin/documents/${doc.id}/versions`, data);
        say(result?.duplicate ? "That file is already the latest version." : "New version queued.", "ok");
        await refresh();
      }).finally(() => {
        hiddenFile.value = "";
      });
    });
    const newVersion = el("button", { class: "btn btn-quiet btn-sm", attrs: { type: "button" } }, [
      pixelIcon("upload", { size: 12 }),
      "New version",
    ]);
    newVersion.addEventListener("click", () => hiddenFile.click());

    const reindex = el("button", { class: "btn btn-quiet btn-sm", attrs: { type: "button" } }, [
      pixelIcon("refresh", { size: 12 }),
      "Re-index",
    ]);
    reindex.addEventListener("click", () =>
      void attempt(async () => {
        const result = await call("POST", `/api/admin/documents/${doc.id}/reindex`);
        say(result?.queued ? "Re-index queued." : "Already queued.", "ok");
        await refresh();
      }),
    );

    const remove = el("button", { class: "btn btn-danger btn-sm", attrs: { type: "button" } }, [
      pixelIcon("trash", { size: 12 }),
      "Delete",
    ]);
    twoStep(remove, () =>
      attempt(async () => {
        await call("DELETE", `/api/admin/documents/${doc.id}`);
        say(`Deleted “${doc.title}”. It is no longer searchable.`, "ok");
        await refresh();
      }),
    );

    const actions = el("td", {}, [el("div", { class: "flex flex-wrap gap-2" }, [newVersion, reindex, remove, hiddenFile])]);
    return el("tr", {}, [title, readable, state, version, actions]);
  }

  function render(documents: DocumentRow[]): void {
    clear(tbody);
    empty.hidden = documents.length > 0;
    for (const doc of documents) tbody.appendChild(row(doc));
  }

  async function refresh(): Promise<void> {
    window.clearTimeout(pollTimer);
    await attempt(async () => {
      const data = (await call("GET", "/api/admin/documents")) as { documents: DocumentRow[] } | null;
      const documents = data?.documents ?? [];
      render(documents);
      if (needsPolling(documents)) pollTimer = window.setTimeout(() => void refresh(), 3000);
    });
  }

  // --- the drop zone: a labelled file input that also accepts a dropped file ------------
  const showChosen = (): void => {
    fileName.textContent = fileInput.files?.[0]?.name ?? "No file chosen";
  };
  fileInput.addEventListener("change", () => {
    showChosen();
    const file = fileInput.files?.[0];
    if (file && !titleInput.value) titleInput.value = file.name.replace(/\.[^.]+$/, "");
  });
  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.dataset.active = "true";
    });
  }
  for (const type of ["dragleave", "drop"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      delete dropzone.dataset.active;
    });
  }
  dropzone.addEventListener("drop", (event) => {
    const files = (event as DragEvent).dataTransfer?.files;
    if (files && files.length > 0) {
      fileInput.files = files;
      fileInput.dispatchEvent(new Event("change"));
    }
  });

  uploadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const file = fileInput.files?.[0];
    if (!file) return;
    const data = new FormData();
    data.append("file", file);
    data.append("title", titleInput.value);
    data.append("groups", groupsInput.value);
    uploadButton.disabled = true;
    say("Uploading…");
    void attempt(async () => {
      await call("POST", "/api/admin/documents", data);
      say("Uploaded. Indexing in the background.", "ok");
      uploadForm.reset();
      showChosen();
      await refresh();
    }).finally(() => {
      uploadButton.disabled = false;
    });
  });

  showChosen();
  void refresh();
}
