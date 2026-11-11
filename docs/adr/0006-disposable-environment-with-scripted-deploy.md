# 0006. A disposable environment with a scripted, repeatable deploy

**Status:** Accepted (Phase 8)

## Context
The subscription is a student credit of about 100 USD. Running the stack costs roughly 40 INR a day, so it
would last months only if it is switched off when unused. The OpenAI account, by contrast, costs nothing idle.

## Decision
- Two resource groups: `rg-leyline` (kept: the OpenAI account and its two model deployments) and
  `rg-leyline-app` (everything that costs money, deleted by `infra/teardown.ps1`).
- Everything is Bicep plus PowerShell: `deploy.ps1` (two passes: infrastructure and image, then apps and
  migrations), `teardown.ps1`, `entra.ps1`, `budget.ps1`, `logs.ps1`. Each supports `-WhatIf` where it changes
  something, and each is safe to run again.
- A monthly budget alert (4000 INR) warns by email; it never stops anything.

## Consequences
- Teardown deletes the database and its backups: all conversations, tickets and uploads are lost; a rebuild
  reloads only the sample corpus.
- The web address changes with each rebuild; `deploy.ps1` registers the new sign-in address in Entra.
- `what-if` and `validate` did not catch real failures. Five defects only appeared on a real deploy (the key
  read order, the express mode, the worker's production settings, the Postgres TLS option, the job command). Each is
  now guarded by a test, and the runbooks list them.

## Alternatives considered
A permanent environment (spends the credit), portal clicks (not repeatable), Terraform (extra state to manage;
Bicep is native and needs only the Azure CLI).

## Evidence
`tests/test_infra.py` (scope of teardown, secret handling, template invariants), the successful end-to-end
deploy, and the runbooks.
