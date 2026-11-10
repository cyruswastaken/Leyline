"""Script and stylesheet tags for the compiled frontend (``frontend/``).

``npm run build`` writes hashed files and a Vite manifest to
``src/leyline/static/dist``. Templates call ``vite_assets("chat")`` and get the
matching ``<link>`` and ``<script type="module">`` tags. Nothing is inline, so
the strict Content-Security-Policy keeps working.

* Without a manifest (a fresh checkout, or tests that never built the
  frontend) the helper emits nothing instead of failing, and logs once.
* With ``FRONTEND_DEV_SERVER`` set (local development only) it points at the
  Vite dev server for hot reload instead.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from markupsafe import Markup, escape

from leyline.config import Settings

log = logging.getLogger(__name__)

STATIC_DIST = Path(__file__).resolve().parent / "static" / "dist"
MANIFEST_PATH = STATIC_DIST / "manifest.json"
URL_PREFIX = "/static/dist/"
ENTRY_DIR = "src/entries"

_cache: dict[tuple[Path, int], dict[str, Any]] = {}
_warned: set[str] = set()


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any] | None:
    """The parsed manifest, re-read only when the file changes."""
    try:
        key = (path, path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _cache:
        _cache.clear()
        _cache[key] = json.loads(path.read_text(encoding="utf-8"))
    return _cache[key]


def _collect(
    manifest: dict[str, Any], name: str, seen: set[str]
) -> tuple[list[str], list[str]]:
    """CSS files and preload chunks reachable from a manifest entry."""
    if name in seen or name not in manifest:
        return [], []
    seen.add(name)
    item = manifest[name]
    css = list(item.get("css", []))
    chunks: list[str] = []
    for imported in item.get("imports", []):
        sub_css, sub_chunks = _collect(manifest, imported, seen)
        css += sub_css
        chunks += [manifest[imported]["file"], *sub_chunks]
    return css, chunks


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        log.warning(message)


def vite_assets(
    settings: Settings, entry: str, *, manifest_path: Path = MANIFEST_PATH
) -> Markup:
    if settings.frontend_dev_server:
        base = settings.frontend_dev_server.rstrip("/")
        return Markup(
            '<script type="module" src="{base}/@vite/client"></script>'
            '<script type="module" src="{base}/{entry}"></script>'
        ).format(base=base, entry=f"{ENTRY_DIR}/{entry}.ts")

    manifest = load_manifest(manifest_path)
    if manifest is None:
        _warn_once("frontend is not built (run `npm run build` in frontend/)")
        return Markup("")
    key = f"{ENTRY_DIR}/{entry}.ts"
    if key not in manifest:
        _warn_once(f"frontend entry {entry!r} is not in the manifest")
        return Markup("")

    css, chunks = _collect(manifest, key, set())
    tags = [
        Markup('<link rel="stylesheet" href="{}{}">').format(URL_PREFIX, name)
        for name in dict.fromkeys(css)
    ]
    tags += [
        Markup('<link rel="modulepreload" href="{}{}">').format(URL_PREFIX, name)
        for name in dict.fromkeys(chunks)
    ]
    tags.append(
        Markup('<script type="module" src="{}{}"></script>').format(
            URL_PREFIX, escape(manifest[key]["file"])
        )
    )
    return Markup("\n").join(tags)
