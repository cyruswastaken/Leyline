"""The authenticated caller.

A ``Principal`` is built only from a verified OIDC ID token (or restored from
the server-side session that was created from one). Nothing in a request
URL, query string, body or header may ever construct one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AppRole(StrEnum):
    """Application functions, carried in the token's roles claim.

    These are separate from document-access groups: a role says what a
    person may *do* in Leyline, a group says which documents they may *read*.
    """

    USER = "user"
    KB_ADMIN = "kb_admin"
    SUPPORT_AGENT = "support_agent"
    SECURITY_AUDITOR = "security_auditor"


class InvalidClaimsError(ValueError):
    """The identity token did not contain a usable identity."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str = ""
    email: str = ""
    groups: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[AppRole] = field(default_factory=lambda: frozenset({AppRole.USER}))
    # True when the identity provider signalled that the token omitted some
    # groups (Entra "overage"). Access then fails closed on missing groups.
    groups_incomplete: bool = False

    def has_role(self, role: AppRole) -> bool:
        return role in self.roles

    def has_any_role(self, roles: frozenset[AppRole]) -> bool:
        return not self.roles.isdisjoint(roles)

    def in_any_group(self, groups: frozenset[str]) -> bool:
        return not self.groups.isdisjoint(groups)

    def to_session(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "email": self.email,
            "groups": sorted(self.groups),
            "roles": sorted(role.value for role in self.roles),
            "groups_incomplete": self.groups_incomplete,
        }

    @classmethod
    def from_session(cls, data: object) -> Principal | None:
        """Restore a principal, or None if the stored data is not valid."""
        if not isinstance(data, dict):
            return None
        user_id = data.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            return None
        return cls(
            user_id=user_id,
            name=_as_str(data.get("name")),
            email=_as_str(data.get("email")),
            groups=frozenset(_str_list(data.get("groups"))),
            roles=_parse_roles(_str_list(data.get("roles"))),
            groups_incomplete=data.get("groups_incomplete") is True,
        )


def principal_from_claims(
    claims: Mapping[str, Any],
    *,
    groups_claim: str = "groups",
    roles_claim: str = "roles",
    group_names: Mapping[str, str] | None = None,
) -> Principal:
    """Build a principal from verified ID-token claims.

    ``group_names`` translates the identifiers an identity provider puts in the
    groups claim into the group names documents use in their ACLs (Entra sends
    object IDs, not names). When it is given, a group that is not in it is
    dropped: nobody gains access through a group nobody mapped.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise InvalidClaimsError("token has no subject")

    claim_names = claims.get("_claim_names")
    overage = isinstance(claim_names, Mapping) and groups_claim in claim_names

    groups = _str_list(claims.get(groups_claim))
    if group_names:
        groups = [group_names[g] for g in groups if g in group_names]

    return Principal(
        user_id=subject,
        name=_as_str(
            claims.get("name")
            or claims.get("preferred_username")
            or claims.get("email")
        ),
        email=_as_str(claims.get("email")),
        groups=frozenset(groups),
        roles=_parse_roles(_str_list(claims.get(roles_claim))),
        groups_incomplete=overage,
    )


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set | frozenset):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _parse_roles(values: list[str]) -> frozenset[AppRole]:
    """Known roles only; unknown strings grant nothing. Everyone is a USER."""
    roles = {AppRole.USER}
    for value in values:
        try:
            roles.add(AppRole(value))
        except ValueError:
            continue
    return frozenset(roles)
