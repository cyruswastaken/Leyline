# Frontend

Leyline's interface is server-rendered Jinja pages with a small amount of TypeScript per page, built
by Vite into `src/leyline/static/dist/` and served by Flask. There is no single-page application and
no client-side routing: Flask decides who may see what, and the scripts only make the pages live.

## Design system

The look comes from a reference mock-up: a near-black background with a faint grid, a neon lime
accent, giant pixel headings with a trailing slash (`ASK/`, `TICKETS/`), small uppercase monospace
labels, braced buttons (`{SEND}`), `×××` section markers, and a pixel-art globe. It is adapted for
an application: pixel type for titles only, monospace for interface chrome, and a readable sans-serif
for everything people read at length (answers, tickets, tables).

| Token | Value |
| --- | --- |
| Background / panel / raised panel | `#0b0c0b` / `#171917` / `#1e211e` |
| Text / muted | `#e8ece6` / `#8f988e` |
| Accent (lime) and its pressed state | `#b6f36a`, `#8fcc45` |
| Status colours | info `#6bb6ff`, warning `#f5c542`, danger `#ff7a7a` |
| Fonts | Silkscreen (titles), JetBrains Mono (chrome), IBM Plex Sans (reading), all OFL-licensed and bundled |

Tokens live in `frontend/src/styles/main.css` (`@theme`), with shared component classes (`.btn`,
`.card`, `.badge`, `.field`, `.table`, `.prose-answer`, `.cite`, `.streaming`). A test checks every
text/background pair against WCAG AA, so a token change that breaks contrast fails the build.

Other building blocks:

- **Pixel globe** (`lib/globe.ts`): drawn procedurally on a canvas from seeded noise in the reference's
  palette (no image to license or download). It turns slowly in discrete steps and stays still when the
  user prefers reduced motion.
- **Dithering** (`lib/dither.ts`), inspired by a second reference: a smooth brightness field is
  squeezed into five flat colours (transparent, three deep greens, lime) and an 8x8 Bayer matrix picks,
  pixel by pixel, which of the two nearest colours to use. It draws the rolling landscape under the
  landing hero and on the error pages. It drifts a pixel at a time and stands still under
  `prefers-reduced-motion`. The top of the scene is transparent so text above it keeps its contrast,
  and the canvases are `aria-hidden`. The same code renders `dither.png` for the sign-in pages.
- **Pixel icons** (`lib/pixel-icon.ts`): 8x8 bitmaps drawn as SVG rectangles, coloured with
  `currentColor`.
- **Motion:** short and functional only; everything respects `prefers-reduced-motion`.

## Structure

```text
frontend/
  src/entries/        one file per page: landing, chat, tickets, admin, monitoring, error
  src/pages/          page behaviour (chat/, tickets/, admin/, monitoring/, ticket-dialog, chrome)
  src/lib/            api (fetch + CSRF + 401 redirect), sse (stream reader), markdown (safe
                      renderer), dom (el helper), globe, dither, pixel-icon, confirm (two-step delete)
  src/styles/main.css design tokens and components (Tailwind 4)
  scripts/            check-budget.mjs, copy-axe.mjs
src/leyline/templates/ Jinja: base.html (layout, nav by role), one template per page, partials/
src/leyline/frontend.py  vite_assets("chat") -> the hashed <link> and <script> tags
src/leyline/static/dist/ build output (git-ignored; built by `npm run build` and in the Docker image)
```

Templates extend `base.html`, declare `{% set entry = "..." %}` and `{% set active = "..." %}`, and
contain no inline CSS or JavaScript. Tailwind finds class names in both `frontend/src` and the
templates (`@source` in `main.css`).

## Working on the UI

```bash
cd frontend && npm ci
npm run check            # lint + typecheck + unit tests + build + bundle budget (what CI runs)
npm run build            # writes src/leyline/static/dist
```

Against the real stack, either:

```bash
docker compose -f compose.yaml -f compose.frontend.yaml up -d --build app
cd frontend && npm run build && cd .. && docker compose restart app   # after UI changes
```

