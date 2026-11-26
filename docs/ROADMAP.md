# Leyline roadmap: path to production grade

## Context

Leyline is a Flask RAG helpdesk that answers IT questions from an internal corpus and enforces document access by role. It is about 1,000 lines of Python, 5 sample documents, and no tests (7 commits, last on 2026-09-06). The docs describe an Azure deployment with Entra ID, AI Search and Blob Storage, but none of that exists in code.

**Goal:** real production engineering (real login, permissions enforced at retrieval, CI/CD, IaC, evals, observability), run with synthetic data and demo users. No formal compliance or on-call work.

**Decisions made with you**

| Topic | Decision |
| --- | --- |
| End goal | Portfolio project built to true production standards |
| Azure | No subscription yet, so build **local-first behind swappable adapters** and deploy to Azure in the last phase |
| v1 features | Conversations + streaming, citations + feedback, document admin + ingestion, ticket escalation |
| Permissions | Per-document Entra-group ACLs, with app roles for app functions |

## Current state audit

Found by reading the code in the initial review (2026-11-09). Items marked **Fixed** were resolved in Phase 0.

### What is worth keeping
- Authorization runs before retrieval, and unknown roles get nothing (`services/authorization.py`). The principle stays; the implementation moves into the search query.
- The grounding and anti-injection rules in the system prompt (`services/LLM.py:60-86`).
- The OpenAI-compatible client pattern (`services/LLM.py:39`), which already lets one SDK talk to Azure, OpenAI or a local model.
- `chunk_document` overlap logic as a starting point, the security event vocabulary, and the dashboard layout.

### Critical: these defeat the product's whole purpose
| # | Problem | Where |
| --- | --- | --- |
| 1 | `GET /documents` returns **every document's full text, including restricted**, to anyone | `app.py:87` |
| 2 | Role comes from the URL, so `/documents/security_admin` and `/search/security_admin?q=...` bypass all access control | `app.py:94,106` |
| 3 | `app.run(debug=True)` exposes the Werkzeug debugger (remote code execution if reachable) | `app.py:135` |
| 4 | No authentication; every user is a hardcoded `local-user` / `employee` | `app.py:46,49` |
| 5 | Monitoring pages and security logs are public | `app.py:68-84` |
| 6 | Stored XSS: log fields go into `innerHTML` on the admin dashboard | `templates/monitoring.html:1003` |
| 7 | A document body line starting `Access Level:` overwrites the header, so content can **downgrade its own classification** | `services/document_loader.py:45` |

**Status:** items 1, 2, 3, 6 and 7 were **fixed in Phase 0** and items 4 and 5 (no authentication, public monitoring) in **Phase 1**, all with regression tests (`tests/test_regressions.py`, `tests/test_access_matrix.py`).

### Bugs: these work differently from what the docs claim
- **Rate limits and blocked requests return 500, not 429/403.** `chat_service` returns no `error` or `reason` keys, so `app.py:59` raises `KeyError`. The event is still logged, so the dashboard looks fine.
- A non-string `message` crashes with a 500, and there is no length limit (`app.py:38`).
- `retrieval_requests` is always 0: the `RETRIEVAL` event is never emitted and `latency_ms` is computed then discarded (`services/retrieval.py:266`).
- Any single keyword match counts as relevant (`retrieval.py:251`), so "no answer" almost never triggers and weak context reaches the model.
- A missing env var crashes the whole app at import, including `/health` (`LLM.py:23-36`).
- LLM calls have no error handling or sensible timeout (`LLM.py:102`).

**Phase 0 status:** 403/429 responses, input validation, the missing `RETRIEVAL` event and latency, the import-time crash and LLM timeouts/retries are **fixed**. The weak relevance threshold remains for Phase 3.

### Not production-ready by design
- Corpus re-read and re-chunked on every request (`chat_service.py:97`).
- In-memory rate limiter: per-worker, unbounded, not thread-safe.
- Monitoring depends on a local log file parsed with `ast.literal_eval` and re-read every 5 seconds; this breaks on multiple instances and on App Service restarts.
- Prompt-injection defence is a 13-phrase substring list. It is easy to bypass and blocks legitimate questions.
- No tests, CI, Dockerfile, migrations, database or IaC. `openai` is unpinned, `python-dotenv` is listed twice.
- Doc drift: `docs/rag-helpdesk-lab-guide.txt` is referenced throughout but gitignored and not on disk; README says MIT but there is no LICENSE; Python 3.11 in docs, 3.14 on this machine.

**Status:** the corpus is no longer re-read per request (documents are indexed once into Postgres, Phase 2), the rate limiter is now Redis-backed and shared across instances (Phase 1), and the log-file monitoring remains (Phase 6). Dependencies are pinned, tests, CI, a Dockerfile and compose now exist, and docs have been corrected; the missing lab guide is replaced in Phase 8.

