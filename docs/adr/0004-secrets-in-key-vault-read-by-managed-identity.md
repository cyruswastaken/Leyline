# 0004. Secrets live in Key Vault and are read by a managed identity

**Status:** Accepted, **partly met**. The roadmap goal "no stored keys" is not fully reached.

## Context
The app needs a database URL, a Redis URL, a storage connection string, a model key and the Entra client
secret. None may sit in a template, an image, a parameter file or an environment variable of the deployment.

## Decision
- Every secret is written once into Key Vault (role-based access, soft delete on) by the deployment, built
  from the live resources.
- The containers read them through a user-assigned managed identity with the "Key Vault Secrets User" role, using
  Key Vault references; the same identity pulls images (AcrPull).
- The Postgres password is generated per deploy, held in a private temporary file during the run, and deleted.
- The Entra client secret is kept encrypted for the operator's Windows account (`infra/.entra.local.json`,
  git-ignored) and never printed.

## Consequences
- Keys for Postgres, Redis, Blob storage and Azure OpenAI still exist (as vault secrets). Using managed identity
  *to those services* would remove them but needs adapter changes (token-based auth for pg8000, Redis, Blob and
  the OpenAI client). This is a recorded follow-up.
- The owner cannot read the secrets either; that is intended and the runbooks say how to grant temporary access.
- Postgres and Redis have public endpoints (TLS plus a password or key; Postgres allows only Azure services).
  Private networking is the production step.

## Alternatives considered
Plain environment variables (secrets in the deployment record), a parameter file (secrets on disk), Entra-based
database authentication (not yet supported by the driver in use).

## Evidence
`tests/test_infra.py`: secret parameters are `@secure()`, no credential literal in any template or script,
every secret environment variable is a reference, the vault and registry use identities and roles.
