/**
 * 8x8 pixel icons, drawn as SVG rectangles so they stay crisp at any size and
 * take their colour from `currentColor`. Each icon is eight rows of eight
 * characters: `#` is a filled pixel.
 */
export const ICONS = {
  chat: [".######.", "#......#", "#.#.#..#", "#......#", ".######.", "..#.....", ".#......", "........"],
  ticket: [".######.", "##....##", "#.####.#", "#......#", "#.####.#", "##....##", ".######.", "........"],
  book: [".######.", "##.....#", "#.####.#", "#......#", "#.####.#", "#......#", ".######.", "........"],
  pulse: ["........", "........", "...#....", "..#.#..#", "##...##.", "........", "........", "........"],
  user: ["..####..", "..####..", "..####..", "...##...", ".######.", "########", "########", "........"],
  plus: ["...##...", "...##...", "...##...", "########", "########", "...##...", "...##...", "...##..."],
  trash: ["..####..", "########", ".#.##.#.", ".#.##.#.", ".#.##.#.", ".#.##.#.", ".######.", "........"],
  refresh: [".#####..", "#.....#.", "#....###", "......#.", "#......#", "#......#", ".#....#.", "..####.."],
  send: ["...#....", "....#...", ".....#..", "########", ".....#..", "....#...", "...#....", "........"],
  stop: ["........", ".######.", ".######.", ".######.", ".######.", ".######.", ".######.", "........"],
  menu: ["........", "########", "........", "########", "........", "########", "........", "........"],
  close: ["#......#", ".#....#.", "..#..#..", "...##...", "...##...", "..#..#..", ".#....#.", "#......#"],
  check: ["........", ".......#", "......##", "#....##.", "##..##..", ".####...", "..##....", "........"],
  up: ["...#....", "...##...", "..###...", "######..", "######..", "######..", ".####...", "........"],
  down: ["........", ".####...", "######..", "######..", "######..", "..###...", "...##...", "...#...."],
  upload: ["...##...", "..####..", ".######.", "...##...", "...##...", "........", "########", "########"],
  shield: [".######.", "########", "########", "########", ".######.", "..####..", "...##...", "........"],
  lock: ["..####..", ".#....#.", ".#....#.", "########", "##....##", "##.##.##", "##....##", "########"],
  help: ["..####..", ".##..##.", "....##..", "...##...", "...##...", "........", "...##...", "...##..."],
} as const;

export type IconName = keyof typeof ICONS;

const SVG_NS = "http://www.w3.org/2000/svg";

export interface IconOptions {
  size?: number;
  /** Accessible name; icons without one are hidden from assistive technology. */
  label?: string;
}

export function pixelIcon(name: IconName, options: IconOptions = {}): SVGSVGElement {
  const size = options.size ?? 16;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 8 8");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("shape-rendering", "crispEdges");
  if (options.label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.label);
  } else {
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
  }
  ICONS[name].forEach((row, y) => {
    // Merge each horizontal run of pixels into one rectangle.
    for (const run of row.matchAll(/#+/g)) {
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", String(run.index ?? 0));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", String(run[0].length));
      rect.setAttribute("height", "1");
      svg.appendChild(rect);
    }
  });
  return svg;
}
