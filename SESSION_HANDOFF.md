# Session handoff

> **Last updated:** 2026-11-10, after Phase 7 (frontend). Phases 0-6 are committed (Phase 6 as `5baa697`). **Phase 7 was handed to the user uncommitted** (they stage and commit); check `git log` / `git status` first to see whether it has landed. The roadmap was renumbered: the new frontend phase is **7**, Azure is now **Phase 8**.
> **Read order for a new session:** this file → `CLAUDE.md` (rules that must not be weakened; git-ignored, so it may be missing on a fresh clone) → the doc for whatever you touch.
> This file records *state and next steps*. It deliberately does not repeat the detail in
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/USAGE.md`](docs/USAGE.md),
> [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md), [`docs/EVALUATION.md`](docs/EVALUATION.md),
> [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) or [`docs/ROADMAP.md`](docs/ROADMAP.md) (audit findings, target architecture, phase gates).
> Update the **Status**, **Pending**, **Known issues** and **Immediate next step** sections at the end of every phase.

## 1. Project overview

**Leyline** is a security-focused enterprise IT helpdesk chatbot: permission-aware retrieval-augmented generation (RAG) on Flask. Employees sign in through their organisation's identity provider and ask IT questions; answers are grounded only in documents the asker may read. Knowledge-base admins upload documents and choose which identity-provider groups can read each one.

**Goal:** production-grade engineering (real login, permissions enforced at retrieval, tests, CI/CD, evals, observability, IaC) as a portfolio project, using **synthetic data** and demo users. Not in scope: formal compliance, on-call, multi-tenancy. No Azure subscription exists yet, so everything is built **local-first behind swappable adapters**; Azure deployment is the last phase.

**v1 features (agreed):** conversations + streaming, citations + feedback, document admin + ingestion, ticket escalation. Permissions: per-document group ACLs plus app roles.

## 2. Current architecture

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Summary of how it connects:

```text
Browser (server-rendered Jinja + vanilla JS, strict CSP nonces)
  └─ Flask app (gunicorn, src/leyline/)  ── Redis: sessions + rate limits
       ├─ OIDC login (Authlib, PKCE)  ──►  Keycloak (local) / Entra ID (Azure, later)
       ├─ /api/chat/stream (SSE) and /chat: rate limit → injection scoring → (rewrite follow-up) → hybrid search(principal) → authorize_hits → delimited context → ChatModel → redact + verify citations → save
       ├─ /api/conversations, /api/messages/<id>/feedback: owner-scoped history, citations re-authorized on read
       ├─ /admin/*: kb_admin API + page  ──►  IngestionService
       └─ Postgres (+pgvector): documents, versions, document_acl, chunks(+vector(768), HNSW), ingestion_jobs, audit_events, conversations, messages, feedback
  Worker (same image): claims jobs → blob store → parse → chunk → embed → swap chunks in one tx
  Blob store: Azurite (local) / Azure Blob;  Embeddings: Ollama nomic-embed-text (local) / any OpenAI-compatible endpoint
  Chat model: `ChatModel` port (ports/llm.py) + OpenAI-compatible adapter with timeouts, retries and a circuit breaker; locally Ollama `qwen2.5:3b`, later Azure OpenAI. Prompts: services/llm.py. Guardrails: services/guardrails.py
  Evals: src/leyline/evals + evals/ (golden questions, thresholds, recorded embeddings) → CI gate
```

- **Frontend:** Jinja pages on one `base.html` plus `frontend/` (Vite 8, strict TypeScript, Tailwind 4, self-hosted fonts) built into `src/leyline/static/dist/` (git-ignored; the Docker image builds it in a Node stage that does not ship). Entries: landing (`/welcome`, public), chat, tickets, admin, monitoring, error. No inline script or style anywhere, so the CSP has no `unsafe-inline`. Data is rendered with `textContent` only. Details: `docs/FRONTEND.md`.
- **Auth:** identity is a `Principal` built only from verified ID-token claims (`groups` = what you may *read*, `roles` = what you may *do*: `user`, `kb_admin`, `support_agent`, `security_auditor`). Server-side Redis sessions, CSRF, CSP, per-IP and per-user rate limits.
- **Authorization core:** `services/authorization.py` - `readable_by` (SQL, inside *each* search leg) and `can_read` (Python) must agree (tested); `authorize_hits` re-checks every hit before the prompt.
- **Retrieval:** `adapters/search_postgres.py` - keyword leg (`ts_rank_cd`) + vector leg (cosine, HNSW, pgvector iterative scan) fused by RRF, relevance floors on the underlying signals (cosine 0.58, keyword rank 0.5, calibrated for nomic-embed-text), max 2 chunks per document, deterministic tie-breaks, graceful fallback to keyword search.
- **Ports/adapters:** `BlobStore`, `Embedder`, `SearchIndex` in `src/leyline/ports/`; implementations in `src/leyline/adapters/`. A new `SearchIndex` (Azure AI Search) must pass `tests/test_search_contract.py` and the eval gate.
- **API surface:** endpoint table in [`docs/USAGE.md`](docs/USAGE.md#http-endpoints); every route must have a policy in `tests/test_access_matrix.py`.
- **Layout:** `src/leyline/{app,config,constants,wiring,worker,seed,extensions,ratelimit,security_headers}.py` + `auth/ web/ db/ ingestion/ ports/ adapters/ services/ evals/ templates/`; `tests/`, `corpus/` (49 synthetic docs), `evals/`, `deploy/keycloak/`, `scripts/e2e_login.py`, `compose.yaml`, `Dockerfile`, `.github/workflows/ci.yml`.

## 3. Completed work (verified)

| Phase | What shipped | Status |
| --- | --- | --- |
| Audit | 7 critical + several functional bugs found in the original lab app (data-dumping routes, role-in-URL, debug mode, XSS, classification downgrade, 500s instead of 403/429, ...). Catalogue in `docs/ROADMAP.md`. | all fixed, each with a regression test |
| 0 | `src/leyline/` restructure, app factory, typed config (fails fast in production), `/healthz` + `/readyz`, Dockerfile, ruff/mypy/pytest/pre-commit, CI, LICENSE | committed |
| 1 | OIDC+PKCE login, Redis sessions, roles, CSRF, CSP nonces, Redis rate limiting, Keycloak realm with demo users, route x persona access matrix | committed |
| 2 | Postgres + Alembic, validated PDF/DOCX/MD/TXT ingestion, chunker, idempotent uploads, atomic re-versioning, job queue + worker, admin UI, per-document group ACLs, audit trail, 49-doc corpus, seed + migration CLIs | committed, pushed |
| 3 | Golden set (78 questions) + eval harness + CI gate; migration `0002` (`vector(768)` + HNSW); hybrid search with RRF, relevance floors, doc cap; contract suite; record/replay of real embeddings for offline CI; Ollama + nomic-embed-text | committed (`da9d8a2`) |
| 4 | `ChatModel` port + resilient adapter (timeouts, retries, circuit breaker, 503 degraded mode); migration `0003` (conversations, messages, feedback); SSE streaming with cancel; verified, re-authorized citations; feedback; follow-up rewriting; token budget; guardrails; rebuilt UI; answer-quality eval | committed (`32c30e2`) |
| 5 | Tickets: migration `0004`, `TicketProvider` port + Postgres queue, edit-before-send draft built only from the user's own words, ticket page + agent queue, per-user rate limit, credential scrubbing | committed (`6171f77`) |
| 6 | Append-only `security_events` (migration `0005`, SQL dashboard), JSON stdout events, OpenTelemetry (off by default), prompt-leak detection, `docs/THREAT_MODEL.md`, red-team suite, security CI workflow + Dependabot + `.gitleaks.toml`, single-layer hardened image (scans clean), Locust load test + `docs/LOAD_TEST.md`, gunicorn 2x16 threads | committed (`5baa697`) |
| 7 | Frontend design system from the user's reference mock-up (near-black grid, lime accent, pixel titles, braced buttons, procedural pixel globe); `frontend/` toolchain; landing, chat, tickets, admin, monitoring and error pages on one layout; a Keycloak login theme (`deploy/keycloak/themes/leyline`) so the sign-in, error and logout pages match; ESLint + Python static checks that keep data out of markup; frontend CI job, bundle budget, `npm audit`, Dependabot npm; Docker Node stage | **built, uncommitted** |

**Phase 3 results** ([`docs/EVALUATION.md`](docs/EVALUATION.md)): recall@5 0.796 -> **1.000**, MRR 0.640 -> **0.936**, recall@1 0.519 -> 0.889, correct refusals 25% -> **83%**, restricted pass 1.000, **0 ACL leaks** (gains confirmed on independent halves of the set).

**Phase 7 verification:** 1180 Python tests (95% coverage; the suite includes 37 static source checks, 12 error-page tests, 12 asset tests and the updated CSP/inline-code tests) and 89 frontend tests (Vitest: Markdown renderer incl. HTML-injection attempts, SSE parser split at every byte, API client, globe determinism and palette, pixel icons, two-step confirm, admin and monitoring logic, WCAG AA contrast of every token pair); ruff/format/strict mypy/ESLint/tsc clean; `E2E_CHAT=1 E2E_SEMANTIC=1 python scripts/e2e_login.py` passed on the image built by the Docker Node stage; Trivy on that image 0 HIGH/CRITICAL; gitleaks clean; `npm audit` clean for production dependencies and nothing high in tooling (10 moderate, dev-only ESLint tree); bundle budget (30 KB JS, 15 KB CSS per page gzipped) met with the largest page at ~8 KB JS. In a real browser: every page walked as its persona (alice, sue.agent, kim.admin, ada.auditor), streaming, Stop, citations, feedback, contact-support dialog, ticket create/assign, 403 page, mobile 375 px with the slide-over sidebar, and axe-core with zero violations on landing, chat (empty and with an answer), tickets, admin and monitoring (it found one real bug: chat lost its h1). **Login theme (added after the first Phase 7 review):** the Keycloak sign-in page, wrong-password state and logout confirmation were restyled (globe, wordmark, braced button, danger colours); verified in a real browser at desktop and 375 px with no overflow, axe zero violations on the sign-in page (it found a missing `<html lang>`: fixed by enabling one locale in the realm), a real sign-in through it, and `tests/test_login_theme.py` (tokens match the app, assets exist, no network loads, realm and compose wiring). **Not verified:** the error page and the agent tickets view under axe, a real screen reader, any Playwright-style automated browser test, CI on GitHub (never run), the new CI frontend job and smoke-test edits (the smoke commands were run by hand against the live stack).

**Phase 6 verification:** 1105 tests pass (CI mode, 95% coverage), ruff/format/strict mypy clean, `E2E_CHAT=1 E2E_SEMANTIC=1 python scripts/e2e_login.py` passed. Run locally: `pip-audit` (clean), gitleaks over history and working tree (clean after allowlisting fake credentials in tests), Trivy config and image (0 HIGH/CRITICAL after hardening), Locust at 8 and 24 users (targets met with 16 threads per worker; with 4 threads 24 users missed them: head-of-line blocking). **Not run:** the security workflow on GitHub (CodeQL and ZAP never executed), an OTLP exporter against a real collector, anything beyond 24 users, a real model under load.

**Phase 5 verification:** 1065 tests pass (CI mode, 95% coverage), ruff/format/strict mypy clean, `E2E_CHAT=1 E2E_SEMANTIC=1 python scripts/e2e_login.py` passed including the ticket flow (create, privacy, agent queue, assign, comment, resolve, close). Not verified: the tickets page and Contact-support dialog in a browser, mobile layout, screen readers.

**Phase 4 results:** with `qwen2.5:3b`, answer eval ([`docs/EVALUATION.md`](docs/EVALUATION.md)): answered 0.926, cites a source 0.833, cites an expected document 0.778, figures supported 0.981, refusals 1.000, restricted pass 0.917, **0 ACL leaks**. Live checks: real model streamed in the browser; with Ollama stopped `/api/chat/stream` sent a clean `model_unavailable` error event.

**Verification evidence (end of Phase 4):** 978 tests (977 + the one fixed after the full run: an inline `style=` the CSP forbids), 95% coverage; ruff, format, strict mypy clean; `E2E_CHAT=1 E2E_SEMANTIC=1 python scripts/e2e_login.py` passed against Keycloak + Postgres + Azurite + worker + Ollama. **Not verified:** CI on GitHub, load/concurrency of streaming (2 workers x 4 threads), mobile layout on a real device, screen-reader behaviour.

**Verification evidence (end of Phase 3):** 837 tests pass with `CI=true` (nothing skipped), 95% coverage; `ruff`, `ruff format --check`, strict `mypy` clean; the exact CI evaluation step passed (twice, even with a competing worker); full-stack `scripts/e2e_login.py` with `E2E_SEMANTIC=1` passed against real Keycloak + Postgres + Azurite + worker + Ollama (including 6 semantic checks and a no-leak-through-paraphrase check); migration `0002` tested up/down with Phase-2-style data. **Not verified:** CI on GitHub (never observed), Python 3.11 locally, any real chat LLM (none configured), groundedness/citation quality (not measurable yet).

## 4. Current development status

**Between phases.** Phase 7 is complete and was handed over for the user to commit. Nothing is in progress. Local machine state: Docker stack may be stopped; **Ollama (portable, installed via winget) may still be running in the background** (`Stop-Process -Name ollama` to stop; exe under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ollama.Ollama.Portable_*`); model `nomic-embed-text` is pulled; a git-ignored `.env` exists with only the embedding settings (no secrets). The dev database `leyline` holds the 49 seeded documents with embeddings.

## 5. Pending tasks

Phase detail and acceptance gates live in [`docs/ROADMAP.md`](docs/ROADMAP.md); this is the actionable backlog.

**Phase-bound**
- **4** done (see Completed work).
- **5, 6** done (see Completed work).
- **6** OpenTelemetry; append-only audit log; SQL-backed monitoring dashboard; threat model + red-team evals; scanners (pip-audit, gitleaks, Trivy, CodeQL, ZAP); Locust load test.
- **7** Bicep IaC, managed identity, Entra (incl. group overage via Microsoft Graph), Azure AI Search adapter, deploy pipeline, runbooks/ADRs.

**Backlog (not tied to a phase)**
- Tests for `services/llm.py` (still ~20% covered; reworked in Phase 4 anyway). It builds its URL as `{endpoint}/openai/v1/`, so a local chat model needs a configurable base URL.
- Speed up the ~6-minute test suite (seeds real Postgres databases; consider pytest-xdist with per-worker databases).
- Decide whether to stop git-ignoring `CLAUDE.md` / `USAGE.md` / `DEVELOPMENT.md` / `DEPLOYMENT.md` (Known issues #9).
- Confirm CI is green on GitHub (new steps: Postgres + Azurite services, retrieval evaluation) and fix anything the first real run reveals.

## 6. Technical decisions

| Area | Chosen | Rejected / alternatives | Why |
| --- | --- | --- | --- |
| Web stack | Flask + vanilla JS (as specified) | FastAPI/React rewrite | User's stack; gunicorn threads suffice for SSE at this scale |
| Auth | App-level OIDC (Authlib, PKCE) | App Service Easy Auth | One code path for Keycloak and Entra; never trust injected identity headers |
| Sessions | Server-side in Redis, opaque id cookie | Flask signed-cookie sessions | Server-controlled expiry/rotation; nothing forgeable client-side |
| Local IdP | Keycloak | A mock IdP | Real token validation, groups and roles claims |
| DB | Postgres + pgvector | Separate vector DB | One store for ACLs, chunks and vectors; Azure AI Search later as an *adapter* |
| DB driver | `pg8000` + SQLAlchemy **2.0.x** | `psycopg`; SQLAlchemy 2.1 | Windows Application Control blocks psycopg's native libpq and SQLAlchemy 2.1's compiled module; pure Python behaves identically everywhere |
| ACL storage | Normalized `document_acl`, queried at search time inside each leg | Copy ACLs into the index | ACL edits/deletes apply immediately; AI Search adapter must still pass the same tests |
| Retrieval | Hybrid: keyword + vector, RRF, floors on the *underlying* signals | Keyword only; vector only; threshold on fused score | Baseline showed vocabulary gaps and overlapping keyword scores; fused RRF scores are relative and cannot say "nothing relevant" |
| Vector size | Fixed `vector(768)` + HNSW | Dimension-less column | ANN index needs a fixed size; 768 fits nomic-embed natively and OpenAI `text-embedding-3` via its `dimensions` option |
| Embedding model | `nomic-embed-text` via Ollama (portable, winget) | LM Studio (not actually installed); OpenAI key; installing Ollama's full installer | Free, offline, signed binary; needs task prefixes (`search_query:` / `search_document:`) |
| CI for hybrid | Record real vectors once into `evals/embeddings.json` (~880 KB) and replay | Run a model in CI; fake vectors | Real-model quality, deterministic, no network; fails loudly if stale |
| Evaluation | Measure first (baseline), strict mode, determinism, leak oracle independent of DB | Eyeballing; tuning on one set | Gains checked on independent halves; thresholds never lowered |
| Job queue | Postgres table, `FOR UPDATE SKIP LOCKED`, leases + fencing token | Celery/Redis/Service Bus | Job state commits atomically with document state; no extra broker |
| DOCX parsing | Own parser on `defusedxml` + `zipfile` | `python-docx` (needs `lxml`) | `lxml` wheels often missing for new Pythons; smaller attack surface |
| Migrations | Alembic, programmatic (no `alembic.ini`), created with the code that uses them | One big upfront schema | Conversation/ticket tables wait for Phases 4/5 |
| Test DB | Real Postgres from compose, migrated by real migrations; fails (not skips) when `CI=true` | `testcontainers`; mocks only | Fewer moving parts; proves migrations work |
| Dependency mgmt | `pip` + pinned `requirements.txt` lock | `uv` | `uv` not installed; lock is regenerated from a clean venv |
| Model tools | The LLM gets **no tools**; ticket creation will be a user-confirmed UI action | Agentic tool use | Removes a class of prompt-injection attacks |
| Rate limiting | Redis sliding window (per user and per IP) | In-process dict; fixed window | Shared across workers/instances; blocked retries don't extend lockout |

## 7. Important configuration

**Setup:** see [`docs/USAGE.md`](docs/USAGE.md) (Docker or host-run, including the semantic-search setup) and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). Quick path: `docker compose up --build` then `docker compose run --rm app python -m leyline.seed`; sign in at `http://localhost:8000/` (demo users and their throwaway password are in `docs/USAGE.md` and `deploy/keycloak/leyline-realm.json`; local Keycloak only).

**Configuration is read only through `src/leyline/config.py`** (pydantic-settings; `.env.example` lists every variable). Names only, never commit values:

| Group | Variables |
| --- | --- |
| Mode | `LEYLINE_ENV` (`local`/`test`/`production`), `FLASK_DEBUG` |
| Chat model (needed for answers) | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` (or the legacy `AZURE_OPENAI_ENDPOINT`/`_API_KEY`/`_DEPLOYMENT`), `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_BREAKER_FAILURES`, `LLM_BREAKER_RESET_SECONDS` |
| Frontend (local only) | `FRONTEND_DEV_SERVER` (Vite dev server for hot reload; production refuses to start with it) |
| Chat behaviour | `LLM_CONTEXT_TOKEN_BUDGET`, `CHAT_HISTORY_MESSAGES`, `CONVERSATION_RETENTION_DAYS` (0 = keep), `INJECTION_BLOCK_SCORE` |
| Identity | `OIDC_METADATA_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_GROUPS_CLAIM`, `OIDC_ROLES_CLAIM` |
| Sessions / edge | `REDIS_URL`, `SESSION_COOKIE_SECURE`, `SESSION_IDLE_SECONDS`, `SESSION_ABSOLUTE_SECONDS`, `TRUSTED_PROXY_COUNT` |
| Data | `DATABASE_URL`, `BLOB_CONNECTION_STRING`, `BLOB_CONTAINER` |
| Embeddings (optional) | `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_API_KEY`, `EMBEDDING_QUERY_PREFIX`, `EMBEDDING_DOCUMENT_PREFIX`, `EMBEDDING_SEND_DIMENSIONS`, `EMBEDDING_DIMENSIONS` (must stay 768), `EMBEDDING_BATCH_SIZE` |
| Search | `SEARCH_TOP_K`, `SEARCH_CANDIDATES`, `SEARCH_RRF_K`, `SEARCH_MIN_SIMILARITY`, `SEARCH_MIN_LEXICAL`, `SEARCH_MAX_CHUNKS_PER_DOCUMENT` |
| Ingestion / worker | `MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES`, `MAX_DOCX_UNCOMPRESSED_BYTES`, `CHUNK_TARGET_WORDS`, `CHUNK_OVERLAP_WORDS`, `INGEST_MAX_ATTEMPTS`, `INGEST_BACKOFF_SECONDS`, `INGEST_LEASE_SECONDS`, `WORKER_POLL_SECONDS`, `WORKER_HEARTBEAT_PATH` |
| Limits / paths | `MAX_MESSAGE_LENGTH`, `MAX_REQUEST_BYTES`, `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_IP_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`, `CORPUS_DIR`, `LOG_DIR` |
| Tests only | `TEST_DATABASE_URL`, `CI`, `LEYLINE_REQUIRE_DB`, `AZURITE_CONNECTION_STRING`, `E2E_SEMANTIC` |

`LEYLINE_ENV=production` refuses to start without model, identity, database and blob settings, with insecure cookies, or with debug on; validation errors never echo values. `.env` is git-ignored. The compose file contains local-only dev credentials and Microsoft's published Azurite dev key; production secrets must come from Key Vault/managed identity (Phase 8). Important coupling: the relevance floors only mean something for the model and prefixes they were calibrated with; the recorded `evals/embeddings.json` is tied to the corpus, chunker, golden set, model and prefixes.

**Runtime/dependencies:** Python >=3.11 (container 3.13, local venv 3.14); key pins in `requirements.txt`: Flask 3.1.3, SQLAlchemy 2.0.57, pg8000 1.31.6, Alembic 1.20.1, Authlib 1.9.5, redis 8.1.0, openai 3.46.0, pgvector 0.5.1, pypdf 6.21.4, azure-storage-blob 12.31.1, gunicorn 26.2.0. Images: `pgvector/pgvector:pg17` (pgvector 0.8.x, needed for iterative scan), `redis:7.4-alpine`, `quay.io/keycloak/keycloak:26.0`, `azurite:3.33.0`, `jaeger:1.62.0` (infra profile only). Ports: app 8000, Keycloak 8080, Postgres 5432, Redis 6379, Azurite 10000 (loopback), Ollama 11434 on the host (containers use `host.docker.internal`).

**Working agreements and environment gotchas (important for any Claude session)**
- **The user stages and commits everything themselves.** Never run `git add`/`git commit`/`git push` (also avoid `git rm --cached`, which stages). At the end of each phase: summarise changes and give a ready-to-paste commit message. Read-only git is fine.
- Work style that has been working: state the plan, build in verified increments, **measure before changing** (baseline first), run the full verification (`CI=true pytest --cov`, `ruff`, `mypy`, real-stack e2e) before declaring done, run CI's exact commands locally before trusting them, and report caveats honestly. Ask only for decisions that change the work, and verify environment claims (e.g. "is X installed?") before stating them.
- **Never overwrite a file you have not checked** (e.g. `.env`); an auto-mode safety check blocked an unchecked overwrite. Use the Write tool, which refuses to clobber unread files.
- **Windows Application Control blocks some compiled files** (mypy's DLLs, SQLAlchemy 2.1, psycopg). Use `python -m <tool>`; mypy is a pure-Python build. Prefer pure-Python dependencies.
- **Docker Desktop must be running** for compose and the integration tests; `docker compose up -d postgres azurite` for tests. Azurite now persists to its volume (`--location /data`); data from before that fix was lost.
- A running worker container shares the dev database with CLI runs; the evals CLI waits for the queue to be idle, but stop the worker when measuring if in doubt.
- Editing tips: `ruff format` rewrites files between reads, so re-`Read` before `Write`; in bash heredoc Python, `\n` inside strings can turn into real newlines and some quotes break the heredoc: write scripts to a file with the Write tool, or use the Edit tool. After changing the Keycloak realm file, recreate Keycloak (`docker compose up -d --force-recreate keycloak`).
- Auto-memory for this project lives in `C:\Users\aruns\.claude\projects\C--cyrus-Projects-Leyline\memory\` (outside the repo), so everything essential is repeated here.

## 8. Known issues and limitations

1. **Small local model quality.** `qwen2.5:3b` omits citations in ~1 in 6 answers and sometimes refuses when the sources answer (see EVALUATION.md). Expected to improve with a larger model; re-run `python -m leyline.evals.answers` after any model/prompt change.
2. **Near-topic out-of-scope questions** can still return loosely related documents ("can I bring my dog to work?" -> visitors/access; "personal income tax" -> expenses): 2 of 12 unanswerable golden questions. A similarity floor cannot fully separate them; the grounding prompt and citations must. Without an embedding model, search is keyword-only (refusal 25%).
3. **Floors are model-specific** (nomic-embed-text + prefixes). Changing the model/prefix requires re-index, a `--sweep`, new env values and updated thresholds; the recorded cache must be re-recorded (needs Ollama). Floors were tuned on the same 78 questions (checked on halves; re-check as the set grows).
4. **A user's own group/role changes lag** until their session ends (<=12 h default); no back-channel logout. Document-side ACL changes and deletions are immediate.
5. **Entra group "overage"** is only detected (`groups_incomplete`, fails closed); resolution through Microsoft Graph is not built. → Phase 8.
6. **Security events are stored synchronously and fail open**: if the `security_events` insert fails the request continues and only the stdout line remains (`EVENT_STORE_FAILED`). Append-only is a trigger; production must also give the app's database role INSERT/SELECT only. The dashboard page is unchanged (newest 25 events, counts by type); no time windows or charts.
7. **Prompt-injection defence is layered but not provable:** signals are scored (only blatant attacks refused), context is delimited and neutralised, the model has no tools, output is redacted and citations verified. A red-team eval suite and threat model are Phase 6.
8. **Parsers run unsandboxed in the worker** (inputs are bounded; the job lease limits damage).
9. **Frontend gaps:** no browser-driven end-to-end tests; the axe audit is manual (steps in `docs/FRONTEND.md`); the Keycloak login theme only applies locally (Entra ID in production is branded in the Entra admin centre; what its tier allows was not checked); dark theme only; `npm audit` reports 10 moderate findings in the dev-only ESLint tree (not shipped). Keycloak keeps its SSO session when you switch demo users in one browser (the realm is re-imported whenever the container is recreated, so `docker compose up -d --force-recreate keycloak` also resets it): end it at `http://localhost:8080/realms/leyline/protocol/openid-connect/logout` first.
10. **Required checks are your setting:** `.github/workflows/security.yml` only blocks merges if you mark its jobs as required in branch protection. It, `ci.yml` and Dependabot have never been seen running on GitHub.
11. **Documentation hygiene:** `.gitignore` ignores `CLAUDE.md`, `USAGE.md`, `DEVELOPMENT.md`, `DEPLOYMENT.md` by bare name. The three docs are tracked but `CLAUDE.md` is **untracked, so a fresh clone lacks it**, and any *new* file with those names would be silently ignored. Recommend removing those four lines from `.gitignore` and committing `CLAUDE.md` (user's call).
12. **CI has never been observed on GitHub** (`gh` is not authenticated here). It now has Postgres/Azurite services and an evaluation step; Python 3.11 compatibility is untested locally.
13. **The full test suite takes ~6 minutes** (real Postgres seeding); the CI job will be slow until parallelised.
14. Retrieval quality is CI-gated; **answer quality is measured manually** (needs a chat model; not in CI). The eval cache `evals/embeddings.json` (~880 KB) is a generated file committed on purpose.
15. Dev-only conveniences (shared demo passwords, plain HTTP Keycloak, Postgres dev password, public Azurite key) are for local use only. `kb_admin` can see titles/ACLs of restricted documents (accepted; never content).
16. The host-run app on port 5000 is not a registered Keycloak redirect URI (documented workaround). Local Postgres holds scratch databases `leyline_test` / `leyline_scratch_test` (recreated by tests).

## 9. Next development phases (prioritised)

1. ~~**Phase 4 - chat experience**~~ (done). Original order, for reference: In order: (a) LLM resilience + a configurable chat-model backend (fixes Known issue #1 and unblocks everything else); (b) conversation/message/feedback migration `0003` and persistence; (c) SSE streaming + cancel; (d) structured citations (use `SearchHit.title/heading/chunk_id`) + feedback; (e) guardrails (delimit untrusted context, replace the denylist with scoring/logging, verify citations against retrieved chunks); (f) follow-up query rewriting + token budget; (g) UI rebuild; (h) groundedness/citation evals added to the harness.
2. ~~Phases 5-7 (tickets, hardening/observability, frontend)~~ (done). **Phase 8 - Azure (next)** (needs a subscription; confirm budget/access with the user first).
3. **Cross-cutting, every phase:** update `docs/ROADMAP.md` status, keep `docs/ARCHITECTURE.md` accurate, add allowed-path *and* denied-path tests for anything security-relevant, never lower eval floors, keep the production-grade gates in `docs/ROADMAP.md` in view (zero ACL leaks, >=85% coverage, no secrets, scanner-clean, repeatable deploy).

## 10. Immediate next step

**Phase 8 (Azure) is in progress, nothing deployed.** Written and verified (uncommitted): `infra/` (Bicep + `deploy.ps1`/`teardown.ps1`; compiles clean, `what-if` shows 23 creates, `validate` passes for both passes), `tests/test_infra.py`, and `LLM_SEND_TEMPERATURE` (GPT-5 models reject a custom temperature). Choices: Korea Central, Container Apps, Postgres+pgvector search (no AI Search), `gpt-5.4-mini` + `text-embedding-3-small`, Azure Managed Redis B0, Azure for Students subscription (policy allows centralindia, eastasia, uaenorth, koreacentral, malaysiawest), Entra tenant is Free tier and the user is Global Administrator. The Azure CLI and Bicep compiler are installed on the user's machine (`az` is at `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin`). Resource providers were registered (App, ContainerRegistry, DBforPostgreSQL, Cache, KeyVault, Storage, OperationalInsights, Insights). **Next:** Entra app registration + groups/roles script, a budget alert, then the first real deploy (ask before spending), re-record `evals/embeddings.json` for text-embedding-3-small and re-sweep the relevance floors, CI deploy workflow, runbooks.


**Phase 8 (Azure deployment) needs things only the user can provide, so start by asking.** Before any Bicep: does the user now have an Azure subscription, which region, and what monthly budget ceiling? Without a subscription there is nothing to deploy; the useful local alternatives are (a) review and commit what exists, push, and watch the first real CI and security-workflow runs on GitHub (fix whatever they reveal, and mark the security jobs as required checks), or (b) prepare Phase 8 offline: Bicep modules validated with `az bicep build`/`what-if` where possible, an Azure AI Search `SearchIndex` adapter that must pass `tests/test_search_contract.py` and the eval gate, Entra group-overage handling via Microsoft Graph, and runbooks/ADRs.

Session start checklist: `git log`/`git status` (has Phase 7 landed?) -> `cd frontend && npm ci && npm run check` (needs Node 22) -> -> start Docker Desktop (`%LOCALAPPDATA%\Programs\DockerDesktop`) -> `docker compose up -d postgres azurite` -> activate `.venv` -> `CI=true python -m pytest -q` (expect ~1105 passed, ~6 min), `python -m ruff check .`, `python -m mypy`. Start Ollama if needed (`nomic-embed-text`, `qwen2.5:3b`) and `docker compose up -d --build`.

Plan (from `docs/ROADMAP.md`): Bicep for App Service (web + worker), Postgres, Redis, Key Vault, Blob, AI Search, Azure OpenAI, Log Analytics, Application Insights; managed identity everywhere; Entra app registration and group mapping including overage; Azure AI Search adapter; GitHub Actions with OIDC federation (build, scan, staging slot, smoke test, swap); budget alerts and one-command spin-up/teardown; runbooks and ADRs; drop the "educational lab" wording only after the production-grade gates pass. Check costs against the Azure pricing calculator first.

**Done when:** a fresh deploy from an empty resource group passes the full verification (Playwright/e2e and eval suites against staging through real Entra login).
