# Architecture and Security

## Purpose

Leyline is a read-only RAG helpdesk. People sign in through their organisation's identity provider, ask IT questions, and get answers grounded only in the documents they are allowed to read.

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
  -> document loading
  -> authorization against the principal    (BEFORE chunking and retrieval)
  -> chunking
  -> relevance retrieval
  -> grounded language-model response
  -> security event logging
```

Authorization occurs before chunking and retrieval. This ordering is essential: an unauthorized document is not available to the retrieval layer or the model.

## Identity

Sign-in uses OpenID Connect, authorization code flow with PKCE (S256), via Authlib. State, nonce and the PKCE verifier live in the server-side session; the ID token's signature, issuer, audience, expiry and nonce are verified before anything is trusted. Locally the identity provider is Keycloak; on Azure it will be Microsoft Entra ID. Both publish a standard discovery document, so the code path is identical.

A `Principal` (`auth/principal.py`) is built only from verified token claims:

| Claim (configurable) | Becomes | Meaning |
| --- | --- | --- |
| `sub` | `user_id` | Stable user identifier, used for rate limits, ownership and audit |
| `groups` | `groups` | Which documents the person may **read** |
| `roles` | `roles` | What the person may **do** in Leyline |

Unknown role strings are ignored and every signed-in user is a plain `user`. Identity and role are **never** read from the URL, query string, body or headers; the removed `/documents/<role>` and `/search/<role>` routes were an example of what not to do.

### App roles

| Role | Grants |
| --- | --- |
| `user` | Chat (every signed-in user) |
| `kb_admin` | Manage documents (Phase 2) |
| `support_agent` | Work escalated tickets (Phase 5) |
| `security_auditor` | `/monitoring*` dashboard and audit events |

Routes opt in with `@login_required` or `@require_roles(...)` (`auth/access.py`). `tests/test_access_matrix.py` lists every route with its policy and fails if a route is registered without one.

### Document access

Interim model until per-document group ACLs arrive with the database (Phase 2):

- `general` documents: any signed-in user.
- `restricted` documents: membership in one of `RESTRICTED_ACCESS_GROUPS` (default `security`). An app role such as `security_auditor` does **not** unlock documents.
- A missing or unrecognised access level, or an empty group configuration, grants nobody access.
- Metadata is read only from a document's leading header block, so body text cannot change its classification.

If an identity provider omits groups from a token (Entra "overage"), the principal is flagged `groups_incomplete` and simply has no groups, so restricted access fails closed. Resolving overage through Microsoft Graph is planned for Phase 7.

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
- Event and user data is rendered with `textContent` or auto-escaped templates, never `innerHTML`.

## Rate limiting

A Redis sliding window (`ratelimit.py`) is shared by every worker and instance. It is applied per client IP on all routes except health probes, and per user on `/chat`. Rejected attempts are removed so a blocked client cannot extend its own lockout. Behind a reverse proxy set `TRUSTED_PROXY_COUNT`; with the default `0`, `X-Forwarded-For` is ignored and cannot be used to evade the limit.

## Repository components

All code lives in `src/leyline/`.

- `app.py` is the `create_app()` factory and request hooks; `config.py` holds validated settings.
- `auth/` holds the principal, Redis sessions, OIDC setup, CSRF and the access decorators.
- `web/` contains the Flask blueprints (auth, chat, health, monitoring).
- `security_headers.py` sets CSP and the other headers; `ratelimit.py` is the Redis limiter.
- `services/chat_service.py` orchestrates the request pipeline.
- `services/authorization.py` decides which documents a principal may read.
- `services/document_loader.py` parses and chunks corpus files.
- `services/retrieval.py` performs lightweight relevance scoring.
- `services/llm.py` calls the configured Azure OpenAI deployment.
- `services/security.py` validates blocked patterns.
- `services/monitoring.py` stores and summarizes security events.
- `corpus/` contains synthetic general and restricted documents.

## Cloud deployment model

The Azure deployment extends this design with:

- Microsoft Entra ID for authentication (app-level OIDC, not App Service Easy Auth, so one code path runs everywhere)
- Entra group membership for document ACLs and Entra app roles for application roles
- Azure Cache for Redis for sessions and rate limits
- Blob Storage as the document source and Azure AI Search for filtered retrieval
- Azure OpenAI for embeddings and response generation
- Log Analytics for diagnostic and security telemetry

See [DEPLOYMENT.md](DEPLOYMENT.md) for the outline and [ROADMAP.md](ROADMAP.md) for the target architecture.

## Threats and limitations

- **Session revocation lag.** Roles and groups are captured at sign-in. Removing a user's group takes effect at their next sign-in, or when the session expires (at most 12 hours by default). Per-request re-authorization against the database arrives in Phase 2; shorter lifetimes and back-channel logout are options.
- Pattern-based prompt-injection checks cannot detect every rephrased attack (Phase 4 replaces them with layered guardrails).
- Security events are still written to a local file; `/monitoring*` is auditor-only but not yet backed by a durable store (Phase 6).
- The model call has a timeout and retries but no graceful degraded mode yet; a model outage currently returns a generic 500 (Phase 4).
- Secrets in environment variables should be replaced with managed identity or Key Vault references in production.
- The development Keycloak realm uses shared demo passwords over plain HTTP and exists only for local use.
- The corpus is synthetic and has not been independently security tested.
- Grounding reduces hallucination risk but does not prove factual correctness.

The initial audit, its fixes and the remaining work are tracked in the [roadmap](ROADMAP.md). Every fixed vulnerability has a regression test in `tests/test_regressions.py`. Review all controls before handling real company data.
