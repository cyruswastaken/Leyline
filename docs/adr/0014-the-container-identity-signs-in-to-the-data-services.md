# 0014. The container identity signs in to the data services, not keys

**Status:** Accepted (Phase 11). Built and tested with fakes; **not yet proven on a rebuilt Azure environment**.
Supersedes the "partly met" part of [0004](0004-secrets-in-key-vault-read-by-managed-identity.md).

## Context
Decision 0004 stopped secrets living in templates and images, but Postgres, Redis, Blob storage and Azure OpenAI
still had keys, written into Key Vault. A key can leak, has to be rotated, and gives its holder access from anywhere.
An identity-based sign-in leaves nothing to leak: the platform hands each container a short-lived token for the
identity it was assigned.

## Decision
- One option, `useManagedIdentity` (`deploy.ps1 -ManagedIdentity`), off by default. On, the template:
  makes the identity the Postgres administrator and turns password sign-in off; turns Redis access keys off and
  gives the identity a data policy; makes the storage account refuse account keys and gives the identity Storage Blob
  Data Contributor; gives it Cognitive Services OpenAI User on the model account; and creates only one secret, the
  Entra client secret for sign-in.
- The app asks the container's identity endpoint (`IDENTITY_ENDPOINT`) for a token for each service, cached until
  five minutes before it expires (`auth/azure_identity.py`, no new dependency). A new database or Redis connection and
  each model call gets a fresh token. Each service has its own switch (`DATABASE_ENTRA_AUTH`, `BLOB_ACCOUNT_URL`,
  `REDIS_ENTRA_USERNAME`, `LLM_ENTRA_AUTH`, `EMBEDDING_ENTRA_AUTH`); locally nothing changes.
- The Deploy workflow reads the mode from the running app and passes it back, so an update never switches it.

## Consequences
- **One identity does everything**, including migrations, so the running app holds database administrator rights. That
  is simple and is the trade-off; the next step is a migration identity and a runtime identity limited to what the app
  needs (`deploy/sql/app_role.sql` shows the shape for the event tables).
- Adopting it means a **rebuild** (the data services change how they accept sign-ins). The Azure OpenAI account's own
  keys still exist on the account (it was created outside this template); Leyline just no longer stores or uses them.
- It works only inside Azure. Local development, tests and the evaluations keep keys.
- The CI identity needs two more assignable roles (Storage Blob Data Contributor in the app group; Cognitive Services
  OpenAI User on the model account), each limited by a condition; `ci-identity.ps1` was changed and must be re-run.

## Alternatives considered
Entra authentication for Postgres through the `azure-identity` package (a new dependency whose native parts some
locked-down machines block; the identity endpoint is a plain HTTP call); separate identities per service (more
resources for no gain while one app uses all of them); keeping keys and rotating them (a process, not a property).

## Evidence
`tests/test_managed_identity.py` (the token client against a local stand-in for the identity endpoint, caching and
refresh, a fresh token per database connection, the Redis, Blob and model clients, unsafe configurations refused),
`tests/test_infra.py` (identity mode creates and references no key secret; the template compiles; both modes pass
production validation). **Not yet measured:** a real sign-in to each service from a deployed container; that is the
check in the rebuild that closes Phase 12.
