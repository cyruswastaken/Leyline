# 0009. CI deploys updates only, started by hand, with a narrow identity

**Status:** Accepted (Phase 8). Not yet exercised by a real GitHub run.

## Context
The repository is **public** and a deployment pipeline holds real power over a paid subscription. The
environment is rebuilt rarely (and its web address changes when it is), while code and configuration changes
are frequent. Rebuilding also needs Microsoft Entra changes (the sign-in address), which is a much larger
privilege than updating the apps.

## Decision
- The `Deploy to Azure` workflow **updates a running environment** (build the image, update the apps, run the
  migrations, restart, check `/readyz`). It refuses to create the environment from nothing and never touches
  Entra. First-time setup and rebuilds stay with `infra/deploy.ps1` on a workstation.
- It starts **only by hand** (`workflow_dispatch`), only from `main`, and only when CI and Security succeeded for
  that exact commit. The default action is `preview` (Azure's what-if); `deploy` is a deliberate choice.
- The `azure` GitHub environment requires an approval by a named reviewer and accepts only `main`.
- The workflow signs in with **OIDC federation**: no Azure password, key or secret is stored in GitHub. The
  identity's federated credential trusts exactly one subject: this repository's `environment:azure`. The
  repository uses GitHub's immutable subjects, so the subject names owner and repository by id
  (`repo:<owner>@<id>/<repo>@<id>:environment:azure`); `ci-identity.ps1` reads the prefix from GitHub. (The
  first run failed with AADSTS700213 because the credential had been created from the plain names.)
- The identity's rights are narrow (set by `infra/ci-identity.ps1`): a custom subscription role that can start
  and read deployments and create or update resource groups but not delete; a custom role on `rg-leyline-app`
  that is Contributor without deleting the group and without writing role assignments; role-assignment rights
  limited by a condition to the two roles the template assigns; AcrPush; and Cognitive Services Contributor on
  the single OpenAI account.
- Sign-in settings are read from the live app (the source of truth), and the template's `writeOidcSecret=false`
  leaves the stored Entra client secret untouched, so GitHub holds no Entra secret either.
- Third-party actions are pinned to a commit, and the workflows are guarded by tests.

## Consequences
- A change to the apps ships with a click and an approval, with the same checks as everything else.
- After a **full rebuild** (teardown deletes `rg-leyline-app` and the role assignments inside it) the identity
  script must be run again.
- Whoever can get a deployment approved can run code in the pipeline with the identity's rights: change or delete
  resources inside `rg-leyline-app` (not the group itself), read its keys, and call the OpenAI account.
- With one maintainer, "self review" is allowed, so the approval is a deliberate pause and a guard against
  accidents and edited workflows, not a four-eyes control. A second reviewer would change that.
- The database password is regenerated on every pipeline deploy (as in `deploy.ps1`), and the apps restart.

## Alternatives considered
Deploy automatically on every push to `main` (spends credit and changes the live site on each merge); a stored
service-principal secret in GitHub (a long-lived secret in a public repository's settings); Contributor at the
subscription (far too broad); full rebuilds from CI (would need permission to edit the Entra app registration).

## Evidence
`tests/test_workflows.py`: manual trigger only, preview by default, the gate and the approval environment, no stored
secret and no hard-coded Azure id, an identity token only in the Azure job, no workflow expression inside a shell
script, no deletion and no Entra access in the workflow, the exact federated subject, the role limits and the
condition that allows only the two template roles. Azure's what-if run with the workflow's own parameters shows an
update of the existing environment with nothing created.
