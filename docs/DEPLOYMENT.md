# Azure Deployment

The recommended cloud architecture uses Azure App Service, Microsoft Entra ID, Azure Blob Storage, Azure AI Search, Azure OpenAI, and Log Analytics.

## Deployment sequence

1. Create a resource group and choose a region that supports the required services.
2. Create Entra groups for general staff and IT administrators, then create test users.
3. Create Blob Storage and upload the `corpus/general` and `corpus/restricted` documents.
4. Create the Azure OpenAI resource and deploy a supported chat model and embedding model.
5. Create Azure AI Search, define a filterable access field, and index the corpus.
6. Create the Linux App Service and configure Easy Auth with Entra ID.
7. Configure application settings and deploy the Flask application.
8. Send diagnostic logs to Log Analytics and validate the security test matrix.

A step-by-step, infrastructure-as-code deployment replaces the earlier portal walkthrough in Phase 7 of the [roadmap](ROADMAP.md). Nothing below has been deployed yet.

## Application settings

For the current application code, configure (see [`.env.example`](../.env.example) for all options):

| Setting | Description |
| --- | --- |
| `LEYLINE_ENV` | Set to `production` (the container image default) |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key (move to Key Vault / managed identity) |
| `AZURE_OPENAI_DEPLOYMENT` | Chat model deployment name |

In production mode the app refuses to start if the model settings are missing or `FLASK_DEBUG` is on. Use `/healthz` as the liveness probe and `/readyz` as the readiness probe.

## Container

The image is built from the repository `Dockerfile` (multi-stage, non-root, gunicorn on port 8000) and is validated in CI. Do not deploy the current version beyond a private test: it has no authentication yet.

## Monitoring

Configure diagnostic settings for App Service, Azure AI Search, and Azure OpenAI to send supported logs and metrics to one Log Analytics workspace. Start with queries for:

- HTTP 403 and 429 responses
- Request volume by identity and time window
- Authentication redirects
- Repeated blocked-pattern events
- Retrieval or model failures

Azure OpenAI diagnostic logs may contain metadata rather than prompt and response content. Do not describe metadata-only logs as full conversation auditing.

## Cost control and teardown

- Use the lowest practical tiers for a short-lived lab.
- Set a budget and alert before creating paid resources.
- Stop or delete the App Service plan when not testing.
- Delete the resource group after validation.
- Remove test users, groups, deployments, and diagnostic workspaces that are no longer needed.
- Rotate or revoke all lab keys during teardown.

Teardown will be a single scripted command once the infrastructure code exists (Phase 7).
