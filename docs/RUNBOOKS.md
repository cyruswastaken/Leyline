# Runbooks

How to operate the Azure deployment: deploy it, tear it down, and fix the things that have actually
gone wrong. Each runbook gives the symptom, how to confirm the cause, the fix, and how to check it
worked. The decisions behind the setup are in the [decision records](adr/README.md); the deployment
overview is in [DEPLOYMENT.md](DEPLOYMENT.md).

All commands run from the repository root in Windows PowerShell after `az login`.

## Quick reference

| What | Where |
| --- | --- |
| Web app | `https://leyline-web.<generated>.koreacentral.azurecontainerapps.io` (print it: `az containerapp show -n leyline-web -g rg-leyline-app --query properties.configuration.ingress.fqdn -o tsv`) |
| Disposable resources | resource group `rg-leyline-app` (Postgres, Redis, Key Vault, Storage, registry, Container Apps, logs) |
| Kept resources | `rg-leyline` (Azure OpenAI account `leyline-openai` and its two model deployments), the Entra app `Leyline Helpdesk`, the budget `leyline-monthly` |
| Containers | `leyline-web`, `leyline-worker`, and the one-off jobs `leyline-task` (migrations) and `leyline-seed` (corpus) |
| Region | Korea Central (the subscription's policy also allows Central India, East Asia, UAE North, Malaysia West) |
| Health | `/healthz` (is the process up) and `/readyz` (is everything it needs reachable) |

| Script | Purpose |
| --- | --- |
| `./infra/deploy.ps1` | Deploy or update everything (`-WhatIf` previews, `-InfraOnly`, `-Seed`, `-ScaleToZero`) |
| `./infra/teardown.ps1` | Delete `rg-leyline-app` and purge the Key Vault (`-IncludeModels`, `-Force`) |
| `./infra/entra.ps1` | Entra app, roles, groups, secret, demo users (`-DemoUsers`, `-ResetPasswords`, `-NewSecret`, `-Remove`) |
| `./infra/budget.ps1` | Monthly cost alert |
| `./infra/logs.ps1` | Container logs without any CLI extension (`-App`, `-Minutes`, `-Errors`, `-System`, `-NoProbes`) |

## 1. Deploy, update, tear down, rebuild

**Preview, then deploy.** The preview creates nothing:

```powershell
./infra/deploy.ps1 -WhatIf
```

```powershell
./infra/deploy.ps1 -Seed
```

`deploy.ps1` is repeatable. It builds the image, pushes it, deploys the apps, runs the migrations,
registers the sign-in address in Entra, and (with `-Seed`) queues the sample corpus. A plain re-run is how
you ship a code or configuration change. Each run generates a **new Postgres password** and restarts the
apps so they pick it up, which is also a free password rotation.

**What a deploy does not catch.** `-WhatIf` and Azure's own validation do not run the real calls, so they
passed templates that then failed. If a deploy fails, read the error, fix it, and run it again: it only
fills in what is missing. See section 11 for the failures seen so far.

**Tear down** (stops the spending, about 40 INR a day while it runs):

```powershell
./infra/teardown.ps1 -WhatIf
```

```powershell
./infra/teardown.ps1
```

It asks you to type the resource group name. **Everything in `rg-leyline-app` is deleted for good,
including the database**: documents, conversations, tickets, audit and security events, uploaded files and
the 7-day database backups go with the server. What survives: the Azure OpenAI account and its model
deployments, the Entra app and its secret file, the budget, and the git repository. The Key Vault is purged
so its name can be reused.

**Rebuild from nothing:**

```powershell
./infra/deploy.ps1 -Seed
```

This recreates everything and loads the 49 sample documents. The web address changes (a new generated
domain), and `deploy.ps1` registers the new sign-in address in Entra for you. Users have to ask their
questions again: conversations and tickets are not restored.

## 2. The app is not ready (`/readyz` returns 503)

`/readyz` lists what it checked. Open it in a browser or:

```powershell
$fqdn = az containerapp show -n leyline-web -g rg-leyline-app --query properties.configuration.ingress.fqdn -o tsv
(Invoke-WebRequest -Uri "https://$fqdn/readyz" -UseBasicParsing).Content
```

| Check that is `false` | Meaning | First thing to try |
| --- | --- | --- |
| `database_reachable` | cannot connect to Postgres | section 6 |
| `schema_current` | the database is behind the code | run the migrations: `az containerapp job start --name leyline-task --resource-group rg-leyline-app` |
| `redis_reachable` | sessions and rate limits cannot be stored; the app then fails closed (503) for signed-in pages | section 7 |
| `blob_storage_reachable` | uploads cannot be stored | check the storage account in the portal; check `leyline-web` logs |
| `llm_configured` | the model settings are missing | redeploy; compare `LLM_BASE_URL` / `LLM_MODEL` on the container |
| `oidc_configured` | sign-in settings are missing | redeploy with the saved Entra values (`./infra/deploy.ps1`) |

Always read the logs for the reason:

```powershell
./infra/logs.ps1 -App web -NoProbes -Errors -Minutes 60
```

A container that crash-loops still shows as "Running" in the portal. Look at the platform events:

```powershell
./infra/logs.ps1 -App leyline-worker -System -Minutes 30
```

## 3. Sign-in fails

| What you see | Cause | Fix |
| --- | --- | --- |
| Microsoft error **AADSTS50011** (reply address does not match) | the app's address is not registered in Entra (it was lost once, see section 11) | `./infra/entra.ps1 -RedirectUri https://<fqdn>/auth/callback` (this keeps the other addresses) |
| Microsoft error **AADSTS50105** (user not assigned to a role) | the app requires assignment and this person has no role | `./infra/entra.ps1 -DemoUsers` for demo users, or assign the user in Entra > Enterprise applications > Leyline Helpdesk > Users and groups |
| Microsoft error **AADSTS7000222** or **7000215** (secret expired or wrong) | the client secret is older than 180 days or was replaced | section 8 |
| The styled **"Sign-in failed"** page (HTTP 400) from our app | the callback was rejected: a stale or replayed sign-in, a clock or state problem, or the secret | try again from the welcome page; if it repeats read `./infra/logs.ps1 -App web -NoProbes -Errors`; the app deliberately logs only the error class, never details |
| You sign in but see no documents | the group claim is not mapped | section 5 |
| `localhost:8080` appears | you are using the local copy of the app (Keycloak), not Azure | use the Azure address |

The **groups the app sees** are decided only from the token's group object IDs through the map in
`OIDC_GROUP_NAMES`; a group that is not in the map grants nothing. To see what the live map contains:

```powershell
$sub = az account show --query id -o tsv
(az rest --method get --url "https://management.azure.com/subscriptions/$sub/resourceGroups/rg-leyline-app/providers/Microsoft.App/containerApps/leyline-web?api-version=2024-03-01" | ConvertFrom-Json).properties.template.containers[0].env | Where-Object { $_.name -eq 'OIDC_GROUP_NAMES' }
```

## 4. Chat refuses everything, or answers badly

**"I couldn't find relevant information" for everything.** This happened on the first Azure test. The relevance
floors are calibrated per embedding model, and the defaults suit `nomic-embed-text`, not
`text-embedding-3-small`. The Azure floors are set in `infra/modules/apps.bicep`
(`SEARCH_MIN_SIMILARITY=0.37`, `SEARCH_MIN_LEXICAL=0.60`) and gated by `tests/test_evals_azure.py`.
Check the live values are those (see the command in section 3, with `SEARCH_` instead of `OIDC_`). If you
change the embedding model, the corpus or the chunker, re-record and re-sweep exactly as in
[EVALUATION.md](EVALUATION.md); never lower a floor to make something pass.

**An answer cites nothing or is wrong.** Retrieval and answer quality are separate. Retrieval is measured
(`python -m leyline.evals`); the model's wording is measured by hand with `python -m leyline.evals.answers`.
Model output is verified and redacted by the app, so a citation that does not match a retrieved document is
removed rather than shown.

**Documents are missing.** They may not be indexed yet: section 5.

## 5. Documents are not indexed, or the worker is unhealthy

The worker takes queued uploads, splits and embeds them, and stores them. Check it:

```powershell
./infra/logs.ps1 -App worker -Minutes 15 -NoProbes
```

Healthy lines look like `outcome=indexed`. Failure patterns seen so far:

| Log line | Cause | Fix |
| --- | --- | --- |
| `SESSION_COOKIE_SECURE must be true in production` | a container was missing a required production setting | every container shares one environment block in `apps.bicep`; redeploy |
| `relation "ingestion_jobs" does not exist` | the migrations have not run | `az containerapp job start --name leyline-task --resource-group rg-leyline-app` |
| `'str' object has no attribute 'wrap_socket'` | the database URL's TLS option was passed as text | fixed in code (`Database` converts `ssl_context=true`); redeploy a current build |
| HTTP 429 or 5xx from the embeddings endpoint | the Azure OpenAI quota or an outage | wait; jobs retry up to 5 times with back-off; check the quota on `leyline-openai` |
| `MissingEmbeddingError` | only in evaluation tools: the recorded embeddings do not match | re-record (EVALUATION.md) |

**Load the sample corpus again** (safe: unchanged files are skipped):

```powershell
./infra/deploy.ps1 -Seed
```

or just `az containerapp job start --name leyline-seed --resource-group rg-leyline-app`.

## 6. The database

- **Cannot connect.** The server accepts connections only from Azure services (a firewall rule), so you cannot
  reach it from your PC; that is by design. Use the app and its logs. Check the server is running:
  `az postgres flexible-server list -g rg-leyline-app --query "[].{n:name,s:state}" -o table`. If it says
  `Stopped`, start it with `az postgres flexible-server start`.
- **Password.** Every `deploy.ps1` run sets a new password and rewrites the `database-url` secret in Key Vault;
  the apps restart to pick it up. If the apps cannot connect after a partial deploy, run `deploy.ps1` again.
- **Restore.** Backups are kept 7 days (single zone, no geo-redundancy). Restore to a *new* server with
  `az postgres flexible-server restore`, then point `DATABASE_URL` at it. Teardown deletes the backups.
- **TLS** is required and verified against the system's trusted roots (DigiCert and Microsoft roots are in
  the image).

