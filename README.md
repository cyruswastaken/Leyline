# Leyline

Leyline is a security-focused enterprise IT helpdesk chatbot built with Flask and retrieval-augmented generation (RAG). It answers questions from an internal document corpus while enforcing document access by user role before content is sent to the language model.

This repository is a learning and portfolio project. The included restricted documents are synthetic and must not be treated as real organizational policy or infrastructure documentation.

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
| [Azure Portal lab guide](docs/rag-helpdesk-lab-guide.txt) | Detailed, numbered Azure Portal walkthrough |
| [Development guide](docs/DEVELOPMENT.md) | Repository conventions and validation workflow |

## Technology

- Python 3.11
- Flask
- Azure OpenAI
- Microsoft Entra ID
- Azure Blob Storage and Azure AI Search for the cloud deployment model
- Vanilla JavaScript and server-rendered HTML

## Repository layout

```text
.
├── app.py
├── services/
├── templates/
├── corpus/
│   ├── general/
│   └── restricted/
├── tests/
├── docs/
├── requirements.txt
└── README.md
```

## Project status

Leyline is an educational lab, not a production-ready service. Review the [architecture and security notes](docs/ARCHITECTURE.md) before using it with real data.

## License

MIT
