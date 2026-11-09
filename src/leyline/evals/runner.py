"""Run golden cases against a ``SearchIndex`` and compute retrieval metrics.

Metrics (all over *documents*; consecutive chunks of one document collapse
into a single result, in rank order):

* ``recall@k``             answerable: acceptable document in the top k
* ``mrr``                  answerable: mean reciprocal rank (first acceptable)
* ``refusal_rate``         unanswerable: share where nothing at all was returned
* ``restricted_pass_rate`` restricted: share where no forbidden document was returned
* ``acl_leaks``            EVERY case: documents returned that the asker may not read.
                           Must always be 0; checked against the corpus ACLs.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from leyline.evals.golden import GoldenCase
from leyline.ports.search import SearchHit, SearchIndex
from leyline.services.authorization import can_read

RECALL_KS = (1, 3, 5)
DEFAULT_LIMIT = 20  # chunks requested per question; deduplicated to documents


@dataclass(frozen=True)
class CaseResult:
    case: GoldenCase
    refs: tuple[str, ...]  # distinct documents, best first
    top_score: float | None
    rank: int | None  # 1-based rank of the first acceptable document (answerable)
    leaked: tuple[str, ...]  # returned documents the asker may not read
    passed: bool
    note: str = ""


@dataclass
class EvalReport:
    results: list[CaseResult]
    summary: dict[str, float]
    leaks: list[tuple[str, str]] = field(default_factory=list)  # (case id, document)
    score_samples: dict[str, list[float]] = field(default_factory=dict)

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]


def ordered_documents(
    hits: Sequence[SearchHit], refs: Mapping[uuid.UUID, str]
) -> tuple[list[str], list[float]]:
    """Distinct document refs in rank order, with each document's best score."""
    ordered: list[str] = []
    scores: list[float] = []
    for hit in hits:
        ref = refs.get(hit.document_id, f"<unknown:{hit.document_id}>")
        if ref not in ordered:
            ordered.append(ref)
            scores.append(hit.score)
    return ordered, scores


def run_eval(
    search: SearchIndex,
    cases: Sequence[GoldenCase],
    *,
    refs: Mapping[uuid.UUID, str],
    acl: Mapping[str, frozenset[str]],
    limit: int = DEFAULT_LIMIT,
) -> EvalReport:
    results: list[CaseResult] = []
    leaks: list[tuple[str, str]] = []

    for case in cases:
        principal = case.principal
        hits = search.search(principal, case.question, limit=limit)
        returned, scores = ordered_documents(hits, refs)

        # The one check that must never fail, for every kind of case.
        leaked = tuple(
            ref
            for ref in returned
            if not can_read(principal, acl.get(ref, frozenset()))
        )
        leaks.extend((case.id, ref) for ref in leaked)

        rank: int | None = None
        if case.kind == "answerable":
            rank = next(
                (i for i, ref in enumerate(returned, 1) if ref in case.expect), None
            )
            passed = rank is not None and rank <= RECALL_KS[-1]
            note = "" if passed else f"expected {list(case.expect)}, got {returned[:5]}"
        elif case.kind == "restricted":
            hit_forbidden = [ref for ref in returned if ref in case.forbid]
            passed = not hit_forbidden
            note = "" if passed else f"returned forbidden {hit_forbidden}"
        else:
            passed = not returned
            note = "" if passed else f"should refuse but returned {returned[:3]}"

        if leaked:
            passed, note = False, f"ACL LEAK: {list(leaked)}"

        results.append(
            CaseResult(
                case=case,
                refs=tuple(returned),
                top_score=scores[0] if scores else None,
                rank=rank,
                leaked=leaked,
                passed=passed,
                note=note,
            )
        )

    return EvalReport(
        results=results,
        summary=summarise(results),
        leaks=leaks,
        score_samples=_score_samples(results),
    )


