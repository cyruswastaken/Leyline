import { startDither } from "../lib/dither";
import { startGlobe } from "../lib/globe";
import { ICONS, pixelIcon, type IconName } from "../lib/pixel-icon";

/** The public landing page: the hero globe and the icons on the feature cards. */
export function initLanding(): void {
  const hero = document.getElementById("hero-globe");
  if (hero instanceof HTMLCanvasElement) startGlobe(hero, { size: 160 });

  // the dithered landscape under the hero, in the app's own colours
  const field = document.getElementById("hero-dither");
  if (field instanceof HTMLCanvasElement) startDither(field, { scene: "landscape", cell: 4 });

  for (const title of document.querySelectorAll<HTMLElement>("[data-icon]")) {
    const name = title.dataset.icon;
    if (name && name in ICONS) title.prepend(pixelIcon(name as IconName, { size: 18 }));
  }
}
