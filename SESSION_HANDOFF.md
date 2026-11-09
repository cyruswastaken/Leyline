# Session handoff

> **Last updated:** 2026-11-09, after Phase 3. Phases 0-2 are committed and pushed (`5d65907`). **Phase 3 was handed to the user uncommitted** (they stage and commit); check `git log` / `git status` first to see whether it has landed.
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
       ├─ /chat: rate limit → input rules → hybrid SearchIndex.search(principal) → authorize_hits → LLM
       ├─ /admin/*: kb_admin API + page  ──►  IngestionService
       └─ Postgres (+pgvector): documents, versions, document_acl, chunks(+vector(768), HNSW), ingestion_jobs, audit_events
  Worker (same image): claims jobs → blob store → parse → chunk → embed → swap chunks in one tx
  Blob store: Azurite (local) / Azure Blob;  Embeddings: Ollama nomic-embed-text (local) / any OpenAI-compatible endpoint
  LLM: Azure OpenAI "v1" Responses API via the `openai` SDK (src/leyline/services/llm.py), not configured locally
  Evals: src/leyline/evals + evals/ (golden questions, thresholds, recorded embeddings) → CI gate
```

- **Frontend:** three server-rendered pages in `src/leyline/templates/` (`index.html` chat, `monitoring.html`, `admin_documents.html`); no build step. Data is rendered with `textContent` only.
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
| 3 | Golden set (78 questions) + eval harness + CI gate; migration `0002` (`vector(768)` + HNSW); hybrid search with RRF, relevance floors, doc cap; contract suite; record/replay of real embeddings for offline CI; Ollama + nomic-embed-text | **built, uncommitted** |

**Phase 3 results** ([`docs/EVALUATION.md`](docs/EVALUATION.md)): recall@5 0.796 -> **1.000**, MRR 0.640 -> **0.936**, recall@1 0.519 -> 0.889, correct refusals 25% -> **83%**, restricted pass 1.000, **0 ACL leaks** (gains confirmed on independent halves of the set).

**Verification evidence (end of Phase 3):** 837 tests pass with `CI=true` (nothing skipped), 95% coverage; `ruff`, `ruff format --check`, strict `mypy` clean; the exact CI evaluation step passed (twice, even with a competing worker); full-stack `scripts/e2e_login.py` with `E2E_SEMANTIC=1` passed against real Keycloak + Postgres + Azurite + worker + Ollama (including 6 semantic checks and a no-leak-through-paraphrase check); migration `0002` tested up/down with Phase-2-style data. **Not verified:** CI on GitHub (never observed), Python 3.11 locally, any real chat LLM (none configured), groundedness/citation quality (not measurable yet).

## 4. Current development status

**Between phases.** Phase 3 is complete and was handed over for the user to commit. Nothing is in progress. Local machine state: Docker stack may be stopped; **Ollama (portable, installed via winget) may still be running in the background** (`Stop-Process -Name ollama` to stop; exe under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Ollama.Ollama.Portable_*`); model `nomic-embed-text` is pulled; a git-ignored `.env` exists with only the embedding settings (no secrets). The dev database `leyline` holds the 49 seeded documents with embeddings.

## 5. Pending tasks

Phase detail and acceptance gates live in [`docs/ROADMAP.md`](docs/ROADMAP.md); this is the actionable backlog.

**Phase-bound**
- **4** Conversation/message/feedback migration (`0003`); LLM resilience (timeouts, retries, circuit breaker, clean degraded mode: fixes the generic 500); SSE streaming + cancel; structured citations + feedback; layered prompt-injection guardrails (replace the 13-phrase denylist; delimit retrieved text as untrusted); follow-up query rewriting; UI rebuild; **groundedness and citation-accuracy evals** (deferred from Phase 3).
- **5** Ticket escalation (`TicketProvider` port, built-in queue, agent view).
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
| Model (needed for `/chat` answers) | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` |
| Identity | `OIDC_METADATA_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_GROUPS_CLAIM`, `OIDC_ROLES_CLAIM` |
| Sessions / edge | `REDIS_URL`, `SESSION_COOKIE_SECURE`, `SESSION_IDLE_SECONDS`, `SESSION_ABSOLUTE_SECONDS`, `TRUSTED_PROXY_COUNT` |
| Data | `DATABASE_URL`, `BLOB_CONNECTION_STRING`, `BLOB_CONTAINER` |
| Embeddings (optional) | `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_API_KEY`, `EMBEDDING_QUERY_PREFIX`, `EMBEDDING_DOCUMENT_PREFIX`, `EMBEDDING_SEND_DIMENSIONS`, `EMBEDDING_DIMENSIONS` (must stay 768), `EMBEDDING_BATCH_SIZE` |
| Search | `SEARCH_TOP_K`, `SEARCH_CANDIDATES`, `SEARCH_RRF_K`, `SEARCH_MIN_SIMILARITY`, `SEARCH_MIN_LEXICAL`, `SEARCH_MAX_CHUNKS_PER_DOCUMENT` |
| Ingestion / worker | `MAX_UPLOAD_BYTES`, `MAX_PDF_PAGES`, `MAX_DOCX_UNCOMPRESSED_BYTES`, `CHUNK_TARGET_WORDS`, `CHUNK_OVERLAP_WORDS`, `INGEST_MAX_ATTEMPTS`, `INGEST_BACKOFF_SECONDS`, `INGEST_LEASE_SECONDS`, `WORKER_POLL_SECONDS`, `WORKER_HEARTBEAT_PATH` |
| Limits / paths | `MAX_MESSAGE_LENGTH`, `MAX_REQUEST_BYTES`, `RATE_LIMIT_REQUESTS`, `RATE_LIMIT_IP_REQUESTS`, `RATE_LIMIT_WINDOW_SECONDS`, `CORPUS_DIR`, `LOG_DIR` |
| Tests only | `TEST_DATABASE_URL`, `CI`, `LEYLINE_REQUIRE_DB`, `AZURITE_CONNECTION_STRING`, `E2E_SEMANTIC` |

`LEYLINE_ENV=production` refuses to start without model, identity, database and blob settings, with insecure cookies, or with debug on; validation errors never echo values. `.env` is git-ignored. The compose file contains local-only dev credentials and Microsoft's published Azurite dev key; production secrets must come from Key Vault/managed identity (Phase 7). Important coupling: the relevance floors only mean something for the model and prefixes they were calibrated with; the recorded `evals/embeddings.json` is tied to the corpus, chunker, golden set, model and prefixes.

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

1. **Model failures return a generic 500** (no degraded mode); `services/llm.py` is barely tested and hard-wires the Azure URL shape. No chat model is configured locally, so real answer generation has never been run. → Phase 4.
2. **Near-topic out-of-scope questions** can still return loosely related documents ("can I bring my dog to work?" -> visitors/access; "personal income tax" -> expenses): 2 of 12 unanswerable golden questions. A similarity floor cannot fully separate them; the grounding prompt and citations must. Without an embedding model, search is keyword-only (refusal 25%).
3. **Floors are model-specific** (nomic-embed-text + prefixes). Changing the model/prefix requires re-index, a `--sweep`, new env values and updated thresholds; the recorded cache must be re-recorded (needs Ollama). Floors were tuned on the same 78 questions (checked on halves; re-check as the set grows).
4. **A user's own group/role changes lag** until their session ends (<=12 h default); no back-channel logout. Document-side ACL changes and deletions are immediate.
5. **Entra group "overage"** is only detected (`groups_incomplete`, fails closed); resolution through Microsoft Graph is not built. → Phase 7.
6. **Monitoring and security events** are written to a local file (`logs/security_events.log`) and read back with `ast.literal_eval`; single-instance only, newest 10 events shown, the summary ignores newer event types. `audit_events` is separate and not append-only. → Phase 6.
7. **Prompt-injection defence is weak:** a 13-phrase denylist; retrieved text is not delimited as untrusted. → Phase 4/6.
8. **Parsers run unsandboxed in the worker** (inputs are bounded; the job lease limits damage).
9. **Documentation hygiene:** `.gitignore` ignores `CLAUDE.md`, `USAGE.md`, `DEVELOPMENT.md`, `DEPLOYMENT.md` by bare name. The three docs are tracked but `CLAUDE.md` is **untracked, so a fresh clone lacks it**, and any *new* file with those names would be silently ignored. Recommend removing those four lines from `.gitignore` and committing `CLAUDE.md` (user's call).
10. **CI has never been observed on GitHub** (`gh` is not authenticated here). It now has Postgres/Azurite services and an evaluation step; Python 3.11 compatibility is untested locally.
11. **The full test suite takes ~6 minutes** (real Postgres seeding); the CI job will be slow until parallelised.
12. Retrieval quality is measured, but **groundedness, citation accuracy and answer quality are not** (need Phase 4). The eval cache `evals/embeddings.json` (~880 KB) is a generated file committed on purpose.
13. Dev-only conveniences (shared demo passwords, plain HTTP Keycloak, Postgres dev password, public Azurite key) are for local use only. `kb_admin` can see titles/ACLs of restricted documents (accepted; never content).
14. The host-run app on port 5000 is not a registered Keycloak redirect URI (documented workaround). Local Postgres holds scratch databases `leyline_test` / `leyline_scratch_test` (recreated by tests).

## 9. Next development phases (prioritised)

1. **Phase 4 - chat experience (next; the biggest user-visible gap).** In order: (a) LLM resilience + a configurable chat-model backend (fixes Known issue #1 and unblocks everything else); (b) conversation/message/feedback migration `0003` and persistence; (c) SSE streaming + cancel; (d) structured citations (use `SearchHit.title/heading/chunk_id`) + feedback; (e) guardrails (delimit untrusted context, replace the denylist with scoring/logging, verify citations against retrieved chunks); (f) follow-up query rewriting + token budget; (g) UI rebuild; (h) groundedness/citation evals added to the harness.
2. **Phase 5 - ticket escalation**, then **Phase 6 - hardening/observability**, then **Phase 7 - Azure** (needs a subscription; confirm budget/access with the user first).
3. **Cross-cutting, every phase:** update `docs/ROADMAP.md` status, keep `docs/ARCHITECTURE.md` accurate, add allowed-path *and* denied-path tests for anything security-relevant, never lower eval floors, keep the production-grade gates in `docs/ROADMAP.md` in view (zero ACL leaks, >=85% coverage, no secrets, scanner-clean, repeatable deploy).

## 10. Immediate next step

**Start Phase 4, step (a): make the answer-generation path resilient and runnable locally.**

Session start checklist: check `git log`/`git status` (has Phase 3 been committed?) → start Docker Desktop → `docker compose up -d postgres azurite` → activate `.venv` → `CI=true python -m pytest -q` (expect **837 passed**, ~6 min), `python -m ruff check .`, `python -m mypy` (all clean). If Ollama is not running, start it (see §4).

Then:
1. **Ask the user which chat model to use locally** (decision pending). Ollama is already installed, so a small local model is the cheapest option (the machine has an RTX 3050 with 4 GB, so roughly a 3B-parameter model, e.g. `llama3.2:3b` or `qwen2.5:3b`, ~2 GB download); alternatives are an OpenAI API key (user keeps it in `.env`) or waiting for Azure OpenAI. Do not download or install anything until they answer.
2. Refactor `src/leyline/services/llm.py` behind a small `LLM` port (the codebase already uses ports for blob/embeddings/search): configurable base URL and API style (so Azure `/openai/v1/`, OpenAI and Ollama's OpenAI-compatible API all work), explicit timeouts, bounded retries, a circuit breaker, and a **clean degraded mode**: a model outage must return a clear 503-style message and log a `MODEL_ERROR` event, never a generic 500.
3. Test it the same way the embeddings adapter is tested (a local fake HTTP server speaking the API, in `tests/test_adapters_and_cli.py` style): success, timeout, 5xx with retry, circuit open, malformed response, and that the chat route degrades cleanly and keeps ACL behaviour unchanged.
4. Raise `services/llm.py` coverage from ~20%, update `docs/ARCHITECTURE.md`/`USAGE.md`, and add a `MODEL_ERROR` row to the monitoring summary.

**Done when:** a question works end to end against a real local model; with the model stopped, `/chat` answers with a clear degraded-mode message (verified by test and by the live stack), full verification is green, and the user has a commit message (they commit).