def summarise(results: Sequence[CaseResult]) -> dict[str, float]:
    answerable = [r for r in results if r.case.kind == "answerable"]
    restricted = [r for r in results if r.case.kind == "restricted"]
    unanswerable = [r for r in results if r.case.kind == "unanswerable"]

    summary: dict[str, float] = {}
    for k in RECALL_KS:
        summary[f"recall@{k}"] = _share(
            r.rank is not None and r.rank <= k for r in answerable
        )
    summary["mrr"] = (
        sum(1.0 / r.rank for r in answerable if r.rank) / len(answerable)
        if answerable
        else 0.0
    )
    summary["refusal_rate"] = _share(not r.refs for r in unanswerable)
    summary["restricted_pass_rate"] = _share(
        not any(ref in r.case.forbid for ref in r.refs) for r in restricted
    )
    summary["acl_leaks"] = float(sum(len(r.leaked) for r in results))
    summary["avg_docs_returned"] = (
        statistics.fmean(len(r.refs) for r in results) if results else 0.0
    )
    return summary


def _share(flags: object) -> float:
    items = list(flags)  # type: ignore[call-overload]
    return sum(1 for flag in items if flag) / len(items) if items else 0.0


def _score_samples(results: Sequence[CaseResult]) -> dict[str, list[float]]:
    """Top scores per kind, to calibrate a relevance threshold."""
    samples: dict[str, list[float]] = {"answerable": [], "unanswerable": []}
    for r in results:
        if r.top_score is None:
            continue
        if r.case.kind == "answerable":
            samples["answerable"].append(r.top_score)
        elif r.case.kind == "unanswerable":
            samples["unanswerable"].append(r.top_score)
    return {kind: sorted(values) for kind, values in samples.items()}


# --- thresholds ---------------------------------------------------------------------


def check_thresholds(
    summary: Mapping[str, float], thresholds: Mapping[str, Mapping[str, float]]
) -> list[str]:
    """Violations of ``{"min": {...}, "max": {...}}``; empty means the gate passes."""
    problems: list[str] = []
    for metric, floor in thresholds.get("min", {}).items():
        value = summary.get(metric)
        if value is None:
            problems.append(f"{metric}: not measured")
        elif value + 1e-9 < floor:
            problems.append(f"{metric}: {value:.3f} is below the minimum {floor:.3f}")
    for metric, ceiling in thresholds.get("max", {}).items():
        value = summary.get(metric)
        if value is None:
            problems.append(f"{metric}: not measured")
        elif value - 1e-9 > ceiling:
            problems.append(f"{metric}: {value:.3f} is above the maximum {ceiling:.3f}")
    return problems


# --- presentation ---------------------------------------------------------------------


def format_report(report: EvalReport, *, verbose: bool = False) -> str:
    s = report.summary
    counts = {
        kind: sum(1 for r in report.results if r.case.kind == kind)
        for kind in ("answerable", "restricted", "unanswerable")
    }
    lines = [
        f"cases: {len(report.results)}  "
        f"(answerable {counts['answerable']}, restricted {counts['restricted']}, "
        f"unanswerable {counts['unanswerable']})",
        "",
        f"  recall@1              {s['recall@1']:.3f}",
        f"  recall@3              {s['recall@3']:.3f}",
        f"  recall@5              {s['recall@5']:.3f}",
        f"  mrr                   {s['mrr']:.3f}",
        f"  refusal_rate          {s['refusal_rate']:.3f}   (unanswerable: no hits)",
        f"  restricted_pass_rate  {s['restricted_pass_rate']:.3f}   (no forbidden doc)",
        f"  acl_leaks             {int(s['acl_leaks'])}       (MUST be 0)",
        f"  avg_docs_returned     {s['avg_docs_returned']:.2f}",
    ]
    bad = report.failures if verbose else [r for r in report.failures if r.leaked]
    if bad:
        lines += ["", "failures:" if verbose else "ACL LEAKS:"]
        for r in bad:
            lines.append(f"  [{r.case.id}] {r.case.persona}: {r.case.question!r}")
            lines.append(f"      {r.note}")
    return "\n".join(lines)