(`compose.frontend.yaml` mounts the templates and built assets over the image's copies), or with hot
reload: run `npm run dev` and set `FRONTEND_DEV_SERVER=http://localhost:5173` for the app. That setting
widens the CSP to the dev server and is rejected when `LEYLINE_ENV=production`.

If the frontend has not been built, pages render without styles or behaviour and the app logs
`frontend is not built`; the Docker image always builds it in a separate Node stage that does not ship.

## The sign-in pages (Keycloak theme)

The sign-in, error and sign-out pages are rendered by the identity provider, not by Flask. Locally
that is Keycloak, so the repository ships a login theme that gives those pages the same look as the
app (pixel globe, `LEYLINE/` wordmark, grid, braced `{SIGN IN}` button, lime focus rings, errors in
the danger colour), so the experience is one continuous product:

```text
deploy/keycloak/themes/leyline/login/
  theme.properties          parent=keycloak.v2, styles=css/leyline.css
  messages/messages_en.properties   page texts ("Sign in", "Sign out of Leyline?")
  resources/css/leyline.css the design tokens and PatternFly overrides
  resources/fonts/          the same fonts as the app (+ their OFL licences)
  resources/img/globe.png   the pixel globe, rendered by the app's own globe code
  resources/img/dither.png  the dithered landscape pinned to the bottom of the page (the app's dither code)
```

- It extends Keycloak's stock theme and replaces only the stylesheet and a few texts, so every login
  flow (wrong password, forgot password, update password, logout) keeps working and keeps Keycloak's
  accessibility behaviour.
- `compose.yaml` mounts the theme; `leyline-realm.json` selects it (`loginTheme`). Keycloak's dev mode
  does not cache themes, so a browser refresh shows edits.
- The colours in `leyline.css` mirror `frontend/src/styles/main.css`; a test compares every one of
  them, so the two cannot drift apart. After changing fonts or the globe, run
  `cd frontend && npm run theme:sync` (it copies the fonts and re-renders `globe.png` and `dither.png`) and commit
  the result.
- The realm enables one locale (English) so Keycloak writes `<html lang="en">`; without internationalisation
  the stock template omits it, which axe reports as a serious failure. No language picker appears.
- axe-core on the sign-in page (also with a wrong-password error): zero violations. The page was also
  checked at 375 px with no horizontal overflow.

**Production identity providers:** in Azure the sign-in page belongs to Microsoft Entra ID, which is
themed in the Entra admin centre (company branding: background colour, logo, text) rather than with
files from this repository. Use the same tokens (`#0b0c0b`, `#b6f36a`) there. How closely it can match
(logo, colours, possibly custom CSS) depends on the tenant's licence and was not verified; the pixel
type and globe may not carry over. Replicating the look exactly would need a custom identity page,
which is a different (and security-sensitive) decision.

## Rules the code enforces

- **Data never becomes markup.** Text from users, documents and the model is inserted only with
  `textContent` or `createElement`. ESLint bans `innerHTML`, `outerHTML`, `insertAdjacentHTML`,
  `document.write`, `eval`, `new Function`, and creating links, images, frames or scripts from client
  code; `tests/test_frontend_sources.py` repeats those checks on the sources and templates in Python.
- **No inline code at all.** Templates hold no `<script>`, `<style>`, `style=` or `on...=` attributes
  (a test fetches every page and checks), so the Content-Security-Policy needs no `unsafe-inline`
  exceptions: scripts and styles come from `/static/dist` with a per-request nonce, fonts from the same
  origin.
- **The server decides access.** Pages are protected by the same decorators and access matrix as the
  API; the scripts never hide something that the server would still serve.
- **Hashed assets** (`/static/dist/assets/*`) are cached for a year and exempt from the per-IP rate
  limit and from session loading; HTML is never cached.

## Accessibility

Structure: a skip link, landmarks, one `h1` per page, labelled controls, `aria-live` regions for
streaming and status, `aria-pressed` on feedback buttons, a native `<dialog>` for the support form,
focus-visible outlines in lime, and `prefers-reduced-motion` support. The chat page keeps a (visually
hidden) `h1` while its large pixel title is only decoration.

Audits use axe-core in a real browser (so colour contrast is measured on the real render):

```bash
cd frontend && npm run axe:copy      # copies axe into the git-ignored build output
# open a page, then in the browser console:
#   const s = document.createElement("script"); s.src = "/static/dist/axe.js"; document.head.append(s);
#   await new Promise((r) => (s.onload = r)); (await axe.run()).violations
```

Result on 2026-11-10: zero violations on the landing, chat (empty and with an answer), tickets,
knowledge admin and monitoring pages, with 27-42 rules passing per page. The error page and the agent view of the tickets page were not audited. The audit found
one real bug, fixed in this phase (the chat page lost its `h1` once a conversation started). It is a
manual check, not part of CI.

## Performance

Fonts and scripts are self-hosted and hashed. Budget (gzipped, enforced by `npm run budget`, per page
including shared chunks): 30 KB JavaScript and 15 KB CSS. Actual: the largest page is about 8 KB of
JavaScript; all pages share about 6 KB of CSS.

## Known gaps

- No browser-driven end-to-end tests (for example Playwright); the real-stack script and unit tests
  cover the API and the rendering code, and the pages were walked through by hand and audited with axe.
- Screen-reader behaviour was checked structurally (roles, names, live regions) but not with a real
  screen reader.
- Only a dark theme exists.
- The sign-in pages are themed only for the local Keycloak; Entra ID in production is branded through its own settings, not these files.