## 7. Redis

Redis (Azure Managed Redis B0) holds sessions and rate-limit counters only, and nothing is persisted.
If it restarts, everyone is signed out; that is expected. If it is unreachable the app fails **closed**
(503) rather than treating anyone as signed in. Check its state in the portal (Resource group > Redis > Overview:
`Running`). The connection string lives in Key Vault as `redis-url`.

## 8. Secrets and rotation

| Secret | Where | Lifetime and rotation |
| --- | --- | --- |
| Entra client secret | encrypted for your Windows account in `infra/.entra.local.json`, and Key Vault `oidc-client-secret` | 180 days from creation (the file records `secretExpires`). Rotate: `./infra/entra.ps1 -NewSecret`, then `./infra/deploy.ps1` |
| Postgres password | Key Vault `database-url` | new on every deploy |
| Azure OpenAI key | Key Vault `openai-api-key` | rotate the key on the account in the portal, then `./infra/deploy.ps1` (it reads the current key) |
| Storage key | Key Vault `blob-connection-string` | rotate on the storage account, then `./infra/deploy.ps1` |
| Redis key | Key Vault `redis-url` | rotate on Redis, then `./infra/deploy.ps1` |
| Demo user passwords | `infra/.entra-demo-users.local.txt` (git-ignored, plain text) | `./infra/entra.ps1 -DemoUsers -ResetPasswords`; delete the file and the users with `-Remove` when finished |

