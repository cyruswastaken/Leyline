/**
 * Ordered dithering, the technique behind the reference's landscape: a smooth
 * brightness field is squeezed into a handful of flat colours, and an 8x8 Bayer
 * matrix decides, pixel by pixel, which of the two nearest colours to use. The
 * result looks like early-computer graphics and needs no image file.
 *
 * `renderDither` is a pure function (pixels in, pixels out) so it can be tested;
 * `startDither` draws it on a canvas, drifting slowly in whole-pixel steps unless
 * the user prefers reduced motion. The colours are the app's own tokens: the
 * darkest level is fully transparent so the page's grid shows through.
 */
import { noise3, type RGB } from "./globe.ts";

/** From nothing to full lime. Level 0 is transparent; the rest are opaque. */
export const RAMP: readonly RGB[] = [
  [11, 12, 11],
  [22, 38, 17],
  [40, 78, 24],
  [143, 204, 69],
  [182, 243, 106],
];

/** The 8x8 Bayer threshold matrix (values 0..63). */
export const BAYER_8: readonly (readonly number[])[] = [
  [0, 32, 8, 40, 2, 34, 10, 42],
  [48, 16, 56, 24, 50, 18, 58, 26],
  [12, 44, 4, 36, 14, 46, 6, 38],
  [60, 28, 52, 20, 62, 30, 54, 22],
  [3, 35, 11, 43, 1, 33, 9, 41],
  [51, 19, 59, 27, 49, 17, 57, 25],
  [15, 47, 7, 39, 13, 45, 5, 37],
  [63, 31, 55, 23, 61, 29, 53, 21],
];

export type DitherScene = "landscape" | "fade";

const clamp01 = (value: number): number => Math.min(1, Math.max(0, value));

/** Threshold for pixel (x, y), strictly between 0 and 1. */
export function threshold(x: number, y: number): number {
  const row = BAYER_8[((y % 8) + 8) % 8] ?? [];
  return ((row[((x % 8) + 8) % 8] ?? 0) + 0.5) / 64;
}

/** Which ramp level a brightness `value` (0..1) becomes at pixel (x, y). */
export function quantise(value: number, x: number, y: number): number {
  const top = RAMP.length - 1;
  return Math.min(top, Math.max(0, Math.floor(clamp01(value) * top + threshold(x, y))));
}

function landscape(x: number, y: number, height: number, drift: number, seed: number): number {
  const u = (x + drift) / 28;
  const ny = y / height;
  // a rolling ridge across the upper half of the band
  const ridge =
    0.4 + 0.12 * Math.sin(u * 0.9 + seed) + 0.07 * Math.sin(u * 2.3 + seed * 2) + 0.12 * (noise3(u * 0.6, 0.5, 0, seed) - 0.5);
  if (ny < ridge) {
    // the sky: a glow that gathers just above the horizon, nothing near the top
    return Math.pow(clamp01(ny / ridge), 3.2) * 0.5;
  }
  // the field: bright along the ridge and sinking into dark moss, with a few lime "flowers"
  const depth = clamp01((ny - ridge) / (1 - ridge));
  const grain = noise3((x + drift) / 9, y / 5, 1, seed + 5);
  const flower = noise3(Math.floor((x + drift) / 2) * 1.7, Math.floor(y / 2) * 1.7, 9, seed + 31) > 0.88 ? 0.35 : 0;
  const ridgeLight = Math.pow(1 - depth, 2.4) * 0.55;
  return clamp01(0.08 + grain * 0.22 + ridgeLight + flower * (1 - depth * 0.5));
}

function fade(y: number, height: number): number {
  return Math.pow(1 - y / height, 2.2) * 0.55;
}

/**
 * RGBA pixels (width x height) of a dithered scene. `drift` slides the scene
 * sideways in whole pixels. Pixels at ramp level 0 are fully transparent.
 */
export function renderDither(
  width: number,
  height: number,
  scene: DitherScene = "landscape",
  drift = 0,
  seed = 3,
): Uint8ClampedArray {
  const pixels = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const value = scene === "fade" ? fade(y, height) : landscape(x, y, height, drift, seed);
      const level = quantise(value, x, y);
      if (level === 0) continue; // stays transparent
      const [r, g, b] = RAMP[level] ?? RAMP[0] ?? [0, 0, 0];
      pixels.set([r, g, b, 255], (y * width + x) * 4);
    }
  }
  return pixels;
}

export interface DitherHandle {
  stop(): void;
}

/**
 * Fills `canvas` with a dithered scene, one logical pixel per `cell` CSS pixels
 * (set `image-rendering: pixelated` on the canvas). Redraws when the element is
 * resized, and drifts slowly unless the user prefers reduced motion.
 */
export function startDither(
  canvas: HTMLCanvasElement,
  options: { scene?: DitherScene; cell?: number; fps?: number } = {},
): DitherHandle {
  const scene = options.scene ?? "landscape";
  const cell = options.cell ?? 4;
  const fps = options.fps ?? 3;
  const context = canvas.getContext("2d");
  if (!context) return { stop: () => undefined };

  let drift = 0;
  const draw = (): void => {
    const width = Math.max(8, Math.ceil(canvas.clientWidth / cell));
    const height = Math.max(8, Math.ceil(canvas.clientHeight / cell));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const pixels = renderDither(width, height, scene, drift) as Uint8ClampedArray<ArrayBuffer>;
    context.putImageData(new ImageData(pixels, width, height), 0, 0);
  };
  draw();

  const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(draw);
  observer?.observe(canvas);

  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  let timer: number | undefined;
  if (!reduced && scene === "landscape") {
    timer = window.setInterval(() => {
      if (document.hidden) return;
      drift += 1;
      draw();
    }, 1000 / fps);
  }
  return {
    stop: () => {
      observer?.disconnect();
      if (timer !== undefined) window.clearInterval(timer);
    },
  };
}