## Target architecture

```text
Browser (server-rendered HTML + vanilla JS, SSE streaming)
   |
Flask API (gunicorn) ---- Redis: sessions, rate limits
   |   OIDC login: Keycloak locally, Entra ID on Azure
   |
   +-- Chat pipeline: limit > validate > rewrite > ACL-filtered search
   |                  > re-authorize > generate > verify > audit
   +-- Postgres: documents, ACLs, conversations, feedback, tickets, audit, jobs
   +-- Ports:  SearchIndex | LLM | Embeddings | BlobStore | TicketProvider
   |
Ingestion worker (same image): parse > chunk > embed > index
```

| Port | Local | Azure |
| --- | --- | --- |
| Identity (standard OIDC) | Keycloak container | Entra ID |
| LLM / embeddings (OpenAI-compatible) | Ollama or OpenAI key; fake in tests | Azure OpenAI |
| SearchIndex | Postgres + pgvector + full-text | Azure AI Search |
| BlobStore (Azure SDK) | Azurite emulator | Blob Storage |
| Database / cache | Postgres, Redis containers | Azure Database for PostgreSQL, Azure Cache for Redis |
| Telemetry (OpenTelemetry) | Jaeger / console | Azure Monitor |
| Secrets | `.env` | Key Vault + managed identity |

**Technical choices and why**
- **Keep Flask and vanilla JS** as your stack specifies. gunicorn threaded workers handle SSE fine at this scale.
- **Python 3.13**, pinned in the container. Dependencies locked with `uv`.
- **App-level OIDC instead of App Service Easy Auth** (a change from your docs). The same code path then runs locally and on Azure, and the app never trusts injected identity headers.
- **Postgres job table** (`FOR UPDATE SKIP LOCKED`) for ingestion, not a separate broker: job state commits atomically with document state.
- **Built-in ticket queue first**, behind `TicketProvider`, so escalation works with no external accounts. ServiceNow and Jira can be added later as adapters.
- **The model gets no tools.** Ticket creation is a user-confirmed UI action, which removes a whole class of injection attacks.

**Authorization design (the core of the project)**
1. Identity comes only from the verified server-side session, never from a request parameter.
2. `SearchIndex.search()` requires a `Principal`, so an unfiltered search cannot be written. The ACL filter is built in one audited function.
3. Each chunk stores its allowed group IDs. A document with an empty ACL is visible to nobody.
4. Before anything reaches the model, each hit is re-checked against Postgres, so revocations and deletions apply immediately even if the index lags.
5. Handle Entra's group-overage case: past the token claim limit, groups must be fetched from Microsoft Graph.
6. Conversations and tickets are owner-scoped, and citations are re-authorized each time they are displayed.

## Phases

Rough effort assumes one developer working part-time. Each phase ends in a working, tested state.

### Phase 0: Stop the bleeding and lay foundations (about 1 week)
- Delete `/documents*` and `/search/<role>` debug routes, turn off debug, fix the `KeyError` responses, add input validation and a message length limit.
- Fix the XSS (`textContent`) and make document metadata header-only.
- Restructure into `src/leyline/`: app factory, blueprints, `ports/`, `adapters/`, typed config (`pydantic-settings`) that validates at startup.
- Add `docker-compose` (Postgres, Redis, Keycloak, Azurite, Jaeger), a multi-stage non-root Dockerfile, and split `/healthz` and `/readyz`.
- Tooling: ruff, mypy, pytest + coverage, pre-commit, and GitHub Actions CI.
- Add LICENSE, `.env.example` and CLAUDE.md; correct docs that overclaim.
- Write regression tests for every audit item first.

**Done when:** `docker compose up` runs the app, CI is green, and every critical audit item has a passing regression test.

### Phase 1: Identity and access (about 1-2 weeks)
- OIDC authorization code + PKCE (Authlib), server-side sessions in Redis, secure cookies, logout.
- `Principal` model, app roles (`user`, `kb_admin`, `support_agent`, `security_auditor`), route decorators, deny by default.
- CSRF protection, CSP and security headers; distributed rate limiting per user and per IP.
- Seed Keycloak with demo users and groups (HR, Finance, Security, staff).

**Done when:** a matrix test proves every role against every route, for both allowed and denied paths.