You, as owner, cannot read Key Vault secrets (the vault uses role-based access and only the app's identity
has the "Secrets User" role). That is intended. To read one yourself, grant your account the "Key Vault
Secrets Officer" role on the vault, and remove it afterwards.

After any secret change the apps must restart; `deploy.ps1` does that.

## 9. The model is slow or unavailable

- **Slow first words.** `gpt-5.4-mini` reasons before it answers, so a pause of a few seconds is normal. The
  app waits up to 55 seconds (`LLM_TIMEOUT_SECONDS`).
- **"Temperature" errors (HTTP 400).** The GPT-5 family accepts only its default temperature; the template sets
  `LLM_SEND_TEMPERATURE=false`. Keep it off for these models.
- **Failures in a row.** A circuit breaker opens after repeated failures and answers "unavailable" immediately
  for 30 seconds instead of making users wait. The log line carries the class only (`timeout`, `http_429`,
  `connection_error`).
- **Quota (429).** Each deployment has a capacity (50 thousand tokens a minute). Raise it on the account or
  retry later.
- **A deployment is missing.** `az cognitiveservices account deployment list -g rg-leyline -n leyline-openai -o table`
  should list `gpt-5.4-mini` and `text-embedding-3-small` as `Succeeded`; `./infra/deploy.ps1` recreates them.

## 10. Cost

- A budget alert (`leyline-monthly`, 4000 INR a month) emails at 50%, 80%, 100% of actual spend and when Azure
  forecasts 100%. **It never stops anything.**
