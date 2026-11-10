import { describe, expect, it } from "vitest";
import { PALETTE, SHADE_LEVELS, noise3, renderGlobe } from "./globe";

const SIZE = 48;

function pixel(data: Uint8ClampedArray, x: number, y: number): [number, number, number, number] {
  const i = (y * SIZE + x) * 4;
  return [data[i] ?? 0, data[i + 1] ?? 0, data[i + 2] ?? 0, data[i + 3] ?? 0];
}

describe("renderGlobe", () => {
  it("is deterministic", () => {
    expect(renderGlobe(SIZE, 1.2)).toEqual(renderGlobe(SIZE, 1.2));
  });

  it("draws a disc: opaque in the middle, transparent in the corners", () => {
    const data = renderGlobe(SIZE, 0.5);
    expect(pixel(data, SIZE / 2, SIZE / 2)[3]).toBe(255);
    for (const [x, y] of [
      [0, 0],
      [SIZE - 1, 0],
      [0, SIZE - 1],
      [SIZE - 1, SIZE - 1],
    ] as const) {
      expect(pixel(data, x, y)[3]).toBe(0);
    }
  });

  it("turns: a different angle shows different pixels", () => {
    expect(renderGlobe(SIZE, 0)).not.toEqual(renderGlobe(SIZE, 1.5));
  });

  it("uses only the palette (shaded in discrete bands) so it stays pixel art", () => {
    const allowed = new Set<string>();
    for (const colour of Object.values(PALETTE)) {
      for (const level of SHADE_LEVELS) {
        allowed.add(colour.map((c) => Math.min(255, Math.round(c * level))).join(","));
      }
    }
    const data = renderGlobe(SIZE, 0.9);
    const used = new Set<string>();
    for (let y = 0; y < SIZE; y++) {
      for (let x = 0; x < SIZE; x++) {
        const [r, g, b, a] = pixel(data, x, y);
        if (a === 255) used.add(`${r},${g},${b}`);
      }
    }
    expect(used.size).toBeGreaterThan(6); // oceans, land and clouds all appear
    for (const colour of used) expect(allowed.has(colour)).toBe(true);
  });

  it("has a cream rim", () => {
    const data = renderGlobe(SIZE, 0.2);
    const [r, g, b] = pixel(data, SIZE / 2, 1);
    expect([r, g, b]).toEqual([...PALETTE.rim]);
  });
});

describe("noise3", () => {
  it("stays within 0..1 and is smooth", () => {
    for (let i = 0; i < 200; i++) {
      const value = noise3(i * 0.37, i * 0.11, i * 0.73, 3);
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThanOrEqual(1);
    }
    expect(Math.abs(noise3(1.5, 2.5, 3.5, 1) - noise3(1.5001, 2.5, 3.5, 1))).toBeLessThan(0.01);
  });
});
