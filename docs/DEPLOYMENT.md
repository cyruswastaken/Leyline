# Azure Deployment

Leyline runs on **Azure Container Apps** with PostgreSQL (pgvector) as the search index, Azure Managed Redis, Blob Storage, Key Vault, Azure OpenAI, Microsoft Entra ID for sign-in and Log Analytics. Everything is Bicep in `infra/`; nothing has been deployed yet.

## Infrastructure as code (`infra/`)

```text
infra/main.bicep          subscription-scope entry point (creates rg-leyline-app)
infra/modules/            platform (identity, registry, vault, storage, logs), data (Postgres, Redis),
                          openai (model deployments), secrets (Key Vault values), apps (environment, web, worker, task job)
infra/deploy.ps1          two-pass deploy: infrastructure, image, then the apps and migrations
infra/teardown.ps1        deletes rg-leyline-app and purges the vault; keeps your OpenAI account
```

Two resource groups: `rg-leyline` (existing; only the two model deployments are added) and
`rg-leyline-app` (everything that costs money while running). Preview, then deploy:

```powershell
az login
./infra/deploy.ps1 -WhatIf          # preview only: creates nothing
./infra/deploy.ps1                  # pass 1: infrastructure + image (about 10 minutes)
./infra/deploy.ps1 -OidcClientId <id> -OidcTenantId <tenant> -OidcClientSecret (Read-Host -AsSecureString)
./infra/teardown.ps1                # when finished (asks you to type the group name)
```

- **Secrets:** generated or read from the live resources and stored only in Key Vault. The Container Apps read them through a managed identity; the Postgres password is generated per run, written to a temporary file only you can read, and deleted afterwards.
- **Sign-in redirect:** pass 1 prints the redirect URI (`https://leyline-web.<domain>/auth/callback`) to register in Entra before pass 2.
- **Demo-tier shortcuts:** Postgres and Redis have public endpoints (TLS plus a password or key; Postgres allows only Azure services). A production deployment would use private networking, Entra authentication to Postgres, and managed-identity access to Blob and OpenAI. The worker has no heartbeat probe (Container Apps cannot run an exec probe); the platform restarts it if the process exits.
- **Models:** the GPT-5 family accepts only its default temperature, so the template sets `LLM_SEND_TEMPERATURE=false` and a 55 second timeout.
- **Cost:** about $35-45 a month while running (Postgres B1ms, Managed Redis B0 at $0.019/hour, registry, Container Apps, logs); model calls cost cents for a demo. Estimates, not a price-sheet check. With a student credit, deploy for demos and tear down.
- **Checked:** the templates compile without warnings (`tests/test_infra.py` compiles them when the Bicep compiler is installed) and pass `az deployment sub what-if` and `validate` on the subscription. A real deploy has not been run.

## Original manual sequence (for reference)

## Deployment sequence

1. Create a resource group and choose a region that supports the required services.
2. Create Entra groups for general staff and IT administrators, then create test users.
3. Create Blob Storage and upload the `corpus/general` and `corpus/restricted` documents.
4. Create the Azure OpenAI resource and deploy a supported chat model and embedding model.
5. Create Azure AI Search, define a filterable access field, and index the corpus.
6. Register an Entra application for Leyline (redirect URI `https://<host>/auth/callback`), add the `groups` and `roles` claims, and create app roles matching `user`, `kb_admin`, `support_agent` and `security_auditor`.
7. Create Azure Database for PostgreSQL (allow-list the `vector` extension), Azure Cache for Redis and the Linux App Service. Run `python -m leyline.db upgrade` once, then deploy the web container and a second instance of the same image running `python -m leyline.worker`.
8. Send diagnostic logs to Log Analytics and validate the security test matrix.

A step-by-step, infrastructure-as-code deployment replaces the earlier portal walkthrough in Phase 8 of the [roadmap](ROADMAP.md). Nothing below has been deployed yet.

## Application settings

For the current application code, configure (see [`.env.example`](../.env.example) for all options):

| Setting | Description |
| --- | --- |
| `LEYLINE_ENV` | Set to `production` (the container image default) |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key (move to Key Vault / managed identity) |
| `AZURE_OPENAI_DEPLOYMENT` | Chat model deployment name |
| `OIDC_METADATA_URL` | Entra discovery URL, `https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration` |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | Entra app registration (secret to Key Vault) |
| `OIDC_GROUPS_CLAIM` / `OIDC_ROLES_CLAIM` | Claim names (`groups` / `roles`) |
| `DATABASE_URL` | `postgresql+pg8000://user:password@host:5432/leyline` (secret; Key Vault) |
| `BLOB_CONNECTION_STRING` | Storage account connection string (secret). Managed identity replaces it in Phase 8 |
| `BLOB_CONTAINER` | Container for original uploads (default `leyline-documents`) |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | Optional; any OpenAI-compatible embeddings endpoint (Azure OpenAI v1 API, OpenAI, Ollama) |
| `REDIS_URL` | `rediss://:<key>@<name>.redis.cache.windows.net:6380/0` |
| `SESSION_COOKIE_SECURE` | Must be `true` in production |
| `TRUSTED_PROXY_COUNT` | Reverse proxies in front of the app (1 on App Service) |

In production mode the app refuses to start if the model or identity settings are missing, cookies are not secure, or `FLASK_DEBUG` is on. Use `/healthz` as the liveness probe and `/readyz` as the readiness probe (it also checks Redis, the database and its migration level, and blob storage). Run the worker with `WORKER_HEARTBEAT_PATH` set and use a health check that fails if that file goes stale; the compose file shows one.

## Sign-in page branding

The local Keycloak sign-in pages use the repository's `leyline` login theme. With Entra ID the sign-in page is Microsoft's: configure it under Entra admin centre > Company branding (background colour `#0b0c0b`, a logo, the sign-in text) to stay close to the app's look. What it allows (logo, colours, and possibly custom CSS) depends on the tenant's licence and has not been checked here, so the pixel globe and fonts may not carry over.

## Container

The image is built from the repository `Dockerfile` (multi-stage, non-root, gunicorn on port 8000) and is validated in CI with the full sign-in flow against Keycloak. Entra-specific behavior (group overage, app roles) is not yet tested against a real tenant; that happens in Phase 8.

## Monitoring

Configure diagnostic settings for App Service, Azure AI Search, and Azure OpenAI to send supported logs and metrics to one Log Analytics workspace. Start with queries for:

- HTTP 403 and 429 responses
- Request volume by identity and time window
- Authentication redirects
- Repeated `SECURITY_RULE_TRIGGERED`, `INJECTION_SIGNAL` and `PROMPT_LEAK_BLOCKED` events (also queryable in the `security_events` table)
- Retrieval or model failures

Azure OpenAI diagnostic logs may contain metadata rather than prompt and response content. Do not describe metadata-only logs as full conversation auditing.

## Cost control and teardown

- Use the lowest practical tiers for a short-lived lab.
- Set a budget and alert before creating paid resources.
- Stop or delete the App Service plan when not testing.
- Delete the resource group after validation.
- Remove test users, groups, deployments, and diagnostic workspaces that are no longer needed.
- Rotate or revoke all lab keys during teardown.

Teardown is `./infra/teardown.ps1` (deletes `rg-leyline-app`, purges the Key Vault, keeps the Azure OpenAI account; `-IncludeModels` also removes the model deployments). It does not remove the Entra app registration or test users.
