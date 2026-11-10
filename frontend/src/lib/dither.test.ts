import { describe, expect, it } from "vitest";
import { BAYER_8, RAMP, quantise, renderDither, threshold } from "./dither";

describe("the Bayer matrix", () => {
  it("holds every value 0..63 exactly once", () => {
    const values = BAYER_8.flat().sort((a, b) => a - b);
    expect(values).toEqual(Array.from({ length: 64 }, (_, i) => i));
  });

  it("gives thresholds strictly inside 0..1 that repeat every 8 pixels", () => {
    for (let y = 0; y < 16; y++) {
      for (let x = 0; x < 16; x++) {
        const t = threshold(x, y);
        expect(t).toBeGreaterThan(0);
        expect(t).toBeLessThan(1);
        expect(t).toBe(threshold(x + 8, y + 8));
      }
    }
  });
});

describe("quantise", () => {
  it("maps black to the first level and white to the last, whatever the pixel", () => {
    for (let i = 0; i < 64; i++) {
      expect(quantise(0, i, i * 3)).toBe(0);
      expect(quantise(1, i, i * 3)).toBe(RAMP.length - 1);
    }
  });

  it("mixes the two nearest levels in proportion to the value", () => {
    const top = RAMP.length - 1;
    const value = 1.5 / top; // halfway between level 1 and level 2
    let high = 0;
    for (let y = 0; y < 8; y++) for (let x = 0; x < 8; x++) high += quantise(value, x, y) === 2 ? 1 : 0;
    expect(high).toBe(32);
  });
});

describe("renderDither", () => {
  const W = 64;
  const H = 32;
  const colours = (data: Uint8ClampedArray): Set<string> => {
    const used = new Set<string>();
    for (let i = 0; i < data.length; i += 4) if (data[i + 3] === 255) used.add(`${data[i]},${data[i + 1]},${data[i + 2]}`);
    return used;
  };

  it("is deterministic", () => {
    expect(renderDither(W, H, "landscape", 3)).toEqual(renderDither(W, H, "landscape", 3));
  });

  it("uses only ramp colours, fully opaque, or is transparent", () => {
    const allowed = new Set(RAMP.slice(1).map((c) => c.join(",")));
    const data = renderDither(W, H);
    for (let i = 0; i < data.length; i += 4) expect([0, 255]).toContain(data[i + 3]);
    const used = colours(data);
    expect(used.size).toBeGreaterThanOrEqual(3);
    for (const colour of used) expect(allowed.has(colour)).toBe(true);
  });

  it("keeps the top of the landscape clear so text above it stays readable", () => {
    const data = renderDither(W, H);
    for (let x = 0; x < W; x++) expect(data[x * 4 + 3]).toBe(0); // the first row
  });

  it("drifts: a different offset shows different pixels", () => {
    expect(renderDither(W, H, "landscape", 0)).not.toEqual(renderDither(W, H, "landscape", 9));
  });

  it("fades from dots at the top to nothing at the bottom", () => {
    const data = renderDither(W, H, "fade");
    const row = (y: number): number => {
      let lit = 0;
      for (let x = 0; x < W; x++) lit += data[(y * W + x) * 4 + 3] === 255 ? 1 : 0;
      return lit;
    };
    expect(row(0)).toBeGreaterThan(row(H - 1));
    expect(row(H - 1)).toBe(0);
  });
});