**Phase 1 status: complete.** Delivered as specified, plus: the route matrix fails when a route has no declared policy; sessions have idle and absolute timeouts, rotate at login, and fail closed (503) when Redis is down; CSP uses per-request nonces; and `scripts/e2e_login.py` (run in CI) drives the real login flow against Keycloak for every demo user. Two items moved: per-request re-authorization against the database landed in Phase 2 (it applies *document-side* changes immediately; a user's own group changes still lag until their session ends, which is documented), and Entra group-overage resolution via Microsoft Graph lands in Phase 8.

### Phase 2: Data and ingestion (about 2 weeks)
- Alembic migrations for `documents`, `document_versions`, `document_acl`, `chunks`, `ingestion_jobs`, `audit_events` (conversation and ticket tables follow in Phases 4 and 5).
- Worker: upload to blob, validate type and size, parse PDF/DOCX/Markdown/TXT, chunk by heading structure, embed in batches, index.
- Idempotent by content hash, retries with backoff, failed-job visibility. Delete and re-version remove old chunks.
- Admin UI: upload, assign ACL groups, status, versions, re-index, delete.
- Record embedding model and dimension per index so switching providers forces a re-index.
- Migrate the 5 sample documents and add a larger synthetic corpus (about 50-100 documents across departments) so retrieval and ACLs are tested meaningfully.

**Done when:** an upload becomes searchable, and a deleted document is unretrievable, both verified automatically.

**Phase 2 status: complete.** Delivered as specified (migrations, validated multi-format ingestion, heading-aware chunking, idempotent upload, safe re-versioning, retrying job queue with leases, admin UI, per-document group ACLs applied inside the search query and re-checked before use, a 49-document synthetic corpus, and the embedding model recorded per version). Differences from the plan:
- **Migrations are created with the code that uses them.** This phase's migration covers documents, versions, ACLs, chunks, jobs and audit events. The `conversations`, `messages`, `feedback`, `tickets` and `ticket_events` tables arrive in Phases 4 and 5, where their shape is known.
- **Retrieval is Postgres full-text search for now.** Chunks already store embeddings (when a provider is configured) but vector and hybrid ranking, thresholds and evaluation are Phase 3. The embedding column is dimension-less until a model is chosen.
- **`pg8000` instead of `psycopg`.** A pure-Python driver runs identically on locked-down machines (psycopg's native libpq is blocked by some Windows application-control policies), CI and the container.
- **Embeddings are optional.** Any OpenAI-compatible endpoint works; without one, documents are keyword-indexed and can be re-indexed later (the admin page flags them).
- **Added beyond the plan:** a worker heartbeat health check, bounded Azure retries (so `/readyz` answers in seconds), an `audit_events` trail for administrative actions, and a migration CLI with autogenerate.

### Phase 3: Retrieval and evaluation (about 1-2 weeks)
- Hybrid search: pgvector + Postgres full-text, merged with reciprocal rank fusion, ACL filter applied inside the query.
- Relevance threshold, a per-document result cap, and the Postgres re-authorization step.
- A contract test suite that every `SearchIndex` adapter must pass, ready for AI Search.
- Eval harness with a golden question set: recall@k, MRR, groundedness, citation accuracy, correct refusals.

**Done when:** baseline metrics are recorded, and CI fails on a retrieval regression or on **any** ACL leak.

**Phase 3 status: complete.** Delivered as specified: hybrid search (pgvector + Postgres full-text, reciprocal-rank fusion) with the ACL predicate inside both legs, relevance floors, a per-document cap, the database re-authorization step, a parametrized `SearchIndex` contract suite, an evaluation harness with a 78-question golden set, and a CI gate that fails on a regression or any ACL leak. Results ([EVALUATION.md](EVALUATION.md)): recall@5 0.796 -> 1.000, MRR 0.640 -> 0.936, correct refusals 25% -> 83%, zero ACL leaks. Differences from the plan:
- **Baseline first.** The harness was built and the keyword baseline recorded before any retrieval change, so every gain is measured.
- **Offline hybrid evals.** Real embeddings are recorded once into `evals/embeddings.json` and replayed in CI (no model needed there). The cache must be re-recorded when the corpus, chunker, model or prefixes change.
- **Embedding model: `nomic-embed-text` via Ollama** (768 dimensions, fixed in the schema by migration `0002`, which also adds an HNSW index and flags stale vectors for re-index). OpenAI/Azure `text-embedding-3` works at 768 dimensions.
- **Groundedness and citation accuracy are not measured yet.** They need the answer-generation path and structured citations (Phase 4); this phase measures retrieval, refusal and permission safety.
- **Found along the way:** Azurite was not persisting blobs (volume not used), tie-breaking by random ids made results irreproducible, and the keyword floor must not apply when there is no vector leg.

### Phase 4: Chat experience (about 2 weeks)
- Persistent conversations, follow-up query rewriting, token budgeting, retention setting.
- SSE streaming with cancel; timeouts, retries and a circuit breaker around the LLM; clear degraded-mode messages.
- Structured citations with clickable sources, and thumbs up/down with reason.
- Guardrails: retrieved text clearly delimited as untrusted, score-and-log injection signals in place of the 403 denylist, citations verified against what was actually retrieved, credential-pattern redaction on a rolling stream buffer.
- UI rebuild on vanilla JS: conversation sidebar, streaming, safe Markdown rendering, accessibility, mobile.

**Done when:** a multi-turn cited conversation streams end to end, and LLM failures degrade cleanly.

**Phase 4 status: complete.** Delivered: a `ChatModel` port with an OpenAI-compatible adapter (Ollama, OpenAI, Azure), timeouts, bounded retries and a circuit breaker with a 503 degraded mode; migration `0003` (conversations, messages, feedback) with owner-scoped, retention-aware storage; SSE streaming with cancel; structured, verified, re-authorized citations; thumbs up/down with a reason; follow-up rewriting from the user's own questions; context token budgeting; guardrails (scored injection signals, delimited untrusted context, citation verification, rolling-buffer credential redaction); a rebuilt vanilla-JS UI (sidebar, streaming, safe Markdown, keyboard and screen-reader support, mobile layout); and an answer-quality evaluation (`python -m leyline.evals.answers`). Differences from the plan:

- **Answer quality is gated manually, not in CI.** It needs a chat model, so `leyline.evals.answers` records results in [EVALUATION.md](EVALUATION.md) instead of failing the build; retrieval and ACL safety remain CI-gated.
- **The 403 denylist was replaced by scoring**, but blatant attacks (an override plus a request for the system prompt) are still refused.
- **`/chat` (stateless JSON) is kept** next to the streaming API for scripts and tests.

### Phase 5: Ticket escalation (about 1 week)
- "Escalate" action with a pre-filled summary the user reviews before sending.
- `TicketProvider` port and built-in queue; agent view with status, assignment and comments.
- Tickets contain only what the user approved, so restricted context cannot leak to agents who lack access.

**Done when:** the full flow from failed answer to resolved ticket passes an end-to-end test.

**Phase 5 status: complete.** Delivered: migration `0004` (tickets, ticket events), a `TicketProvider` port with the built-in Postgres queue, a "Contact support" draft the user edits before sending (built only from their own words), a ticket page for requesters and an agent queue (assign, comment, status), per-user creation rate limit, credential scrubbing, security events for ticket actions, access-matrix entries for every new route, and a full-flow test plus a real-stack check. Choices made along the way:

- **No internal notes:** every comment is visible to both requester and agents, so nothing hidden can be mistaken for private.
- **No conversation link:** a ticket does not point back to the conversation, so agents cannot reach it; users who want agents to see an answer paste it into the ticket themselves.
- **No external ticketing system yet:** ServiceNow/Jira are future `TicketProvider` implementations.

### Phase 6: Hardening and observability (about 1-2 weeks)
- OpenTelemetry traces and metrics: per-stage latency, tokens, cost, no-answer rate, feedback score. JSON logs to stdout, with prompts excluded by default.
- Append-only audit log; rebuild the security dashboard on SQL with role-gated access.
- Written threat model (OWASP LLM Top 10 and STRIDE) and a red-team eval suite for injection, exfiltration and cross-user access.
- CI security: pip-audit, gitleaks, Trivy, CodeQL, ZAP baseline. Load test with Locust against defined latency targets.

**Done when:** the threat model is written, security scans block merges, and load targets are met.

**Phase 6 status: complete, with two deviations and one repository setting for you.** Delivered: append-only `security_events` in Postgres with a SQL-backed auditor dashboard (replacing the log file and `ast.literal_eval`), JSON events on stdout, OpenTelemetry traces and metrics (off by default; console or OTLP), system-prompt leak detection, a written [threat model](THREAT_MODEL.md) mapped to STRIDE and the OWASP LLM Top 10, a red-team suite (`tests/test_redteam.py`) built around a fully compromised model, a security workflow (pip-audit, gitleaks, Trivy image and config, CodeQL, ZAP baseline) plus Dependabot, a hardened single-layer container image that scans clean, and a Locust load test with defined targets ([LOAD_TEST.md](LOAD_TEST.md)). Notes:

- **Scans block merges only once they are required** by a rule on the repository; the workflow cannot do that itself. (When written it had not yet run on GitHub; it has since, and is green on `main` as of 2026-11-19 after the Phase 8 fixes.) Since 2026-11-19 a ruleset on `main` requires all ten CI and Security jobs (see the Phase 8 notes below).
- **Dashboard shape:** the page is unchanged; the SQL back end serves the same fields. A richer dashboard (time windows, charts) was not built.
- **Cost** is not metered (tokens are estimates) and the load test measures the app with a fake model, not model capacity.

### Phase 7: Frontend design system (about 1.5-2 weeks)
- A shared design system taken from a reference mock-up: near-black grid, neon lime accent, pixel display type, uppercase monospace chrome, braced `{BUTTONS}`, a pixel-art globe.
- A real frontend build (Vite, TypeScript, Tailwind) replacing four hand-written pages with inline styles and scripts.
- Every page on one shared layout: a public landing page, chat, tickets and the agent queue, knowledge-base admin, monitoring, and error pages.
- Accessibility, mobile layout, reduced-motion support, and a strict CSP that needs no inline exceptions.

**Done when:** all pages use the shared design system with no inline CSS or JavaScript, the frontend checks run in CI, and an automated accessibility audit of every page finds no violations.

**Phase 7 status: complete.** Delivered as specified (see [FRONTEND.md](FRONTEND.md)): `frontend/` (Vite 8, strict TypeScript, Tailwind 4, self-hosted fonts); the `landing`, `chat`, `tickets`, `admin`, `monitoring` and `error` entries on one `base.html`; a procedural pixel globe and 8x8 pixel icons; unit tests for the rendering, streaming and API code; ESLint rules and Python tests that keep data out of markup; a CSP with no inline code; a frontend CI job with a bundle budget, `npm audit` in the security workflow and Dependabot for npm. Choices and differences:

- **Adapted, not copied:** pixel type for titles, monospace for chrome, and a readable sans for answers and tables, so long text stays comfortable.
- **Dark only,** one theme done well; every text pair is tested against WCAG AA.
- **Accessibility audit is manual.** axe-core was run in a real browser on the landing, chat, tickets, admin and monitoring pages (zero violations after fixing one real bug: the chat page lost its `h1`). The error page was not audited. It is not in CI; running it there needs a browser test runner.
- **A bundle budget is enforced** (30 KB JavaScript and 15 KB CSS per page, gzipped); the largest page is about 8 KB of JavaScript.
- **Signed-out visitors now land on `/welcome`** instead of going straight to the identity provider.
- **Dithering** (from a second reference): an ordered-dither landscape (`lib/dither.ts`, Bayer 8x8, five colours from the app's tokens) under the landing hero, on the error pages and along the bottom of the sign-in pages. The "Sign-in failed" page is now the styled error page with a `{Try again}` button instead of a bare line of HTML.
- **The sign-in pages use the same design** through a Keycloak login theme (`deploy/keycloak/themes/leyline`), so the whole local experience is consistent. In Azure the sign-in page is Entra ID's and is branded in the Entra admin centre.
- **Azure moved to Phase 8.**

### Phase 8: Azure deployment (about 2 weeks, needs a subscription)

**Status: built, deployed and verified (2026-11-19); two production-grade gates remain open (see the scorecard below).** The environment is deployed from code, rebuilt from an empty resource group, updated by an approved CI workflow, and checked with the Azure-specific retrieval and answer evals. Decisions made with the user, and where they differ from the plan at the end of this section (that list is the original plan; the notes here are what was built):
- **Subscription:** Azure for Students (about $100 credit, region policy: centralindia, eastasia, uaenorth, koreacentral, malaysiawest). Everything runs in **Korea Central**, next to the existing Azure OpenAI account `leyline-openai`.
- **Container Apps instead of App Service** (web, worker and a manual task job from one image); **Postgres + pgvector stays the search index** (it already passes every ACL and retrieval gate; Azure AI Search's paid tiers would use most of the credit, and the `SearchIndex` port keeps it addable later).
- **Models:** `gpt-5.4-mini` (chat) and `text-embedding-3-small` at 768 dimensions. The embedding change means the relevance floors must be re-swept and `evals/embeddings.json` re-recorded; no floor is lowered to pass.
- **Redis:** Azure Managed Redis B0 (the classic Azure Cache for Redis is not offered in Korea Central).
- **Secrets:** all in Key Vault, read by the apps through a managed identity (Key Vault references); the registry is pulled with the same identity. Keys for Postgres, Redis, Blob and Azure OpenAI still exist (as secrets), so "no stored keys" is not fully met: managed-identity access to those services needs adapter changes and is a follow-up.
- **Disposable by design:** `rg-leyline-app` holds everything that costs money; `infra/teardown.ps1` deletes it, `infra/deploy.ps1` rebuilds it. Always-on cost is roughly $35-45 a month, so the plan is to deploy for demos and tear down.
- **Entra (done, in the user's tenant):** app `Leyline Helpdesk` with assignment required, four app roles, four security groups, a client secret kept encrypted locally (`infra/entra.ps1`). Group ids become document group names through the new `OIDC_GROUP_NAMES` map (unmapped groups grant nothing). Demo users are created only when the user runs `-DemoUsers`. Not yet tested with a real token.
- **Budget:** `infra/budget.ps1` created `leyline-monthly` (4000 INR a month; alerts at 50/80/100% actual and 100% forecast; alerts only, nothing is stopped).
- **Pass 2 deployed (2026-11-10):** web app, worker and task job are running; the migrations ran; `/readyz` is fully ready. A real Entra sign-in was confirmed by the user, with per-group access spot-checked on Azure (HR and employee accounts), and chat answers with citations on the live site (out-of-scope questions are refused) after the Azure-specific search floors were set. The corpus is loaded (49 documents indexed through Azure OpenAI embeddings, no failures). Re-recording embeddings and the CI deploy workflow remain. Five defects only a real deploy could show are fixed and guarded (see DEPLOYMENT.md).
- **Pass 1 deployed (2026-11-10):** infrastructure and image are live; the apps (pass 2), the Azure redirect URI and the first sign-in are next. A real deploy found a bug that `what-if` and `validate` cannot (the OpenAI key was read while model deployments were being written, a `RequestConflict`); it is fixed in `modules/openai-key.bicep` and guarded by a test.
- **CI deploy workflow written (2026-11-11), NOT yet run on GitHub:** `.github/workflows/deploy.yml` (manual, `main` only, CI and Security must have passed on the commit, approval environment `azure`, OIDC with no stored secret, update-only: it refuses to create the environment and never touches Entra) and `infra/ci-identity.ps1` (the identity with narrow roles and the GitHub environment; **applied and read back on 2026-11-11**: app `Leyline CI Deploy` with no secret and one credential trusting only `repo:cyruswastaken/Leyline:environment:azure`, five role grants, GitHub environment `azure` with approval by the owner and `main` only; a second run created no duplicates). Also fixed the two failing Security jobs (gitleaks flagged two public Azure role ids; the Trivy action pin did not exist) and pinned third-party actions to commits. Decision record 0009; guard tests in `tests/test_workflows.py`.
- **Fresh deploy from an empty resource group verified (2026-11-11):** teardown (about 24 minutes) then `deploy.ps1 -Seed` (about 17 minutes) recreated everything with no manual step: ready, standard mode, 49 documents indexed, sign-in address registered. Not repeated after the rebuild: a real sign-in and chat.
- **Runbooks and decision records written (2026-11-11):** `docs/RUNBOOKS.md` (deploy, teardown, rebuild; the fixes for every failure seen) and `docs/adr/` (8 records). `tests/test_docs.py` checks that every command switch in the runbooks is real, links resolve and the records are complete and indexed.
- **CI deploy workflow proven (2026-11-18/19):** the first run failed at Azure sign-in (AADSTS700213) because the repository uses immutable OIDC subjects (owner and repository by id) and the credential had been created from the names; `ci-identity.ps1` now reads the subject from GitHub. After that a preview and a real deploy both passed, and the live site was checked (`/readyz` ready, both apps on the new image, sign-in redirect correct).
- **Pipeline fixes found by the first real runs:** the Security workflow now passes (gitleaks allowlist of two public role ids, Trivy pinned to a commit, a documented audit exception for one unpatched dev-tooling advisory, picomatch); the container smoke test's events helper retries on rate limits. 15 Dependabot PRs were triaged: 11 merged, 1 closed (TypeScript 7 fails the frontend checks), 3 held (SQLAlchemy 2.1, Node 26 and Python 3.15 images) until tested on the deployed runtime.
- **Demo users and access (2026-11-19):** the seven Entra demo users were recreated and the role pages and group-restricted answers were checked by the user on the live site.
- **Answer quality on Azure (2026-11-19, `docs/EVALUATION.md`):** gpt-5.4-mini answers 0.963, cites a source and an expected document 1.000, refuses unanswerable 1.000, restricted pass 1.000, ACL leaks 0, figures supported 0.981 (the metric now understands number words). Measured against a local database loaded with the Azure embedding model, not the deployed one.
- **Full suite:** 1,407 tests pass (2026-11-24; 1,294 at the 2026-11-19 measurement), coverage 95% overall (`services/authorization.py` at 100%).
- **Branch ruleset on `main` (2026-11-19):** before this, `main` had no protection and no rulesets, so the scans were not required checks and `main` could be force-pushed or deleted. The ruleset "main: no force-push, checks must pass" (id 27617185) blocks deletion and force-push and requires the ten CI and Security jobs before a pull request can merge. Repository admins can bypass it (the single maintainer commits straight to `main`), so direct pushes are not checked by it; the Deploy workflow's own gate (CI and Security must have passed on the exact commit) is the backstop. The required names were compared with the real job names. **Seen on a live PR on 2026-11-26** (#28): it blocked the merge while the Trivy scan failed and allowed it once every check passed. The browser tests were added the same day, making eleven required jobs.
- **Open, by design or not yet done:** managed-identity access to Postgres, Redis, Blob and Azure OpenAI (keys still live in Key Vault; ADR 0004); first-token latency with the real model (measured, target missed: the one gate still open below); Playwright and axe in CI; three held Dependabot PRs.
- **Original plan (kept for reference; the decisions above replace it where they differ):**
- Bicep modules for App Service (container, web + worker on one plan), Postgres, Redis, Key Vault, Blob, AI Search, Azure OpenAI, Log Analytics, Application Insights.
- Managed identity everywhere, with no stored keys. Entra app registration and group mapping including overage.
- Azure AI Search adapter that must pass the Phase 3 contract suite and ACL evals.
- GitHub Actions with OIDC federation: build, scan, deploy to staging slot, smoke test, swap.
- Budget alerts plus one-command spin-up and teardown, to suit demo-only use. Costs checked against the Azure pricing calculator first.
- Runbooks, architecture decision records, and rewritten docs. Drop the "educational lab" wording only once the gates below pass.

**Done when:** a fresh deploy from an empty resource group passes the full verification below.

### Phase 9: Latency and loose ends (2026-11-24)

**Status: first token improved, gate still open.** A deterministic check now skips the follow-up rewrite when a
question stands alone ([0010](adr/0010-skip-the-follow-up-rewrite-when-a-question-stands-alone.md)); a 30-conversation
follow-up eval keeps recall@5 at 0.964. Real-model load test, 8 users: first token p95 3.8 s to 3.0 s. Timed from
inside Azure (stages only) a new question reaches its first word in about 1.5 s at p95; the end-to-end figure on the
deployed site is not measured, so the gate stays open. The a051 figure check was fixed (figures supported 1.000).
Not done yet: the three held Dependabot PRs and the Azure follow-up recording (the ruleset was seen on a live PR on 2026-11-26).

### Phase 10: Retrieval and answer quality upgrades (2026-11-24)

**Status: built and measured; two ideas not adopted.**
- **Golden set 78 to 126** questions, written before measuring, including 12 near-topic unanswerable ones scored on
  their own. This exposed the weak spot: near-topic refusal 0.083 (local) and 0.417 (Azure). Floors were raised
  where the numbers allowed and a near-topic floor pins the baseline; none was lowered.
- **Not adopted, measured:** the document title in the embedded text ([0012](adr/0012-keep-the-document-title-out-of-the-embedding-text.md))
  and a `bge-reranker-base` cross-encoder ([0011](adr/0011-no-cross-encoder-reranker-for-now.md): lower recall, no
  usable floor). The port, adapter and sweep stay, off.
- **Real token and cost metering** per answered turn, on the turn's event, summed by day for the auditor
  ([0013](adr/0013-usage-and-cost-live-on-the-turn-event.md)); deviation from the plan: not on `messages`, to keep
  conversations owner-scoped.
- **Answer quality in CI:** recorded gpt-5.4-mini answers replayed through today's pipeline with floors
  (`tests/test_answers_replay.py`), plus a deterministic citation-grounding check.

### Phase 11: Production operations on Azure (2026-11-24)

**Status: built and tested locally; nothing here has been run on Azure yet** (it needs a rebuild and tenant changes
that wait for your approval; they are Phase 12's verification).
- **Security events off the request path:** a bounded background queue (`EVENT_QUEUE_SIZE`, set in Azure); a full
  queue drops and counts events and logs `EVENT_STORE_FAILED`. `deploy/sql/app_role.sql` limits the app's database
  role to INSERT and SELECT on the event tables; a test proves UPDATE, DELETE and TRUNCATE are denied (triggers
  cannot stop TRUNCATE). Applying it for real needs a separate migration identity, which is not built.
- **Entra group overage through Graph** (`OIDC_RESOLVE_GROUP_OVERAGE`): mapped groups only, any failure keeps the
  fail-closed result, the access token is never kept ([0016](adr/0016-resolve-entra-group-overage-through-graph-and-fail-closed.md)).
  Off until a tenant administrator consents to `GroupMember.Read.All`; not made.
- **Managed identity, no data-service keys** (`deploy.ps1 -ManagedIdentity`, template switch `useManagedIdentity`):
  Postgres, Redis, Blob storage and the models accept the container identity; the Entra client secret is the only
  secret left in Key Vault ([0014](adr/0014-the-container-identity-signs-in-to-the-data-services.md)). Compiles and passes
  production validation in both modes; needs a rebuild to adopt; one identity is the database administrator
  (recorded trade-off). The OpenAI account's own keys still exist on the account.
- **Alerts and objectives:** log-based alert rules (site down, 5xx, slow first token, model errors and throttling,
  dropped events, silent worker) and database CPU and storage alerts, with `infra/alerts.ps1` for the email action
  group, [SLO.md](SLO.md) and runbook sections 16 and 17 ([0015](adr/0015-alerts-from-the-logs-already-written.md)). No
  Application Insights or exporter dependency; none has been seen firing in Azure.
- **Restore drill:** `infra/restore-drill.ps1` restores to a temporary server, checks the data read-only and deletes it
  (costs a second small server for about 15 to 30 minutes). Not run: it creates a billable resource and needs your OK.
- **Safer deploys:** the Deploy workflow refuses a destructive migration unless a box is ticked
  (`scripts/check_migrations.py`), smoke-tests the signed-out site after deploying (`scripts/smoke_test.py`) and rolls
  both apps back to the previous image if the release fails; it also passes the sign-in mode and the action group
  from the running environment so an update never switches them. `ci-identity.ps1` gained two assignable roles; it must
  be re-run (by you) before a managed-identity deploy.

### Phase 12: Browser tests and final verification (2026-11-25)

**Status: browser tests built and passing locally; the rest is open.**
- **Playwright and axe** (`frontend/e2e`, `compose.e2e.yaml`, CI job "Browser tests (Playwright + axe)"): 17
  tests over real Keycloak sign-ins, permissions, upload to deletion, streaming to ticket resolution, and an
  accessibility audit of every page. Passed locally in Microsoft Edge and on GitHub (Chromium, merged in #28 on 2026-11-26); it is
  a required check in the `main` ruleset.
- **Open:** the full verification (teardown, `deploy.ps1 -Seed -ManagedIdentity`, `ci-identity.ps1`, a CI deploy,
  a person's Entra sign-in and chat, evals and load test, alerts seen firing), the latency gate, and the final
  production claim.

## Production-grade gates

Scorecard, updated 2026-11-21. The "educational lab" wording stays until the one open gate (latency under load with a real model) is met.

| Gate | State | Evidence |
| --- | --- | --- |
| Zero ACL leaks in evals, as a hard CI gate | **Met** | `acl_leaks` 0 in the CI retrieval gate, the Azure gate and both answer evals |
| Every route has allow and deny tests; coverage at least 85%, 100% of the authorization module | **Met** | `tests/test_access_matrix.py`; 1,294 tests, 95% total, `services/authorization.py` 100% |
| No high or critical findings from scanners | **Met, one documented exception** | Security workflow green (pip-audit, npm audit, CodeQL, gitleaks, Trivy, ZAP); `frontend/scripts/audit-gate.mjs` allows one unpatched dev-tooling advisory (picomatch), nothing shipped to browsers |
| No secrets in code, config or images | **Met** | gitleaks over the full history; secrets only in Key Vault (`tests/test_infra.py`). Keys for Postgres, Redis, Blob and OpenAI still exist as Key Vault secrets (ADR 0004) |
| Agreed latency targets for first token and full response, met under load | **Open** | Measured with the real model (2026-11-24, `docs/LOAD_TEST.md`, 4 and 8 users, 0 failures): whole turn p95 4.0 s (target 6 s, met), other endpoints met, but **first token p95 3.8 s against the 2.5 s target (missed)**. After skipping the rewrite for follow-ups that stand alone (Phase 9, 2026-11-24, follow-up eval keeps recall@5 0.964): first token p50 2.2 s, **p95 3.0 s, still missed**; whole turn p95 3.2 s. What is left is the embedding call, the model's first token and the holdback. The target was set with a fake model and is deliberately not changed to fit. Timed from inside Azure (stages only, 24 new questions): search + first 96 characters p50 1.07 s, p95 1.49 s, so a new question should be well under 2.5 s there; a follow-up that needs a rewrite is near it. The end-to-end figure on the deployed site is not measured (no Entra sign-in in Locust); Phase 11 exports the first-token metric and alerts on it, which closes the gate with evidence. Load test measured on a laptop against Azure OpenAI |
| Deployment is repeatable from code, with tested rollback | **Met for image-only changes** | Repeatable: rebuilt from an empty resource group (2026-11-11) and updated by the approved CI workflow. Rollback tested on the live environment (2026-11-21): web and worker back to an older image in 39 s and forward in 34 s, `/readyz` 200 on every probe (runbook section 15). Limits: migrations only go forward, so an image rollback is safe only while the schema is compatible; data written by a bad version is not undone |
| All security-relevant actions appear in the audit log | **Met** | `audit_events` (append-only trigger), covered by the red-team and access-matrix tests |

## Verification

- **Each PR:** lint, types, unit and integration tests against real Postgres and Redis containers, fake-LLM pipeline tests, security scans.
- **End to end (Playwright, in compose):** log in as each demo user; confirm a Security-only document is cited for Security users and invisible to staff; revoke a group and confirm access ends immediately; upload, ask, cite, delete, ask again; stream, give feedback, escalate, resolve.
- **Evals:** retrieval, groundedness and red-team suites on every relevant change.
- **Azure:** the eval suites against the deployed environment, and a person signing in through Entra (there is no staging copy, and the browser tests sign in through Keycloak only).
