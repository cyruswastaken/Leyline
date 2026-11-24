"""Evaluate how follow-up questions are searched.

A follow-up like "and on a Mac?" cannot be searched as typed. The app can ask the
chat model for a standalone rewrite, which costs a model round trip, or skip the
rewrite when the question already stands alone (``services/rewrite_policy.py``).
This module measures the trade-off on conversations written by hand:

* ``never``  - search the message as typed (the cheapest policy)
* ``always`` - search the author's ideal rewrite (what a perfect model would do;
               the ceiling, since a real model's rewrite is not measured here)
* ``policy`` - the shipped behaviour: the message as typed when
               ``needs_rewrite`` says it stands alone, the ideal rewrite otherwise

The policy is good when it keeps ``always``'s recall while skipping many rewrites.
Every query is also checked for ACL leaks against the corpus ACLs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from leyline.evals.golden import GoldenError, persona_principal
from leyline.evals.runner import RECALL_KS, ordered_documents
from leyline.ports.search import SearchIndex
from leyline.services.authorization import can_read
from leyline.services.rewrite_policy import needs_rewrite

Kind = Literal["answerable", "restricted"]
POLICIES = ("never", "always", "policy")


@dataclass(frozen=True)
class FollowupCase:
    id: str
    persona: str
    kind: Kind
    history: tuple[str, ...]
    message: str
    standalone: str
    needs_rewrite: bool  # the author's judgement, for reporting the policy's accuracy
    expect: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass(frozen=True)
class FollowupReport:
    summary: dict[str, float]
    misses: list[str]  # human-readable notes on cases the policy got wrong


def load_followups(
    path: Path, oracle: Mapping[str, frozenset[str]]
) -> list[FollowupCase]:
    cases: list[FollowupCase] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        where = f"{path.name}:{number}"
        try:
            raw = json.loads(line)
            case = FollowupCase(
                id=raw["id"],
                persona=raw["persona"],
                kind=raw["kind"],
                history=tuple(raw["history"]),
                message=raw["message"],
                standalone=raw["standalone"],
                needs_rewrite=bool(raw["needs_rewrite"]),
                expect=tuple(raw.get("expect", ())),
                forbid=tuple(raw.get("forbid", ())),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise GoldenError(f"{where}: invalid case ({exc})") from None
        _validate(case, oracle, where)
        if case.id in seen:
            raise GoldenError(f"{where}: duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise GoldenError(f"{path.name}: no cases")
    return cases


def _validate(
    case: FollowupCase, oracle: Mapping[str, frozenset[str]], where: str
) -> None:
    if not case.history or not all(q.strip() for q in case.history):
        raise GoldenError(f"{where}: a follow-up needs earlier questions")
    if not case.message.strip() or not case.standalone.strip():
        raise GoldenError(f"{where}: empty message or standalone query")
    principal = persona_principal(case.persona)
    for ref in (*case.expect, *case.forbid):
        if ref not in oracle:
            raise GoldenError(f"{where}: unknown document {ref!r}")
    if case.kind == "answerable":
        if not case.expect or case.forbid:
            raise GoldenError(f"{where}: answerable cases need only 'expect'")
        for ref in case.expect:
            if not can_read(principal, oracle[ref]):
                raise GoldenError(f"{where}: {case.persona} cannot read {ref}")
    elif case.kind == "restricted":
        if not case.forbid or case.expect:
            raise GoldenError(f"{where}: restricted cases need only 'forbid'")
        for ref in case.forbid:
            if can_read(principal, oracle[ref]):
                raise GoldenError(f"{where}: {case.persona} CAN read {ref}")
    else:
        raise GoldenError(f"{where}: unknown kind {case.kind!r}")


def _query_for(policy: str, case: FollowupCase) -> str:
    if policy == "never":
        return case.message
    if policy == "always":
        return case.standalone
    return case.standalone if needs_rewrite(case.message) else case.message


def run_followups(
    search: SearchIndex,
    cases: Sequence[FollowupCase],
    *,
    refs: Mapping[uuid.UUID, str],
    acl: Mapping[str, frozenset[str]],
    limit: int = 20,
) -> FollowupReport:
    answerable = [c for c in cases if c.kind == "answerable"]
    ranks: dict[str, dict[str, int | None]] = {p: {} for p in POLICIES}
    leaks = 0
    restricted_failed: set[str] = set()
    misses: list[str] = []

    for case in cases:
        principal = persona_principal(case.persona)
        for policy in POLICIES:
            hits = search.search(principal, _query_for(policy, case), limit=limit)
            returned, _ = ordered_documents(hits, refs)
            leaks += sum(
                1
                for ref in returned
                if not can_read(principal, acl.get(ref, frozenset()))
            )
            if case.kind == "restricted":
                if any(ref in case.forbid for ref in returned):
                    restricted_failed.add(case.id)
                continue
            ranks[policy][case.id] = next(
                (i for i, ref in enumerate(returned, 1) if ref in case.expect), None
            )

    summary: dict[str, float] = {}
    for policy in POLICIES:
        for k in RECALL_KS[-1:]:
            summary[f"{policy}_recall@{k}"] = _share(
                (ranks[policy][c.id] or 99) <= k for c in answerable
            )
        summary[f"{policy}_mrr"] = (
            sum(1.0 / r for c in answerable if (r := ranks[policy][c.id]))
            / len(answerable)
            if answerable
            else 0.0
        )
    summary["skip_rate"] = _share(not needs_rewrite(c.message) for c in cases)
    summary["policy_agrees_with_author"] = _share(
        needs_rewrite(c.message) == c.needs_rewrite for c in cases
    )
    restricted = [c for c in cases if c.kind == "restricted"]
    summary["restricted_pass_rate"] = _share(
        c.id not in restricted_failed for c in restricted
    )
    summary["acl_leaks"] = float(leaks)

    for case in answerable:
        if (ranks["policy"][case.id] or 99) > 5:
            never, always = ranks["never"][case.id], ranks["always"][case.id]
            misses.append(
                f"[{case.id}] policy missed {list(case.expect)} "
                f"(never rank {never}, always rank {always})"
            )
    return FollowupReport(summary=summary, misses=misses)


def _share(flags: object) -> float:
    items = list(flags)  # type: ignore[call-overload]
    return sum(1 for flag in items if flag) / len(items) if items else 0.0


def format_followups(report: FollowupReport) -> str:
    s = report.summary
    lines = [
        "follow-ups (recall@5 / mrr by policy):",
        f"  never   (as typed)      {s['never_recall@5']:.3f}  {s['never_mrr']:.3f}",
        f"  always  (ideal rewrite) {s['always_recall@5']:.3f}  {s['always_mrr']:.3f}",
        f"  policy  (shipped)       {s['policy_recall@5']:.3f}  {s['policy_mrr']:.3f}",
        f"  rewrites skipped        {s['skip_rate']:.3f}",
        f"  policy agrees w/author  {s['policy_agrees_with_author']:.3f}",
        f"  restricted_pass_rate    {s['restricted_pass_rate']:.3f}",
        f"  acl_leaks               {int(s['acl_leaks'])}       (MUST be 0)",
    ]
    lines += [f"  {m}" for m in report.misses]
    return "\n".join(lines)
