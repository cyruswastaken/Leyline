import "../styles/main.css";
import { startDither } from "../lib/dither";
import { initChrome } from "../pages/chrome";

initChrome();

const banner = document.getElementById("error-dither");
if (banner instanceof HTMLCanvasElement) startDither(banner, { scene: "landscape", cell: 4 });
