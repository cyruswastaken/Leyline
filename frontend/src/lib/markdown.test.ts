import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";

function render(source: string, citations: { n: number }[] | null = null): HTMLElement {
  const container = document.createElement("div");
  renderMarkdown(container, source, citations);
  return container;
}

describe("renderMarkdown", () => {
  it("renders paragraphs, joining wrapped lines", () => {
    const out = render("first line\nsecond line\n\nnext paragraph");
    expect([...out.querySelectorAll("p")].map((p) => p.textContent)).toEqual([
      "first line second line",
      "next paragraph",
    ]);
  });

  it("renders ordered and bullet lists", () => {
    const out = render("1. one\n2. two\n\n- a\n- b");
    expect(out.querySelector("ol")?.children).toHaveLength(2);
    expect(out.querySelector("ul")?.textContent).toBe("ab");
  });

  it("renders bold, inline code and fenced code as elements", () => {
    const out = render("use **bold** and `code`\n\n```\nline 1\nline 2\n```");
    expect(out.querySelector("strong")?.textContent).toBe("bold");
    expect(out.querySelector("p code")?.textContent).toBe("code");
    expect(out.querySelector("pre code")?.textContent).toBe("line 1\nline 2");
  });

  it("turns only known citation numbers into chips", () => {
    const out = render("see [1] and [7]", [{ n: 1 }, { n: 2 }]);
    const chips = out.querySelectorAll<HTMLButtonElement>("button.cite");
    expect(chips).toHaveLength(1);
    expect(chips[0]?.dataset.n).toBe("1");
    expect(chips[0]?.getAttribute("aria-label")).toBe("Go to source 1");
    expect(out.textContent).toContain("[7]");
  });

  it("leaves citation markers as plain text while streaming (no citations yet)", () => {
    const out = render("see [1]", null);
    expect(out.querySelector("button")).toBeNull();
    expect(out.textContent).toBe("see [1]");
  });

  it("never interprets HTML from the model", () => {
    const attack =
      '<img src=x onerror="alert(1)"><script>alert(2)</script> **<b>x</b>** `<i>y</i>`\n\n```\n<svg onload=alert(3)>\n```';
    const out = render(attack, [{ n: 1 }]);
    expect(out.querySelector("img, script, svg, b, i")).toBeNull();
    expect(out.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(out.textContent).toContain("<svg onload=alert(3)>");
  });

  it("replaces previous content and tolerates empty input", () => {
    const container = document.createElement("div");
    renderMarkdown(container, "old");
    renderMarkdown(container, "");
    expect(container.childNodes).toHaveLength(0);
  });
});
