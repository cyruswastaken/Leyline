# Architecture and Security

## Purpose

Leyline is a read-only RAG helpdesk. People sign in through their organisation's identity provider, ask IT questions, and get answers grounded only in the documents they are allowed to read. Knowledge-base administrators upload documents and decide which groups may read them.

## Request flow

```text
Browser
  -> security headers (CSP nonce, no-store, ...)
  -> per-IP rate limit                      (Redis, all routes except probes)
  -> session load                           (server-side, Redis; fail closed)
  -> CSRF check                             (signed-in, state-changing requests)
  -> route: login_required / require_roles  (deny by default)
  -> per-user rate limit                    (Redis)
  -> blocked-pattern validation
  -> permission-aware hybrid search         (keyword + vector legs, ACL predicate inside each query)
  -> re-authorization of every hit          (against the database, just before use)
  -> grounded language-model response
  -> security event logging
```

```text
Admin upload                                       Worker (same image)
  validate by content  ->  blob store (Azure/Azurite)
  -> document + version + ACL + job (one DB transaction)
                                       claim job (FOR UPDATE SKIP LOCKED)
                                       -> parse -> chunk -> embed (optional)
                                       -> replace chunks + promote version (one transaction)
```

Authorization is part of retrieval itself and is then verified again before anything reaches the prompt. An unauthorized chunk is never selected, and if it somehow were (a lagging index), it is dropped before the model sees it.

## Identity

Sign-in uses OpenID Connect, authorization code flow with PKCE (S256), via Authlib. State, nonce and the PKCE verifier live in the server-side session; the ID token's signature, issuer, audience, expiry and nonce are verified before anything is trusted. Locally the identity provider is Keycloak; on Azure it will be Microsoft Entra ID. Both publish a standard discovery document, so the code path is identical.

A `Principal` (`auth/principal.py`) is built only from verified token claims:

| Claim (configurable) | Becomes | Meaning |
| --- | --- | --- |
| `sub` | `user_id` | Stable user identifier, used for rate limits, ownership and audit |
| `groups` | `groups` | Which documents the person may **read** |
| `roles` | `roles` | What the person may **do** in Leyline |

Unknown role strings are ignored and every signed-in user is a plain `user`. Identity and role are **never** read from the URL, query string, body or headers.

### App roles

| Role | Grants |
| --- | --- |
| `user` | Chat (every signed-in user) |
| `kb_admin` | Manage documents and their ACLs; **no** read access to content |
| `support_agent` | Work escalated tickets (Phase 5) |
| `security_auditor` | `/monitoring*` dashboard and audit events |

Routes opt in with `@login_required` or `@require_roles(...)` (`auth/access.py`). `tests/test_access_matrix.py` lists every route with its policy and fails if a route is registered without one.

## Documents and permissions

### Data model (Postgres)

| Table | Holds |
| --- | --- |
| `documents` | Title, creator, pointer to the live version, soft-delete marker, optional external id |
| `document_versions` | One row per uploaded file: hash, size, stored-file key, status (`pending`, `processing`, `indexed`, `failed`, `superseded`), embedding model used |
| `document_acl` | One row per (document, group). **The single source of truth for who may read** |
| `chunks` | Searchable text with its heading, a generated full-text column and an optional 768-dimension embedding (HNSW-indexed) |
| `ingestion_jobs` | The durable work queue (ingest and purge jobs) |
| `audit_events` | Administrative actions: who uploaded, re-versioned, re-indexed, re-permissioned or deleted what |

The schema is managed by Alembic migrations (`python -m leyline.db upgrade`); `/readyz` reports not-ready if the database is not at the code's revision, and a test fails if the models and migrations drift apart.

### Access rule

A document is readable by a principal when its ACL contains one of the principal's groups, or the reserved entry `*` (any signed-in user). An empty ACL means nobody. Application roles never grant read access. The rule exists twice, and a test asserts the two agree for every combination of groups:

- `can_read` (Python) in `services/authorization.py`
- `readable_by` (SQL predicate) in the same module, applied inside the search query

If an identity provider omits groups from a token (Entra "overage"), the principal is flagged `groups_incomplete` and simply has no groups, so only `*` documents are readable. Resolving overage through Microsoft Graph is planned for Phase 7.

### Immediate effect

