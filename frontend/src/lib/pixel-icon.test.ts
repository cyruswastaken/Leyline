import { describe, expect, it } from "vitest";
import { ICONS, pixelIcon, type IconName } from "./pixel-icon";

describe("pixel icons", () => {
  it.each(Object.keys(ICONS) as IconName[])("%s is an 8x8 bitmap with some pixels", (name) => {
    const rows = ICONS[name];
    expect(rows).toHaveLength(8);
    for (const row of rows) expect(row).toMatch(/^[.#]{8}$/);
    expect(rows.join("")).toContain("#");
  });

  it("draws one rectangle per horizontal run of pixels", () => {
    const svg = pixelIcon("plus", { size: 24 });
    expect(svg.getAttribute("width")).toBe("24");
    expect(svg.querySelectorAll("rect")).toHaveLength(8); // one run per row
    expect(svg.getAttribute("shape-rendering")).toBe("crispEdges");
    expect(svg.getAttribute("fill")).toBe("currentColor");
  });

  it("is hidden from assistive technology unless it has a label", () => {
    expect(pixelIcon("chat").getAttribute("aria-hidden")).toBe("true");
    const labelled = pixelIcon("chat", { label: "Chat" });
    expect(labelled.getAttribute("aria-label")).toBe("Chat");
    expect(labelled.getAttribute("role")).toBe("img");
    expect(labelled.hasAttribute("aria-hidden")).toBe(false);
  });
});
