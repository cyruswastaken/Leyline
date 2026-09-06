# Architecture and Security

## Purpose

Leyline is a read-only RAG helpdesk. It loads documents, filters them by role, retrieves relevant authorized content, and asks Azure OpenAI to produce a grounded answer.

## Request flow

```text
Client
  -> Flask route
  -> per-user rate limit
  -> blocked-pattern validation
  -> document loading
  -> role-based authorization
  -> chunking
  -> relevance retrieval
  -> grounded language-model response
  -> security event logging
```

Authorization occurs before chunking and retrieval. This ordering is essential: an unauthorized document is not available to the retrieval layer or the model.

## Repository components

- `app.py` exposes the Flask routes and local identity defaults.
- `services/chat_service.py` orchestrates the request pipeline.
- `services/authorization.py` maps roles to access levels.
- `services/document_loader.py` parses and chunks corpus files.
- `services/retrieval.py` performs lightweight relevance scoring.
- `services/LLM.py` calls the configured Azure OpenAI deployment.
- `services/security.py` validates blocked patterns.
- `services/rate_limiter.py` limits requests per identity.
- `services/monitoring.py` stores and summarizes security events.
- `corpus/` contains synthetic general and restricted documents.

## Access model

| Role | Allowed access levels |
| --- | --- |
| `employee` | `general` |
| `security_admin` | `general`, `restricted` |

Every corpus document declares its access level in metadata. Unknown roles receive no documents.

## Cloud deployment model

The Azure deployment extends this design with:

- Microsoft Entra ID and App Service Easy Auth for authentication
- Entra group membership for role assignment
- Blob Storage as the document source
- Azure AI Search for indexed and filtered retrieval
- Azure OpenAI for embeddings and response generation
- Log Analytics for diagnostic and security telemetry

See [DEPLOYMENT.md](DEPLOYMENT.md) and the [Portal lab guide](rag-helpdesk-lab-guide.txt) for implementation details.

## Threats and limitations

- Pattern-based prompt-injection checks cannot detect every rephrased attack.
- The local rate limiter is in memory and is not suitable for multi-instance production deployments.
- Local role defaults are not authentication; they exist for the lab application.
- Secrets in local environment variables should be replaced with managed identity or Key Vault references in production.
- The corpus is synthetic and has not been independently security tested.
- Grounding reduces hallucination risk but does not prove factual correctness.

Treat this project as a security lab and review all controls before handling real company data.
