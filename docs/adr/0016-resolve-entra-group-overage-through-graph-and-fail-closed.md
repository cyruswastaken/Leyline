# 0016. Resolve Entra group overage through Graph, and fail closed

**Status:** Accepted (Phase 11). Built and tested against a fake Graph; **not yet tried against a real tenant**, and
off until an administrator grants the permission.

## Context
Entra puts at most 200 groups in a token. A user in more gets an "overage" marker instead of a list. Leyline already
treated that as "groups unknown" and granted only `*` documents, which is safe but locks such a person out of
everything restricted.

## Decision
- With `OIDC_RESOLVE_GROUP_OVERAGE`, sign-in asks Microsoft Graph (`/me/getMemberObjects`, the user's own delegated
  token, scope `GroupMember.Read.All`) for the user's groups and maps them through `OIDC_GROUP_NAMES`.
- Only the configured Graph address is ever called, never a URL from the token (the `_claim_sources` endpoint is
  ignored); paging follows a next link only on the same host.
- Only groups mapped to a name count; an unmapped id grants nothing, exactly as for groups in a token.
- Any failure (no permission, throttling, timeout, a bad reply, an unexpected error) keeps the fail-closed result: the
  user signs in with no groups beyond `*`, the login event says why, and nothing is ever granted by the failure.
- The access token is used for that one call and is neither stored in the session nor logged.
- Startup refuses the option without `OIDC_GROUP_NAMES`, without the scope, or with a non-HTTPS Graph address.

## Consequences
- A user in over 200 groups regains access to the documents their groups allow, but only as of sign-in (like every other
  group change; a session lasts up to 12 hours).
- It needs a tenant administrator to grant `GroupMember.Read.All` to the app, a broad read permission. That grant
  is a tenant change and was **not made**; the code is dormant until it is.
- One more outbound call at sign-in, only for the users that need it.

## Alternatives considered
Entra group claims restricted to groups assigned to the application (a tenant setting that avoids overage but cuts
what the token carries); an application permission read by the server (works without the user's token but gives the
server standing rights to read the whole directory); doing nothing (safe, but those users lose restricted access).

## Evidence
`tests/test_group_overage.py`: ids map to names and unmapped ones are dropped; every failure mode keeps the user signed
in with no extra access; nothing is looked up when the option is off or the token is complete; the access token is not
in the session store or the events; a resolved user can read a restricted document the same user cannot when Graph is
down; the client never follows a foreign link or a URL from the token.
