// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(new URL("./main.css", import.meta.url), "utf-8");

function token(name: string): string {
  const match = css.match(new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match?.[1]) throw new Error(`token --color-${name} not found`);
  return match[1];
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r = 0, g = 0, b = 0] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

// Text pairs used in the interface. WCAG AA needs 4.5:1 for body text.
const AA = 4.5;
const pairs: [string, string][] = [
  ["text", "bg"],
  ["text", "panel"],
  ["text", "panel-raised"],
  ["text", "bg-deep"],
  ["muted", "bg"],
  ["muted", "panel"],
  ["muted", "panel-raised"],
  ["muted", "bg-deep"],
  ["lime", "bg"],
  ["lime", "panel"],
  ["lime", "bg-deep"],
  ["lime-ink", "lime"],
  ["lime-ink", "lime-dim"],
  ["info", "panel"],
  ["info", "bg-deep"],
  ["warn", "panel"],
  ["warn", "bg-deep"],
  ["danger", "panel"],
  ["danger", "bg-deep"],
  ["danger", "bg"],
];

describe("design tokens", () => {
  it.each(pairs)("%s on %s meets WCAG AA", (foreground, background) => {
    expect(contrast(token(foreground), token(background))).toBeGreaterThanOrEqual(AA);
  });

  it("keeps the borders visible against the panels (non-text, 1.2:1 minimum)", () => {
    expect(contrast(token("line-strong"), token("bg-deep"))).toBeGreaterThanOrEqual(1.2);
  });

  it("measures contrast correctly", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 0);
    expect(contrast("#777777", "#777777")).toBeCloseTo(1, 5);
  });
});
