from __future__ import annotations

import re
from collections.abc import Iterable

from leyline.db.models import EVERYONE_GROUP
from leyline.ingestion.parsers import InvalidDocumentError

MAX_TITLE_LENGTH = 200
MAX_GROUPS = 25
# Entra group object ids, Keycloak group names, simple slugs.
_GROUP = re.compile(r"^[A-Za-z0-9._:@/\- ]{1,128}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_title(title: str) -> str:
    cleaned = _CONTROL.sub(" ", title).strip()
    if not cleaned:
        raise InvalidDocumentError("A title is required.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise InvalidDocumentError(
            f"The title is longer than {MAX_TITLE_LENGTH} characters."
        )
    return cleaned


def validate_groups(groups: Iterable[str]) -> frozenset[str]:
    """Normalize an ACL. An empty set is valid: nobody can read the document."""
    cleaned = {group.strip() for group in groups if group.strip()}
    if len(cleaned) > MAX_GROUPS:
        raise InvalidDocumentError(f"At most {MAX_GROUPS} groups are allowed.")
    for group in cleaned:
        if group != EVERYONE_GROUP and not _GROUP.fullmatch(group):
            raise InvalidDocumentError(f"Invalid group name: {group[:40]!r}.")
    return frozenset(cleaned)


def safe_filename(filename: str) -> str:
    """A display-only name: no path components, no control characters."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = _CONTROL.sub("", name).strip()
    return name[:255] or "upload"
