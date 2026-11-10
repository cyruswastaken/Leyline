import { append, clear, el } from "./dom";

/** A source the answer may cite; only these numbers become citation chips. */
export interface Citation {
  n: number;
}

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[\d{1,2}\])/g;
const ORDERED = /^\s*\d+[.)]\s+/;
const BULLET = /^\s*[-*]\s+/;

function inline(parent: HTMLElement, text: string, citations: Citation[] | null): void {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const token = match[0];
    const index = match.index ?? 0;
    if (index > last) parent.appendChild(document.createTextNode(text.slice(last, index)));
    if (token.startsWith("**")) {
      parent.appendChild(el("strong", { text: token.slice(2, -2) }));
    } else if (token.startsWith("`")) {
      parent.appendChild(el("code", { text: token.slice(1, -1) }));
    } else {
      const n = Number(token.slice(1, -1));
      if (citations?.some((c) => c.n === n)) {
        parent.appendChild(
          el("button", {
            class: "cite",
            text: String(n),
            attrs: { type: "button", "aria-label": `Go to source ${n}` },
            data: { n: String(n) },
          }),
        );
      } else {
        parent.appendChild(document.createTextNode(token));
      }
    }
    last = index + token.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

/**
 * Renders the small Markdown subset answers use (paragraphs, lists, fenced code,
 * bold, inline code and `[n]` citations) into `container`.
 *
 * Everything is built from DOM nodes and text; nothing is parsed as HTML, so
 * `<img onerror=...>` in a model answer is just visible text.
 */
export function renderMarkdown(
  container: HTMLElement,
  source: string,
  citations: Citation[] | null = null,
): void {
  clear(container);
  const lines = source.replace(/\r/g, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    if (line.trim() === "") {
      i++;
      continue;
    }
    if (line.startsWith("```")) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !(lines[i] ?? "").startsWith("```")) code.push(lines[i++] ?? "");
      i++;
      append(container, [el("pre", {}, [el("code", { text: code.join("\n") })])]);
      continue;
    }
    const ordered = ORDERED.test(line);
    if (ordered || BULLET.test(line)) {
      const marker = ordered ? ORDERED : BULLET;
      const list = el(ordered ? "ol" : "ul");
      while (i < lines.length && marker.test(lines[i] ?? "")) {
        const item = el("li");
        inline(item, (lines[i] ?? "").replace(marker, ""), citations);
        list.appendChild(item);
        i++;
      }
      container.appendChild(list);
      continue;
    }
    const paragraph: string[] = [];
    while (
      i < lines.length &&
      (lines[i] ?? "").trim() !== "" &&
      !(lines[i] ?? "").startsWith("```") &&
      !ORDERED.test(lines[i] ?? "") &&
      !BULLET.test(lines[i] ?? "")
    ) {
      paragraph.push(lines[i++] ?? "");
    }
    const p = el("p");
    inline(p, paragraph.join(" "), citations);
    container.appendChild(p);
  }
}
