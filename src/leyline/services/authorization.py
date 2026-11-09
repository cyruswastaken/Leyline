"""Document authorization: the one place that decides who may read what.

A document is readable by a principal when the document's ACL contains a
group the principal belongs to, or the reserved "everyone" entry. A document
with an empty ACL is readable by nobody. Application roles (kb_admin,
security_auditor, ...) never grant read access to content.

Three places depend on this module and must stay consistent:

* ``can_read``        - pure check, used in tests and for display decisions
* ``readable_by``     - the SQL predicate every search adapter must apply
* ``authorize_hits``  - re-checks hits against the database just before they
                        are used, so an ACL change or deletion applies
                        immediately even if a search index lags behind

``tests/test_acl_consistency.py`` asserts that the first two agree.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import ColumnElement, exists, or_, select

from leyline.auth.principal import Principal
from leyline.db.database import Database
from leyline.db.models import EVERYONE_GROUP, Document, DocumentAcl
from leyline.ports.search import SearchHit


def can_read(principal: Principal, acl_groups: Iterable[str]) -> bool:
    groups = set(acl_groups)
    return EVERYONE_GROUP in groups or not principal.groups.isdisjoint(groups)


def readable_by(principal: Principal) -> ColumnElement[bool]:
    """SQL predicate over ``documents``: rows ``principal`` may read."""
    matches = [DocumentAcl.group_id == EVERYONE_GROUP]
    if principal.groups:
        matches.append(DocumentAcl.group_id.in_(sorted(principal.groups)))
    return exists().where(DocumentAcl.document_id == Document.id, or_(*matches))


def authorize_hits(
    database: Database, principal: Principal, hits: Sequence[SearchHit]
) -> list[SearchHit]:
    """Keep only hits that are still current, undeleted and readable *now*."""
    if not hits:
        return []

    document_ids = {hit.document_id for hit in hits}
    with database.session() as session:
        rows = session.execute(
            select(Document.id, Document.current_version_id).where(
                Document.id.in_(document_ids),
                Document.deleted_at.is_(None),
                readable_by(principal),
            )
        ).all()

    current = {(row.id, row.current_version_id) for row in rows}
    return [hit for hit in hits if (hit.document_id, hit.version_id) in current]
