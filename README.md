# Leyline

Leyline is a security-focused enterprise IT helpdesk chatbot built with Flask and retrieval-augmented generation (RAG). It answers questions from an internal document corpus while enforcing document access by user role before content is sent to the language model.

This is a portfolio project being built to production standards. The included documents are synthetic and must not be treated as real organizational policy or infrastructure documentation.

## What the project demonstrates

- Retrieval grounded in a local enterprise document corpus
- Role-based document authorization
- Input security checks and per-user rate limiting
- Responses constrained to authorized context
- Security event logging and monitoring endpoints
- A deployment path for Azure App Service, Azure OpenAI, and Microsoft Entra ID

## Documentation

| Document | Purpose |
| --- | --- |
| [Usage guide](docs/USAGE.md) | Install, configure, run, and test Leyline locally |
| [Architecture and security](docs/ARCHITECTURE.md) | Request flow, authorization boundary, and known risks |
| [Azure deployment guide](docs/DEPLOYMENT.md) | Cloud deployment overview, configuration, monitoring, and teardown |
| [Development guide](docs/DEVELOPMENT.md) | Repository conventions and validation workflow |
| [Roadmap](docs/ROADMAP.md) | Audit findings, target architecture and phased plan |

## Technology

- Python 3.11+ (container: 3.13)
- Flask
- Azure OpenAI
- Microsoft Entra ID
- Azure Blob Storage and Azure AI Search for the cloud deployment model
- Vanilla JavaScript and server-rendered HTML
- Docker, GitHub Actions, pytest, ruff, mypy

## Repository layout

```text
.
├── src/leyline/
│   ├── app.py          # create_app() factory
│   ├── config.py       # validated settings
│   ├── web/            # Flask blueprints
│   ├── services/       # pipeline logic
│   └── templates/
├── corpus/
│   ├── general/
│   └── restricted/
├── tests/
├── docs/
├── Dockerfile
├── compose.yaml
└── pyproject.toml
```

## Project status

Leyline is under active development and is **not yet safe for real data**: there is no authentication yet. Phase 0 (security fixes, tests, CI, container) is complete; Phase 1 (identity and access) is next. See the [roadmap](docs/ROADMAP.md) and the [architecture and security notes](docs/ARCHITECTURE.md).

## License

MIT
