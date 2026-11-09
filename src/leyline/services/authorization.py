"""Document authorization.

Decides which documents a ``Principal`` may read. This runs before chunking,
retrieval and prompt construction, so unauthorized content never reaches the
retrieval layer or the language model.

Interim model (until per-document group ACLs arrive with the database in
Phase 2): a "general" document needs only a signed-in user, and a
"restricted" document additionally needs membership in one of the configured
restricted groups. Anything else, including a document with a missing or
unknown access level, is denied to everyone.
"""

from __future__ import annotations

from typing import Any

from leyline.auth.principal import Principal

GENERAL = "general"
RESTRICTED = "restricted"


def can_access(
    access_level: object,
    principal: Principal,
    *,
    restricted_groups: frozenset[str],
) -> bool:
    if access_level == GENERAL:
        return True
    if access_level == RESTRICTED:
        # An empty configured group set grants nobody access.
        return bool(restricted_groups) and principal.in_any_group(restricted_groups)
    return False


def filter_authorized_documents(
    documents: list[dict[str, Any]],
    principal: Principal,
    *,
    restricted_groups: frozenset[str],
) -> list[dict[str, Any]]:
    """Return only documents the principal is authorized to read."""
    return [
        document
        for document in documents
        if can_access(
            document.get("access_level"),
            principal,
            restricted_groups=restricted_groups,
        )
    ]
