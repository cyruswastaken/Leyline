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

**Status:** the corpus is still loaded per request (Phase 2), the rate limiter is now Redis-backed and shared across instances (Phase 1), and the log-file monitoring remains (Phase 6). Dependencies are pinned, tests, CI, a Dockerfile and compose now exist, and docs have been corrected; the missing lab guide is replaced in Phase 7.

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

**Phase 1 status: complete.** Delivered as specified, plus: the route matrix fails when a route has no declared policy; sessions have idle and absolute timeouts, rotate at login, and fail closed (503) when Redis is down; CSP uses per-request nonces; and `scripts/e2e_login.py` (run in CI) drives the real login flow against Keycloak for every demo user. Two items moved: per-request re-authorization against the database (to limit group-revocation lag) lands with Postgres in Phase 2, and Entra group-overage resolution via Microsoft Graph lands in Phase 7.

### Phase 2: Data and ingestion (about 2 weeks)
- Alembic migrations for `documents`, `document_versions`, `document_acl`, `chunks`, `ingestion_jobs`, `conversations`, `messages`, `feedback`, `tickets`, `ticket_events`, `audit_events`.
- Worker: upload to blob, validate type and size, parse PDF/DOCX/Markdown/TXT, chunk by heading structure, embed in batches, index.
- Idempotent by content hash, retries with backoff, failed-job visibility. Delete and re-version remove old chunks.
- Admin UI: upload, assign ACL groups, status, versions, re-index, delete.
- Record embedding model and dimension per index so switching providers forces a re-index.
- Migrate the 5 sample documents and add a larger synthetic corpus (about 50-100 documents across departments) so retrieval and ACLs are tested meaningfully.

**Done when:** an upload becomes searchable, and a deleted document is unretrievable, both verified automatically.

### Phase 3: Retrieval and evaluation (about 1-2 weeks)
- Hybrid search: pgvector + Postgres full-text, merged with reciprocal rank fusion, ACL filter applied inside the query.
- Relevance threshold, a per-document result cap, and the Postgres re-authorization step.
- A contract test suite that every `SearchIndex` adapter must pass, ready for AI Search.
- Eval harness with a golden question set: recall@k, MRR, groundedness, citation accuracy, correct refusals.

**Done when:** baseline metrics are recorded, and CI fails on a retrieval regression or on **any** ACL leak.

### Phase 4: Chat experience (about 2 weeks)
- Persistent conversations, follow-up query rewriting, token budgeting, retention setting.
- SSE streaming with cancel; timeouts, retries and a circuit breaker around the LLM; clear degraded-mode messages.
- Structured citations with clickable sources, and thumbs up/down with reason.
- Guardrails: retrieved text clearly delimited as untrusted, score-and-log injection signals in place of the 403 denylist, citations verified against what was actually retrieved, credential-pattern redaction on a rolling stream buffer.
- UI rebuild on vanilla JS: conversation sidebar, streaming, safe Markdown rendering, accessibility, mobile.

**Done when:** a multi-turn cited conversation streams end to end, and LLM failures degrade cleanly.

### Phase 5: Ticket escalation (about 1 week)
- "Escalate" action with a pre-filled summary the user reviews before sending.
- `TicketProvider` port and built-in queue; agent view with status, assignment and comments.
- Tickets contain only what the user approved, so restricted context cannot leak to agents who lack access.

**Done when:** the full flow from failed answer to resolved ticket passes an end-to-end test.

### Phase 6: Hardening and observability (about 1-2 weeks)
- OpenTelemetry traces and metrics: per-stage latency, tokens, cost, no-answer rate, feedback score. JSON logs to stdout, with prompts excluded by default.
- Append-only audit log; rebuild the security dashboard on SQL with role-gated access.
- Written threat model (OWASP LLM Top 10 and STRIDE) and a red-team eval suite for injection, exfiltration and cross-user access.
- CI security: pip-audit, gitleaks, Trivy, CodeQL, ZAP baseline. Load test with Locust against defined latency targets.

**Done when:** the threat model is written, security scans block merges, and load targets are met.

### Phase 7: Azure deployment (about 2 weeks, needs a subscription)
- Bicep modules for App Service (container, web + worker on one plan), Postgres, Redis, Key Vault, Blob, AI Search, Azure OpenAI, Log Analytics, Application Insights.
- Managed identity everywhere, with no stored keys. Entra app registration and group mapping including overage.
- Azure AI Search adapter that must pass the Phase 3 contract suite and ACL evals.
- GitHub Actions with OIDC federation: build, scan, deploy to staging slot, smoke test, swap.
- Budget alerts plus one-command spin-up and teardown, to suit demo-only use. Costs checked against the Azure pricing calculator first.
- Runbooks, architecture decision records, and rewritten docs. Drop the "educational lab" wording only once the gates below pass.

**Done when:** a fresh deploy from an empty resource group passes the full verification below.

## Production-grade gates

- Zero ACL leaks in evals, as a hard CI gate.
- Every route has allow and deny tests; coverage of at least 85%, and 100% of the authorization module.
- No high or critical findings from scanners.
- No secrets in code, config or images.
- Agreed latency targets for first token and full response, met under load.
- Deployment is repeatable from code, with tested rollback.
- All security-relevant actions appear in the audit log.

## Verification

- **Each PR:** lint, types, unit and integration tests against real Postgres and Redis containers, fake-LLM pipeline tests, security scans.
- **End to end (Playwright, in compose):** log in as each demo user; confirm a Security-only document is cited for Security users and invisible to staff; revoke a group and confirm access ends immediately; upload, ask, cite, delete, ask again; stream, give feedback, escalate, resolve.
- **Evals:** retrieval, groundedness and red-team suites on every relevant change.
- **Azure:** the same Playwright and eval suites against staging, via real Entra login.
