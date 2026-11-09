# Usage

This guide covers running Leyline locally.

## Prerequisites

- Python 3.11 or newer (CI tests 3.11 and 3.13; the container uses 3.13)
- Docker Desktop, if you want to use the container workflow
- An Azure OpenAI deployment (or another OpenAI-compatible endpoint) to get
  answers from `/chat`. Without one the app still starts; `/readyz` reports
  not-ready and `/chat` fails when it needs the model.

## Configuration

Copy `.env.example` to `.env` and fill in the Azure OpenAI values. Do not
commit `.env`. All settings are validated at startup by
[`config.py`](../src/leyline/config.py); the example file lists every option.

`LEYLINE_ENV=production` refuses to start unless the model settings are
present and `FLASK_DEBUG` is off.

## Run with Docker

```powershell
docker compose up --build
```

The app listens on `http://127.0.0.1:8000/`. To also start Postgres, Redis,
Keycloak, Azurite and Jaeger (not used by the app yet):

```powershell
docker compose --profile infra up --build
```

## Run with Python

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install --no-deps -e .
python -m leyline.app
```

This serves `http://127.0.0.1:5000/` using the Flask development server. Set
`FLASK_DEBUG=true` only on a trusted machine; the debugger allows remote code
execution.

## HTTP endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Chat interface |
| `GET` | `/healthz` | Liveness: the process is up |
| `GET` | `/readyz` | Readiness: model configured and corpus available (503 otherwise) |
| `POST` | `/chat` | Process a message |
| `GET` | `/monitoring` | Monitoring dashboard (unauthenticated until Phase 1) |
| `GET` | `/monitoring/summary` | Security event summary (unauthenticated until Phase 1) |
| `GET` | `/monitoring/events` | Recent security events (unauthenticated until Phase 1) |

Earlier versions exposed `/documents`, `/documents/<role>` and
`/search/<role>`. They leaked restricted content and accepted the role from
the URL, so they were removed.

Example request:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/chat `
  -ContentType "application/json" `
  -Body '{"message":"How should I protect my corporate password?"}'
```

## Current behavior

- There is no login yet. Every request is treated as user `local-user` with
  role `employee`, which can read only `general` documents. Real
  authentication arrives in Phase 1.
- Blocked prompt-extraction patterns return HTTP 403 before retrieval.
- Excessive requests from one identity return HTTP 429 (10 per 60 seconds by
  default).
- Invalid input returns HTTP 400: non-JSON bodies, non-string or empty
  messages, and messages over 2,000 characters.
- Requests without relevant context return an unavailable response rather
  than an invented answer.

## Tests and checks

```powershell
pytest --cov      # tests
ruff check .      # lint
ruff format .     # formatting
mypy              # strict type checking
```