Because the ACL is queried at search time rather than copied into the index, **changing an ACL or deleting a document applies to the very next question**. Deleting also removes the chunks in the same transaction. Every search hit is additionally re-checked against the database (`authorize_hits`) right before use, which also protects against any future index that lags (Azure AI Search in Phase 7).

### Ingestion

- **Validation by content.** Extension, magic bytes and structure must agree (a `.pdf` must be a PDF, a `.docx` a valid Office zip); the browser's content type is ignored. Limits: 10 MB per file, 300 PDF pages, a cap on a DOCX's unpacked size (zip bombs), no encrypted PDFs, XML parsed with entity expansion disabled.
- **Stored by id, not name.** The original file goes to blob storage under `documents/<document-id>/<version-id>`; the user's filename is display-only, sanitized and always rendered as text.
- **Idempotent.** Uploading identical bytes as the latest version is a no-op.
- **Safe re-versioning.** A new version stays invisible until it is fully indexed, then replaces the old chunks and becomes current in one transaction, so searches never see an empty or half-indexed document. A slow older version that finishes after a newer one is marked `superseded` instead of overwriting it.
- **Reliable jobs.** Jobs commit atomically with the document state. Workers claim with `FOR UPDATE SKIP LOCKED`, failures retry with exponential back-off, unusable files fail immediately without retries, a crashed worker's job is reclaimed after a lease timeout, and a fencing token stops a worker that lost its lease from finishing someone else's job. Failures are visible in the admin page.
- **Embeddings are optional** and pluggable (any OpenAI-compatible endpoint). Each version records the model used, so a model change is detected and offered as a re-index.

## Retrieval

`PostgresSearchIndex` (behind the `SearchIndex` port) runs two legs over the `chunks` table:

- **Keyword:** `ts_rank_cd` over a generated full-text column (OR semantics; only `[a-z0-9]` tokens
  reach the query, so user text cannot inject tsquery operators).
- **Semantic:** cosine similarity over a fixed-size `vector(768)` column with an HNSW index. The
  query is embedded with the same model (and task prefix) as the documents. pgvector's
  iterative scan keeps the index scanning until enough *readable* rows are found, so a selective
  permission filter cannot starve a user of results.

Each leg applies `readable_by(principal)`, current-version and not-deleted conditions **inside
its own SQL**, so an unreadable chunk can never enter either candidate list. The lists are fused
with reciprocal-rank fusion (RRF), then capped at two chunks per document. Fused scores are
relative and cannot say "nothing relevant", so relevance is gated on the underlying signals: a
chunk is a candidate only if its cosine similarity (or, in hybrid mode, keyword rank) clears a
floor. Every hit is still re-checked with `authorize_hits` before it reaches the prompt.

Failure behaviour: with no embedder, or if embedding the question fails, search degrades to full
keyword search (the keyword floor is not applied then); evaluations run in a strict mode that
refuses to degrade silently. Tie-breaks are deterministic (title, chunk position), so results are
reproducible. Quality is measured and gated in CI; see [EVALUATION.md](EVALUATION.md).

## Sessions

The browser holds only an opaque random 256-bit id in an `HttpOnly`, `SameSite=Lax` cookie (`__Host-` prefixed and `Secure` when `SESSION_COOKIE_SECURE=true`). The principal and CSRF token live in Redis.

- Sliding idle timeout (30 min) and an absolute lifetime (12 h), both configurable.
- The session id is rotated at login (session fixation) and destroyed at logout; sign-out also ends the identity provider's session.
- If Redis is unreachable the request is refused with 503. It is never treated as anonymous-but-fine, and never as signed in.
- Health probes (`/healthz`, `/readyz`) never touch the session store.

## Browser protections

