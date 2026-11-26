# Leyline

Permission-aware RAG helpdesk (Flask). Goal: production-grade engineering
with synthetic data. The phased roadmap is in `docs/ROADMAP.md`.

**Starting a session: read `SESSION_HANDOFF.md` first** (current status, pending work,
decisions, known issues, and the exact next step), then this file.

## Commands

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt
pip install --no-deps -e .       # editable install of src/leyline
pytest                           # tests (use --cov for coverage)
ruff check . && ruff format .    # lint + format
mypy                             # strict type check
docker compose up --build        # app, worker, Postgres, Redis, Keycloak, Azurite
docker compose run --rm app python -m leyline.seed   # load the sample corpus
docker compose up -d postgres azurite   # needed by the integration tests
python -m leyline.db upgrade|current|check|revision -m "msg"   # migrations
python scripts/e2e_login.py      # full-stack check (stack up + corpus seeded); E2E_SEMANTIC=1 adds semantic checks
E2E_CHAT=1 python scripts/e2e_login.py   # also drives a real chat model (Ollama qwen2.5:3b)
python -m leyline.evals.answers [--verbose]   # answer quality with the real chat model (manual gate)
cd frontend && npm ci && npm run check   # UI: lint, typecheck, unit tests, build, bundle budget
docker compose -p leyline-e2e -f compose.yaml -f compose.e2e.yaml up -d --build --wait   # browser-test stack (stop the dev stack first; then seed with `run --rm app python -m leyline.seed`)
cd frontend && E2E_BROWSER_CHANNEL=msedge npm run e2e   # Playwright journeys + axe audit (PowerShell: $env:E2E_BROWSER_CHANNEL="msedge"); `down -v` on the e2e project afterwards
cd frontend && npm run build && cd .. && docker compose -f compose.yaml -f compose.frontend.yaml up -d --build app   # UI loop against the real stack
./infra/deploy.ps1 -WhatIf       # Azure: preview the deployment (creates nothing); then ./infra/deploy.ps1, ./infra/teardown.ps1
python -m leyline.evals [--index fts|hybrid] [--embeddings evals/embeddings.json] [--check evals/thresholds.json] [--sweep]
```

CI runs the same checks (`.github/workflows/ci.yml`); keep them green.

## Layout

- `src/leyline/app.py`: `create_app()` factory; run via gunicorn
  `leyline.app:create_app()`
- `src/leyline/config.py`: typed settings, the only place env vars are read
- `src/leyline/auth/`: `Principal`, Redis sessions, OIDC setup, CSRF, `login_required` / `require_roles`
- `src/leyline/web/`: Flask blueprints (routes only, no business logic)
- `frontend/`: Vite + strict TypeScript + Tailwind 4 (design system, per-page scripts, tests); builds into `src/leyline/static/dist/` (git-ignored). `src/leyline/templates/` are thin Jinja pages on `base.html`; `src/leyline/frontend.py` emits the hashed asset tags (see docs/FRONTEND.md)
- `deploy/keycloak/themes/leyline/login/`: the Keycloak sign-in pages in the app's design system (extends `keycloak.v2`; mounted by compose, selected by `loginTheme` in the realm). Its colours mirror `frontend/src/styles/main.css` (a test compares them); after changing fonts or the globe run `cd frontend && npm run theme:sync`
- `src/leyline/db/`: SQLAlchemy models + Alembic migrations (pg8000 driver, pure Python)
- `src/leyline/ports/` + `adapters/`: BlobStore, Embedder, SearchIndex interfaces and implementations
- `src/leyline/ingestion/`: parsers, chunker, validation, job queue, `IngestionService`
- `src/leyline/worker.py`, `seed.py`: ingestion worker and corpus importer
- `src/leyline/adapters/search_postgres.py`: hybrid search (keyword + vector, RRF, relevance floors)
- `src/leyline/evals/` + top-level `evals/`: evaluation harness, golden questions, thresholds, recorded embeddings (see docs/EVALUATION.md)
- `src/leyline/extensions.py`: typed access to per-app settings, Redis, limiter
- `infra/`: Azure Bicep (subscription scope) plus `deploy.ps1` / `teardown.ps1` / `entra.ps1` (Entra app, roles, groups; local secret files are git-ignored); disposable resources live in `rg-leyline-app`, never touch the OpenAI group `rg-leyline`; secrets only in Key Vault (`tests/test_infra.py` guards this)
- `deploy/keycloak/`: local identity provider realm (demo users in docs/USAGE.md)
- `src/leyline/services/`: pipeline logic (`chat_service` turn pipeline + streaming, `conversations` owner-scoped storage, `guardrails`, `llm` prompts)
- `src/leyline/ports/llm.py` + `adapters/llm_openai.py`, `circuit_breaker.py`: the chat model behind a port, with a circuit breaker
- `corpus/`: synthetic documents with a `Groups:` ACL header (legacy `Access Level:` still understood)
- `tests/test_regressions.py`: one test per audited vulnerability
- `tests/test_access_matrix.py`: every route x every persona; fails on a route with no declared policy
- `tests/support.py`: personas and helpers (`login_as` creates a real server-side session)

## Rules that must not be weakened

- Identity comes only from the authenticated session, never from request
  input (URL, query, body, headers). Every new route needs an entry in the
  access-matrix test and `login_required` / `require_roles`.
- Session store failure fails closed (503), never anonymous or signed-in.
- Who may read a document is decided only by `document_acl` through `services/authorization.py`
  (`readable_by` in SQL, `can_read` in Python; a test keeps them equal). Search takes a
  `Principal` argument on purpose, and hits are re-checked with `authorize_hits`. App roles
  (even kb_admin) never grant read access.
- Uploads are validated by content, stored under ids (never the filename), and parsed with
  bounded size/pages/expansion. Document text is never returned by admin endpoints.
- Schema changes only through Alembic migrations; never edit a merged migration.
- Every new route needs an entry in `tests/test_access_matrix.py`.
- The Azure deployment has its own floors, recording and gate (`evals/thresholds.azure.json`, `evals/embeddings.azure.json`, `tests/test_evals_azure.py`; floors live in `infra/modules/apps.bicep`). Record with no task prefixes and from a folder without a `.env` (see docs/EVALUATION.md).
- Retrieval changes are judged by `python -m leyline.evals`; CI gates `evals/thresholds.json`.
  Never lower a floor to make a change pass, never tune golden questions to the search, and
  re-record `evals/embeddings.json` when the corpus, chunker, golden set, model or prefixes change.
  `acl_leaks` must be 0 and `restricted_pass_rate` 1.0, always.
- Conversations are owner-scoped on every query (owner from the principal only); someone else's
  conversation or message answers 404 exactly like a missing one. Old citations are re-checked with
  `readable_by` each time they are shown.
- Assistant messages are never put back into a prompt (an ACL may have changed since); follow-up
  rewriting uses only the user's own earlier questions. Retrieved text is always delimited as
  untrusted and neutralised; the model has no tools; model output is redacted and its citations verified.
- Pages never build markup from data: no innerHTML/outerHTML/insertAdjacentHTML, document.write, eval, and no links, images, frames or scripts created from client code. ESLint (`frontend/eslint.config.js`) and `tests/test_frontend_sources.py` both enforce it.
- No inline script, style, `style=` or `on...=` in any template, so the CSP has no `unsafe-inline`; scripts and styles come from `/static/dist`. A new page needs an entry in `frontend/vite.config.ts`, `src/entries/<name>.ts`, `{% set entry = "<name>" %}`, and an access-matrix row.
- The sign-in pages must keep looking like the app: change a design token in `frontend/src/styles/main.css` and the same value in `leyline.css` of the login theme. The realm must keep one enabled locale so Keycloak emits `<html lang>`.
- Frontend changes keep `npm run check` green (lint, types, tests, budget: 30 KB JS and 15 KB CSS per page gzipped) and the design tokens AA-contrast (tested).
- Authorization happens before chunking, retrieval and prompt building.
  Unauthorized content must never reach the model.
- Document metadata is read only from the leading header block.
- Never render log or event data with `innerHTML`; use `textContent`.
- Config is validated at startup; production must fail fast.
- Every security-sensitive change needs allowed-path and denied-path tests.

## Known gaps (tracked in the roadmap)

- A user's own groups/roles are captured at sign-in and lag until the session expires;
  document-side ACL changes and deletions apply immediately.
- Hybrid retrieval needs an embedding model (Ollama + nomic-embed-text); without one it is
  keyword-only. Relevance floors are calibrated for nomic-embed-text and must be re-swept for another model.
  Near-topic out-of-scope questions can still return loosely related documents.
- Parsers run in the worker process without sandboxing (inputs are bounded).
- Answer quality (`leyline.evals.answers`) needs a chat model, so it is a manual gate, not CI.
  A 3B local model follows citation and refusal instructions imperfectly; expect misses.
- Security events are stored synchronously and fail open (an event-store outage drops rows, not requests); append-only is a trigger, so production must also give the app's database role INSERT/SELECT only.
- Browser tests (`frontend/e2e`, CI job `Browser tests`) run in Chromium only, with a fake chat model and
  keyword search; the Entra sign-in on Azure is checked by a person.
- `requirements.txt` is a pinned lock of runtime deps; regenerate it from a
  clean venv when changing `pyproject.toml` dependencies.
