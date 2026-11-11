# Decision records

Short records of the decisions that shape Leyline: what was decided, why, what it costs, and what
evidence backs it. They are kept so a later change can be judged against the reasons, not guessed at.
A record is never edited to hide a mistake; if a decision changes, a new record supersedes the old one.

Format: **Status**, **Context**, **Decision**, **Consequences**, **Alternatives considered**, **Evidence**.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-authorize-in-the-database-and-again-in-python.md) | Authorize documents in the database query and again in code | Accepted |
| [0002](0002-postgres-pgvector-as-the-search-index.md) | Postgres with pgvector is the search index, not Azure AI Search | Accepted |
| [0003](0003-azure-container-apps-standard-mode.md) | Azure Container Apps (standard mode) instead of App Service | Accepted |
| [0004](0004-secrets-in-key-vault-read-by-managed-identity.md) | Secrets live in Key Vault and are read by a managed identity | Accepted, partly met |
| [0005](0005-entra-groups-by-id-roles-by-assignment.md) | Entra: group IDs mapped to names, app roles, assignment required | Accepted |
| [0006](0006-disposable-environment-with-scripted-deploy.md) | A disposable environment with a scripted, repeatable deploy | Accepted |
| [0007](0007-calibrate-retrieval-per-embedding-model.md) | Retrieval floors are calibrated and gated per embedding model | Accepted |
| [0008](0008-no-inline-code-and-no-markup-from-data.md) | No inline code and no markup built from data | Accepted |
