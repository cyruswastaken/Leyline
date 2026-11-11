# 0008. No inline code and no markup built from data

**Status:** Accepted (Phase 7)

## Context
The pages show text a model produced and text other people wrote (ticket comments, log lines, document
titles). Any path that turns such text into markup, or any inline script or style, weakens the browser's last
line of defence against injection.

## Decision
- No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval`, and no links, images, frames or
  scripts created from client code; text goes in with `textContent`. A small Markdown renderer builds DOM nodes.
- No inline script, `style=` or `on...=` anywhere, so the Content Security Policy has **no `unsafe-inline`**; every
  script and stylesheet comes from `/static/dist`.
- ESLint (`frontend/eslint.config.js`) and `tests/test_frontend_sources.py` both enforce it.

## Consequences
- The UI is built with Vite and TypeScript; a new page needs an entry file, a build entry and an access-matrix row.
- Some convenient techniques are off the table (inline event handlers, data-URI images).

## Alternatives considered
A sanitizing library with `innerHTML` (one bug away from script execution), relaxing the policy with `unsafe-inline`.

## Evidence
The two enforcement points above, the CSP tests (`tests/test_csrf_and_headers.py`), and the axe audit with zero
violations on the audited pages.
