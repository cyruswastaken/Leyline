/**
 * A procedural pixel-art planet, in the palette of the design reference
 * (ocean blues, leaf greens, a cream rim and clouds). It is drawn from seeded
 * noise, so there is no image to license or load and it always looks the same.
 *
 * `renderGlobe` is a pure function (pixels in, pixels out) so it can be tested;
 * `startGlobe` draws it on a canvas, rotating slowly in discrete steps (which
 * suits pixel art) unless the user prefers reduced motion.
 */

export type RGB = readonly [number, number, number];

export const PALETTE = {
  deepOcean: [28, 79, 140],
  ocean: [40, 118, 182],
  shallows: [88, 190, 205],
  beach: [233, 224, 184],
  grassDark: [43, 98, 48],
  grass: [76, 150, 62],
  grassLight: [122, 186, 70],
  rock: [120, 130, 110],
  ice: [236, 242, 238],
  cloud: [246, 244, 232],
  rim: [233, 220, 176],
} as const satisfies Record<string, RGB>;

/** Every colour the globe can produce (shading picks a band of one of these). */
export const SHADE_LEVELS = [0.55, 0.78, 1, 1.12] as const;

function hash(x: number, y: number, z: number, seed: number): number {
  let h = Math.imul(x, 374761393) ^ Math.imul(y, 668265263) ^ Math.imul(z, 2147483647) ^ Math.imul(seed, 1274126177);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

const smooth = (t: number): number => t * t * (3 - 2 * t);
const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/** Smooth 3D value noise in [0, 1]. */
export function noise3(x: number, y: number, z: number, seed: number): number {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const zi = Math.floor(z);
  const u = smooth(x - xi);
  const v = smooth(y - yi);
  const w = smooth(z - zi);
  const corner = (dx: number, dy: number, dz: number): number => hash(xi + dx, yi + dy, zi + dz, seed);
  return lerp(
    lerp(lerp(corner(0, 0, 0), corner(1, 0, 0), u), lerp(corner(0, 1, 0), corner(1, 1, 0), u), v),
    lerp(lerp(corner(0, 0, 1), corner(1, 0, 1), u), lerp(corner(0, 1, 1), corner(1, 1, 1), u), v),
    w,
  );
}

function fbm(x: number, y: number, z: number, seed: number): number {
  let total = 0;
  let amplitude = 0.5;
  let frequency = 1;
  for (let octave = 0; octave < 4; octave++) {
    total += amplitude * noise3(x * frequency, y * frequency, z * frequency, seed + octave * 17);
    amplitude /= 2;
    frequency *= 2;
  }
  return total;
}

function shade(color: RGB, level: number): RGB {
  return [
    Math.min(255, Math.round(color[0] * level)),
    Math.min(255, Math.round(color[1] * level)),
    Math.min(255, Math.round(color[2] * level)),
  ];
}

function surfaceColor(height: number, latitude: number, cloud: number): RGB {
  if (cloud > 0.64) return PALETTE.cloud;
  if (Math.abs(latitude) > 0.86 + (height - 0.5) * 0.12) return PALETTE.ice;
  if (height < 0.4) return PALETTE.deepOcean;
  if (height < 0.47) return PALETTE.ocean;
  if (height < 0.5) return PALETTE.shallows;
  if (height < 0.525) return PALETTE.beach;
  if (height < 0.6) return PALETTE.grassLight;
  if (height < 0.69) return PALETTE.grass;
  if (height < 0.77) return PALETTE.grassDark;
  return PALETTE.rock;
}

/**
 * RGBA pixels (size x size) of the planet turned by `angle` radians. Pixels
 * outside the disc are fully transparent.
 */
export function renderGlobe(size: number, angle: number, seed = 7): Uint8ClampedArray {
  const pixels = new Uint8ClampedArray(size * size * 4);
  const radius = size / 2 - 1;
  const centre = size / 2;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  // light from the upper left, towards the viewer
  const light = [-0.55, -0.6, 0.58] as const;

  for (let py = 0; py < size; py++) {
    for (let px = 0; px < size; px++) {
      const x = (px + 0.5 - centre) / radius;
      const y = (py + 0.5 - centre) / radius;
      const d2 = x * x + y * y;
      if (d2 > 1) continue;
      const z = Math.sqrt(1 - d2);
      const offset = (py * size + px) * 4;

      // the rim is a one-pixel cream outline, like the reference
      if (d2 > Math.pow(1 - 1.4 / radius, 2)) {
        const [r, g, b] = PALETTE.rim;
        pixels.set([r, g, b, 255], offset);
        continue;
      }

      // rotate the point around the vertical axis, then sample the noise on the sphere
      const sx = x * cos + z * sin;
      const sz = -x * sin + z * cos;
      const height = fbm(sx * 2.2 + 11, y * 2.2 + 11, sz * 2.2 + 11, seed);
      const cloud = fbm(sx * 3 + 40, y * 3.4 + 40, sz * 3 + 40 + angle * 0.35, seed + 99);
      const base = surfaceColor(height, y, cloud);

      // lambert shading, posterised into four bands for the pixel-art look
      const lit = Math.max(0, x * light[0] + y * light[1] + z * light[2]);
      const level = SHADE_LEVELS[Math.min(SHADE_LEVELS.length - 1, Math.floor(lit * SHADE_LEVELS.length))] ?? 1;
      const [r, g, b] = shade(base, level);
      pixels.set([r, g, b, 255], offset);
    }
  }
  return pixels;
}

export interface GlobeHandle {
  stop(): void;
}

/**
 * Draws the globe on `canvas` (a small logical size, scaled up with
 * `image-rendering: pixelated`). Animates unless the user prefers reduced
 * motion, and pauses while the tab is hidden.
 */
export function startGlobe(canvas: HTMLCanvasElement, options: { size?: number; fps?: number } = {}): GlobeHandle {
  const size = options.size ?? 112;
  const fps = options.fps ?? 4;
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext("2d");
  if (!context) return { stop: () => undefined };

  const draw = (angle: number): void => {
    context.putImageData(new ImageData(renderGlobe(size, angle) as Uint8ClampedArray<ArrayBuffer>, size, size), 0, 0);
  };
  draw(0.6);

  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  if (reduced) return { stop: () => undefined };

  let angle = 0.6;
  const timer = window.setInterval(() => {
    if (document.hidden) return;
    angle += 0.09;
    draw(angle);
  }, 1000 / fps);
  return { stop: () => window.clearInterval(timer) };
}
