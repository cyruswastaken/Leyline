# Leyline

Leyline is a security-focused enterprise IT helpdesk chatbot built with Flask and retrieval-augmented generation (RAG). It answers questions from an internal document corpus while enforcing document access by user role before content is sent to the language model.

This is a portfolio project being built to production standards. The included documents are synthetic and must not be treated as real organizational policy or infrastructure documentation.

## What the project demonstrates

- Retrieval grounded in a local enterprise document corpus
- Sign-in with OpenID Connect (Keycloak locally, Microsoft Entra ID on Azure), server-side sessions, CSRF and CSP protection
- Per-document group ACLs enforced inside the search query and re-checked before use
- Document ingestion (PDF, DOCX, Markdown, text) through a durable job queue and worker, with an admin UI
- Input security checks and distributed (Redis) rate limiting
- Responses constrained to authorized context
- Security event logging and an auditor-only monitoring dashboard
- A deployment path for Azure App Service, Azure OpenAI, and Microsoft Entra ID

## Documentation

| Document | Purpose |
| --- | --- |
| [Usage guide](docs/USAGE.md) | Install, configure, run, and test Leyline locally |
| [Architecture and security](docs/ARCHITECTURE.md) | Request flow, authorization boundary, and known risks |
| [Azure deployment guide](docs/DEPLOYMENT.md) | Cloud deployment overview, configuration, monitoring, and teardown |
| [Development guide](docs/DEVELOPMENT.md) | Repository conventions and validation workflow |
| [Frontend](docs/FRONTEND.md) | Design system, build, structure, accessibility and how to work on the UI |
| [Threat model](docs/THREAT_MODEL.md) | STRIDE and OWASP LLM Top 10 analysis, controls and the tests that prove them |
| [Load test](docs/LOAD_TEST.md) | Latency targets, how to run Locust, measured results |
| [Retrieval evaluation](docs/EVALUATION.md) | Golden set, metrics, results and the CI quality gate |
| [Roadmap](docs/ROADMAP.md) | Audit findings, target architecture and phased plan |

## Technology

- Python 3.11+ (container: 3.13)
- Flask
- Azure OpenAI
- Microsoft Entra ID (Keycloak for local development)
- Redis (sessions, rate limits)
- PostgreSQL with pgvector (hybrid keyword + semantic search), Azure Blob Storage (Azurite locally); Azure AI Search planned for the cloud deployment
- Ollama / any OpenAI-compatible endpoint for embeddings (`nomic-embed-text` locally)
- Vanilla JavaScript and server-rendered HTML
- Docker, GitHub Actions, pytest, ruff, mypy

## Repository layout

```text
.
├── src/leyline/
│   ├── app.py          # create_app() factory
│   ├── config.py       # validated settings
│   ├── auth/           # principal, sessions, OIDC, CSRF, access control
│   ├── web/            # Flask blueprints
│   ├── db/             # models and Alembic migrations
│   ├── ingestion/      # parsers, chunker, job queue, document lifecycle
│   ├── ports/ adapters/# interfaces and their implementations
│   ├── services/       # authorization, chat pipeline, audit
│   ├── worker.py       # ingestion worker
│   └── templates/
├── corpus/
│   ├── general/
│   └── restricted/
├── tests/
├── docs/
├── deploy/keycloak/    # local identity provider realm
├── Dockerfile
├── compose.yaml
└── pyproject.toml
```

## Project status

Leyline is under active development and is **not yet safe for real data**. Phases 0 (security fixes, tests, CI, container), 1 (identity and access), 2 (database, document ingestion, per-document ACLs) and 3 (hybrid retrieval and an evaluation gate), 4 (chat experience: conversations, streaming, citations, guardrails) 5 (ticket escalation) and 6 (hardening and observability) and 7 (frontend design system) are complete; Phase 8 (Azure deployment, needs a subscription) is next. See the [roadmap](docs/ROADMAP.md) and the [architecture and security notes](docs/ARCHITECTURE.md).

## License

MIT