- **CSRF:** state-changing requests from a signed-in session need an `X-CSRF-Token` header (or `csrf_token` form field) matching the session token, plus a same-origin `Origin` header when one is sent.
- **CSP:** `default-src 'none'`, scripts and styles only with a fresh per-request nonce, no `unsafe-inline`, `frame-ancestors 'none'`.
- Also `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP/CORP, `Cache-Control: no-store`, and HSTS when cookies are secure.
- Event, title and filename data is rendered with `textContent` or auto-escaped templates, never `innerHTML`.

## Rate limiting

A Redis sliding window (`ratelimit.py`) is shared by every worker and instance. It is applied per client IP on all routes except health probes, and per user on `/chat`. Rejected attempts are removed so a blocked client cannot extend its own lockout. Behind a reverse proxy set `TRUSTED_PROXY_COUNT`; with the default `0`, `X-Forwarded-For` is ignored and cannot be used to evade the limit.

## Repository components

All code lives in `src/leyline/`.

- `app.py` is the `create_app()` factory and request hooks; `config.py` holds validated settings; `wiring.py` builds the concrete adapters shared by the app and the worker.
- `auth/` holds the principal, Redis sessions, OIDC setup, CSRF and the access decorators.
- `web/` contains the Flask blueprints (auth, chat, admin, health, monitoring).
- `db/` holds the SQLAlchemy models, the engine wrapper and the Alembic migrations.
- `ports/` defines the interfaces the app depends on (`BlobStore`, `Embedder`, `SearchIndex`); `adapters/` implements them (Azure Blob / Azurite, OpenAI-compatible embeddings, Postgres full-text search, in-memory blobs for tests). Swapping Azure AI Search in later means writing one adapter that passes the same tests.
- `ingestion/` holds the parsers, chunker, validation, the job queue and the document lifecycle service.
- `worker.py` is the ingestion worker; `seed.py` imports the sample corpus through the normal ingestion path.
- `services/` holds authorization, the chat pipeline, audit, security rules and the model call.
- `evals/` (package `leyline.evals`) is the retrieval evaluation harness; its data (golden questions, thresholds, recorded embeddings) lives in the top-level `evals/` directory.
- `security_headers.py` sets CSP and the other headers; `ratelimit.py` is the Redis limiter.
- `corpus/` contains the synthetic documents (49 across IT, HR, Finance, Security and Facilities).

## Cloud deployment model

The Azure deployment extends this design with:

- Microsoft Entra ID for authentication (app-level OIDC, not App Service Easy Auth, so one code path runs everywhere)
- Entra group object ids as document ACL groups and Entra app roles for application roles
- Azure Database for PostgreSQL (pgvector) and Azure Cache for Redis
- Blob Storage for the original files and Azure AI Search as an additional `SearchIndex` adapter
- Azure OpenAI for embeddings and response generation
- Log Analytics for diagnostic and security telemetry

See [DEPLOYMENT.md](DEPLOYMENT.md) for the outline and [ROADMAP.md](ROADMAP.md) for the target architecture.

## Threats and limitations

- **User group revocation lag.** A person's *groups and roles* are captured at sign-in. Removing someone from a group in the identity provider takes effect at their next sign-in or when the session expires (at most 12 hours by default). *Document-side* changes (ACL edits, deletions) do apply immediately. Shorter lifetimes or back-channel logout can narrow the window.
- **Administrators see metadata.** `kb_admin` can see titles, filenames and ACLs of every document, including restricted ones, but never their content.
- **Relevance floors are model-specific.** The shipped floors are calibrated for `nomic-embed-text`; another model needs a re-calibration sweep. Near-topic out-of-scope questions ("can I bring my dog to work?") can still return loosely related documents. Without an embedding model, search is keyword-only and refuses far less.
- **Parsing runs in the worker process.** Inputs are bounded and validated, but a maliciously crafted PDF could still consume CPU/memory in the worker; the job lease limits the blast radius. Sandboxing parsers is a later hardening step.
- **Prompt injection.** Pattern-based checks cannot detect every rephrased attack, and retrieved text is not yet clearly delimited as untrusted (Phase 4 adds layered guardrails).
- **Audit trail.** Administrative actions are audited in Postgres, but security events (logins, denials, retrievals) still go to a local file, and `audit_events` is not yet append-only (Phase 6).
- The model call has a timeout and retries but no graceful degraded mode yet; a model outage currently returns a generic 500 (Phase 4).
- Secrets in environment variables should be replaced with managed identity or Key Vault references in production.
- The development Keycloak realm uses shared demo passwords over plain HTTP, and Azurite uses Microsoft's published development key; both exist only for local use.
- The corpus is synthetic and has not been independently security tested.
- Grounding reduces hallucination risk but does not prove factual correctness.

The initial audit, its fixes and the remaining work are tracked in the [roadmap](ROADMAP.md). Every fixed vulnerability has a regression test. Review all controls before handling real company data.
