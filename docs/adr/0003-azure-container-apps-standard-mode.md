# 0003. Azure Container Apps (standard mode) instead of App Service

**Status:** Accepted (Phase 8). Changes the original plan, which named App Service.

## Context
The same image runs three ways: the web app, the ingestion worker, and one-off jobs (migrations, corpus
seeding). The environment is meant to be created and deleted often to save credit.

## Decision
Run all of them on Azure Container Apps (consumption plan), pulling the image from Azure Container Registry with
a user-assigned managed identity. Set the environment mode **explicitly** to `WorkloadProfiles`.

## Consequences
- Jobs, secret references to Key Vault and scale-to-zero are available; teardown and rebuild are quick.
- On this subscription an environment created without a mode came out as **express**, which has no Key Vault
  references and no jobs; the first two pass-2 attempts failed on it. The template now asks for the standard
  mode and a test keeps it that way.
- Container Apps cannot run an exec health probe, so the worker's heartbeat-file check is not available; the
  platform restarts a worker whose process exits, but a hung worker is not detected.
- A crash-looping container still shows as "Running" in the portal; the runbooks say how to look (RUNBOOKS.md,
  section 2).

## Alternatives considered
App Service (no jobs, a plan that bills even when idle), AKS (far more to run), express (lacks the features
needed here).

## Evidence
The `ExpressKeyVaultSecretsNotSupported` errors, the `environmentMode` fix and `tests/test_infra.py`.
