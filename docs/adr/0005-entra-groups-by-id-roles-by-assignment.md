# 0005. Entra: group IDs mapped to names, app roles, assignment required

**Status:** Accepted (Phase 8)

## Context
Documents name their audiences (`hr`, `finance`, `security`, `staff`). Entra puts group **object IDs** in the
token, and the free tenant tier cannot send names. App functions (knowledge admin, security auditor, support
agent) are separate from document access.

## Decision
- App roles (`user`, `kb_admin`, `support_agent`, `security_auditor`) carry the functions in the `roles` claim.
- Four security groups (`Leyline staff/security/hr/finance`) carry document access in the `groups` claim.
- The app translates group IDs to names with a configured map (`OIDC_GROUP_NAMES`). With a map configured, a
  group that is not in it grants nothing, and a token entry that merely spells `hr` is ignored.
- The service principal requires assignment: someone in the directory with no role cannot sign in at all.
- Overage (more than 200 groups) is detected and fails closed.

## Consequences
- A new group needs a map entry (`infra/entra.ps1` writes it) and a redeploy.
- Group membership is read at sign-in; changes apply at the next sign-in.
- Company branding on the free tier is limited, so the sign-in page is Microsoft's, not the app's themed page.

## Alternatives considered
Names in the token (needs a paid tier or on-premises sync), resolving groups through Microsoft Graph at
sign-in (more moving parts and permissions), using roles for document access (would blur function and access).

## Evidence
Unit tests for the mapping and its denied paths (`tests/test_principal_and_authorization.py`), the live map
matching Entra's four group IDs, and real sign-ins on Azure. An early version of the script removed the Azure
redirect address and left six demo users without roles; both are fixed and guarded.
