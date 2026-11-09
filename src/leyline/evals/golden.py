"""The golden question set and the oracle it is checked against.

A golden case says who asks, what they ask, and what a *correct* search does:

* ``answerable``   - a document the asker may read answers it; it should rank high.
* ``restricted``   - the real answer lives in a document the asker may NOT read;
                     that document must never be returned.
* ``unanswerable`` - nothing in the knowledge base answers it; a good search
                     returns nothing rather than weakly related text.

Expected documents are named by their stable corpus ``Document ID`` (the
``source_ref`` column). Access is checked against the ACLs declared in the
corpus files themselves, an oracle that is independent of the database and of
the search code under test.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from leyline.auth.principal import Principal
from leyline.seed import SUFFIXES, groups_for
from leyline.services.authorization import can_read
from leyline.services.document_loader import parse_document

Kind = Literal["answerable", "restricted", "unanswerable"]
KINDS: tuple[Kind, ...] = ("answerable", "restricted", "unanswerable")

# Personas mirror the demo users in deploy/keycloak/leyline-realm.json.
PERSONAS: dict[str, tuple[str, ...]] = {
    "employee": ("staff",),
    "security": ("staff", "security"),
    "hr": ("staff", "hr"),
    "finance": ("staff", "finance"),
}


class GoldenError(ValueError):
    """The golden set is malformed or inconsistent with the corpus."""


@dataclass(frozen=True)
class GoldenCase:
    id: str
    persona: str
    question: str
    kind: Kind
    # Any of these documents is an acceptable answer (answerable only).
    expect: tuple[str, ...] = ()
    # These documents must never be returned (restricted only).
    forbid: tuple[str, ...] = ()

    @property
    def principal(self) -> Principal:
        return persona_principal(self.persona)


def persona_principal(name: str) -> Principal:
    try:
        groups = PERSONAS[name]
    except KeyError:
        raise GoldenError(f"unknown persona {name!r}") from None
    return Principal(user_id=f"eval-{name}", name=name, groups=frozenset(groups))


def load_acl_oracle(corpus_dir: Path) -> dict[str, frozenset[str]]:
    """``Document ID`` -> groups, read straight from the corpus files."""
    oracle: dict[str, frozenset[str]] = {}
    for path in sorted(p for p in corpus_dir.rglob("*") if p.suffix in SUFFIXES):
        metadata = parse_document(path)
        ref, groups = metadata.get("id"), groups_for(metadata)
        if ref and groups is not None:
            oracle[ref] = frozenset(groups)
    return oracle


def load_golden(path: Path, oracle: Mapping[str, frozenset[str]]) -> list[GoldenCase]:
    """Read and validate the golden set against the corpus ACLs."""
    cases: list[GoldenCase] = []
    seen: set[str] = set()

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            raw = json.loads(line)
            case = GoldenCase(
                id=raw["id"],
                persona=raw["persona"],
                question=raw["question"],
                kind=raw["kind"],
                expect=tuple(raw.get("expect", ())),
                forbid=tuple(raw.get("forbid", ())),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise GoldenError(f"{path.name}:{number}: invalid case ({exc})") from None

        _validate(case, oracle, where=f"{path.name}:{number}")
        if case.id in seen:
            raise GoldenError(f"{path.name}:{number}: duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)

    if not cases:
        raise GoldenError(f"{path.name}: no cases")
    return cases


def _validate(
    case: GoldenCase, oracle: Mapping[str, frozenset[str]], *, where: str
) -> None:
    if case.kind not in KINDS:
        raise GoldenError(f"{where}: unknown kind {case.kind!r}")
    if not case.question.strip():
        raise GoldenError(f"{where}: empty question")
    principal = case.principal

    for ref in (*case.expect, *case.forbid):
        if ref not in oracle:
            raise GoldenError(f"{where}: unknown document {ref!r}")

    if case.kind == "answerable":
        if not case.expect or case.forbid:
            raise GoldenError(
                f"{where}: answerable cases need 'expect' and no 'forbid'"
            )
        for ref in case.expect:
            if not can_read(principal, oracle[ref]):
                raise GoldenError(
                    f"{where}: {case.persona} cannot read expected document {ref}"
                )
    elif case.kind == "restricted":
        if not case.forbid or case.expect:
            raise GoldenError(
                f"{where}: restricted cases need 'forbid' and no 'expect'"
            )
        for ref in case.forbid:
            if can_read(principal, oracle[ref]):
                raise GoldenError(
                    f"{where}: {case.persona} CAN read {ref}: not a restricted case"
                )
    elif case.expect or case.forbid:
        raise GoldenError(f"{where}: unanswerable cases take no 'expect' or 'forbid'")
