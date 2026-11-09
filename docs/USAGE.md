# Usage

This guide covers running Leyline locally.

## Prerequisites

- Python 3.11 or newer (CI tests 3.11 and 3.13; the container uses 3.13)
- Docker Desktop (the sign-in flow needs Redis and Keycloak, which the compose stack provides)
- An Azure OpenAI deployment (or another OpenAI-compatible endpoint) to get answers from `/chat`. Without one you can still sign in and use everything except answer generation: `/readyz` reports not-ready and a question that finds relevant documents fails when it reaches the model.

## Configuration

Copy `.env.example` to `.env` and fill in the Azure OpenAI values. Do not commit `.env`. All settings are validated at startup by [`config.py`](../src/leyline/config.py); the example file lists every option. The compose file already wires Redis and Keycloak, so a local `.env` only needs the model settings.

`LEYLINE_ENV=production` refuses to start unless the model and identity settings are present, session cookies are `Secure`, and `FLASK_DEBUG` is off.

## Run with Docker

```powershell
docker compose up --build
```

This starts the app, Redis and a Keycloak configured from [`deploy/keycloak/leyline-realm.json`](../deploy/keycloak/leyline-realm.json). Open `http://localhost:8000/` and you are redirected to Keycloak to sign in. The first start takes about a minute while Keycloak imports the realm.

To also start Postgres, Azurite and Jaeger (not used by the app yet):

```powershell
docker compose --profile infra up --build
```

### Demo users

All demo users share the throwaway password `Leyline-dev-1!` and exist only in the local Keycloak.

| User | Groups (document access) | App role |
| --- | --- | --- |
| `alice.employee` | staff | user |
| `sam.security` | staff, **security** | user |
| `ada.auditor` | staff | **security_auditor** |
| `kim.admin` | staff | **kb_admin** |
| `sue.agent` | staff | **support_agent** |
| `hank.hr` | staff, hr | user |

Try asking about the internal network architecture as `alice.employee` and as `sam.security`: only the security group may retrieve that restricted document. Only `ada.auditor` can open `/monitoring`. The Keycloak admin console is at `http://localhost:8080/` (`admin` / `admin-dev-only`).

## Run with Python

Run Redis and Keycloak with Docker, and the app on the host:

```powershell
docker compose up -d redis keycloak
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install --no-deps -e .
$env:OIDC_METADATA_URL = "http://localhost:8080/realms/leyline/.well-known/openid-configuration"
$env:OIDC_CLIENT_ID = "leyline-web"
$env:OIDC_CLIENT_SECRET = "leyline-dev-secret-change-me"
python -m leyline.app
```

This serves `http://localhost:5000/` with the Flask development server. The realm registers redirect URIs only for port 8000, so for host-run development either add `http://localhost:5000/auth/callback` to the client in Keycloak or run the app with gunicorn on port 8000. Set `FLASK_DEBUG=true` only on a trusted machine; the debugger allows remote code execution.

## HTTP endpoints

| Method | Endpoint | Access | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | public | Liveness: the process is up |
| `GET` | `/readyz` | public | Readiness: Redis, identity provider, model and corpus (503 otherwise) |
| `GET` | `/login` | public | Start sign-in (`?next=/path` returns you afterwards) |
| `GET` | `/auth/callback` | public | Identity provider redirect target |
| `POST` | `/logout` | signed in + CSRF | End the session and the identity provider's session |
| `GET` | `/me` | signed in | Current user, roles, groups and CSRF token |
| `GET` | `/` | signed in | Chat interface |
| `POST` | `/chat` | signed in + CSRF | Process a message |
| `GET` | `/monitoring` | `security_auditor` | Monitoring dashboard |
| `GET` | `/monitoring/summary` | `security_auditor` | Security event summary |
| `GET` | `/monitoring/events` | `security_auditor` | Recent security events |

Unauthenticated browser navigation is redirected to `/login`; unauthenticated API calls get `401`; signed-in users without the required role get `403`. State-changing requests need the `X-CSRF-Token` header (the page reads it from a `<meta>` tag; `/me` also returns it).

Earlier versions exposed `/documents`, `/documents/<role>` and `/search/<role>`. They leaked restricted content and accepted the role from the URL, so they were removed.

## Behavior

- Identity comes only from the signed-in session; nothing in the request can change who you are.
- Blocked prompt-extraction patterns return HTTP 403 before retrieval.
- Rate limits return HTTP 429: 10 chat requests per user and 120 requests per client IP, per 60 seconds by default.
- Invalid input returns HTTP 400: non-JSON bodies, non-string or empty messages, and messages over 2,000 characters.
- Requests without relevant context return an unavailable response rather than an invented answer.
- If Redis is unavailable, requests that need a session return 503.

## Tests and checks

```powershell
pytest --cov      # unit and integration tests (fake Redis, fake identity provider)
ruff check .      # lint
ruff format .     # formatting
mypy              # strict type checking
```

With the compose stack running, `python scripts/e2e_login.py` performs the real sign-in flow against Keycloak for every demo user and checks what each can reach, including that only the security group retrieves the restricted document. CI runs it too.
