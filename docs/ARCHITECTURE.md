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

All code lives in `src/leyline/`.

- `app.py` is the `create_app()` factory; `config.py` holds validated settings.
- `web/` contains the Flask blueprints (chat, health, monitoring).
- `services/chat_service.py` orchestrates the request pipeline.
- `services/authorization.py` maps roles to access levels.
- `services/document_loader.py` parses and chunks corpus files.
- `services/retrieval.py` performs lightweight relevance scoring.
- `services/llm.py` calls the configured Azure OpenAI deployment.
- `services/security.py` validates blocked patterns.
- `services/rate_limiter.py` limits requests per identity.
- `services/monitoring.py` stores and summarizes security events.
- `corpus/` contains synthetic general and restricted documents.

## Access model

| Role | Allowed access levels |
| --- | --- |
| `employee` | `general` |
| `security_admin` | `general`, `restricted` |

Every corpus document declares its access level in a leading header block. Metadata is read only from that block, so a line in the document body cannot change a document's classification. Documents without a recognised access level, and unknown roles, receive no access.

## Cloud deployment model

The Azure deployment extends this design with:

- Microsoft Entra ID and App Service Easy Auth for authentication
- Entra group membership for role assignment
- Blob Storage as the document source
- Azure AI Search for indexed and filtered retrieval
- Azure OpenAI for embeddings and response generation
- Log Analytics for diagnostic and security telemetry

See [DEPLOYMENT.md](DEPLOYMENT.md) for the deployment outline. The target architecture is described in [ROADMAP.md](ROADMAP.md).

## Threats and limitations

- Pattern-based prompt-injection checks cannot detect every rephrased attack.
- The local rate limiter is in memory and is not suitable for multi-instance production deployments.
- There is no authentication yet. `/chat` uses a fixed `local-user` / `employee` identity, and the `/monitoring*` routes are open. Both are addressed in Phase 1 of the [roadmap](ROADMAP.md).
- Roles and identity are never accepted from request input; routes that did so were removed after the initial audit.
- Secrets in local environment variables should be replaced with managed identity or Key Vault references in production.
- The corpus is synthetic and has not been independently security tested.
- Grounding reduces hallucination risk but does not prove factual correctness.

The initial audit, its fixes and the remaining work are tracked in the [roadmap](ROADMAP.md). Every fixed vulnerability has a regression test in `tests/test_regressions.py`. Review all controls before handling real company data.