- The running stack costs roughly 40 INR a day (Postgres B1ms, Redis B0, container registry, Container Apps,
  logs). That is an estimate: check Cost Management > Cost analysis for the real figure.
- With a student credit, tear down when you are not demonstrating, and rebuild with `./infra/deploy.ps1 -Seed`.
- `./infra/budget.ps1 -Email <address> -Amount 4000` creates or updates the alert; `-Remove` deletes it.

## 11. Deploy errors seen in practice

| Error | Cause | Fix |
| --- | --- | --- |
| `RequestConflict ... provisioning state is not terminal` on the OpenAI account | the key was read while model deployments were being written | fixed: the key is read in `modules/openai-key.bicep` after the deployments; re-run |
| `ExpressKeyVaultSecretsNotSupported`, `Job resources are not supported on express environments` | this subscription creates Container Apps environments as *express* unless told otherwise | fixed: `environmentMode: 'WorkloadProfiles'` in `apps.bicep`; delete an express environment (it is empty) and redeploy |
| `Conflict ... resource is new, but already exists ... pending delete` on `leyline-web` | a previous failed attempt left a delete pending | wait a few minutes and re-run |
| `SESSION_COOKIE_SECURE must be true` in a worker or job | a required setting was only on the web app | fixed: all containers share one environment block |
| `'str' object has no attribute 'wrap_socket'` | TLS option for Postgres passed as text | fixed in `Database` |
| Migration job runs `python -m " leyline.db upgrade"` and fails | `az ... --args` splits `-m` wrongly | fixed: each job's own command is built in; the script overrides nothing |
| The deploy script reports "Migration job ended with status Failed" | read the real error: `./infra/logs.ps1 -App leyline-task -Errors` | depends on the line |
| `The command requires the extension containerapp` / `EOF when reading a line` | the CLI wants to install an extension and has no keyboard | use `./infra/logs.ps1` and `az rest` instead, or install the extension yourself |
| Key Vault name already taken after a teardown | the deleted vault is held for 7 days | `teardown.ps1` purges it; if it could not, run `az keyvault purge --name <name>` |
| Microsoft: redirect address mismatch right after a re-run of `entra.ps1` | an old version replaced the address list | fixed (addresses are merged); re-register with `-RedirectUri` |
| Demo users cannot sign in (AADSTS50105) | a role assignment silently failed | fixed (retries, reports, verifies); re-run `./infra/entra.ps1 -DemoUsers` |

## 12. Suspected data exposure (a person saw a document they should not)

Treat it as a security incident:

1. **Contain.** Scale `leyline-web` to zero replicas in the portal (Container Apps > leyline-web > Scale), or
   run `./infra/teardown.ps1`. The worker does not serve users.
2. **Establish what happened.** Security events are stored in the database and shown on the **Signals** page to
   a security auditor (`/monitoring`); filter by type (`SECURITY_RULE_TRIGGERED`, `INJECTION_SIGNAL`,
   `PROMPT_LEAK_BLOCKED`, `AUTH_FAILED`). The events table is append-only. Azure OpenAI diagnostics hold
   metadata, not conversations, so do not claim full conversation auditing.
3. **Re-run the gates against the live configuration.** `acl_leaks` must be 0 and `restricted_pass_rate` 1.0:
   `python -m leyline.evals --index hybrid --embeddings evals/embeddings.azure.json` with the deployed floors
   (see EVALUATION.md). A leak is a regression of the permission filter: `readable_by` in SQL and `authorize_hits`
   in Python.
4. **Check the access data.** A user's groups are captured at sign-in and last until the session ends (12 hours
   maximum, 30 minutes idle). A group change in Entra applies at the next sign-in; a document's own ACL changes
   apply immediately. Check the group memberships and the `OIDC_GROUP_NAMES` map (section 3).
5. **Recover.** Fix, redeploy, and add a regression test (`tests/test_regressions.py`) before reopening.

## 13. Demo users

```powershell
./infra/entra.ps1 -DemoUsers -WhatIf
```

```powershell
./infra/entra.ps1 -DemoUsers -ResetPasswords
```

Creates (or completes) seven users like the local Keycloak ones, gives them their groups and roles, and
verifies the result; it exits with an error if anything is missing, and re-running only fills in what is missing.
Passwords go to `infra/.entra-demo-users.local.txt`, never to the screen. Sign in as each in a private window on
the **Azure** address. Remove them (and the whole registration) with `./infra/entra.ps1 -Remove`.
