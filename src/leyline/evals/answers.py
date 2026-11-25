"""``python -m leyline.evals.answers`` - judge the *answers*, not just retrieval.

For each golden question this runs the real pipeline (permission-aware
retrieval, re-authorization, delimited prompt, the configured chat model,
citation verification) and measures:

* ``answer_rate``        answerable questions that got an answer, not a refusal
* ``citation_rate``      answers that cite at least one source
* ``citation_accuracy``  answers whose cited sources include an expected document
* ``number_support``     answers whose figures all appear in the sources shown to
                         the model (a cheap check against invented numbers)
* ``refusal_rate``       unanswerable questions that were refused
* ``restricted_pass``    restricted questions where no forbidden document was
                         used, cited or named in the answer
* ``acl_leaks``          sources shown to the model that the asker may not read

There is no model-as-judge: every metric is computed from the sources and the
corpus ACLs, so a run is explainable. It needs a chat model, so it is a manual
gate (results are recorded in docs/EVALUATION.md), unlike the retrieval gate
that runs in CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.config import Settings, get_settings
from leyline.db.database import Database
from leyline.evals.__main__ import DEFAULT_GOLDEN, document_refs
from leyline.evals.answer_cache import (
    AnswerCache,
    RecordingChatModel,
    ReplayChatModel,
)
from leyline.evals.golden import GoldenCase, GoldenError, load_acl_oracle, load_golden
from leyline.ports.llm import ChatModel, ModelUnavailableError
from leyline.ports.search import SearchIndex
from leyline.services.authorization import can_read
from leyline.services.chat_service import NO_ANSWER, finalize_answer, retrieve
from leyline.services.llm import PROMPT_VERSION, generate_answer
from leyline.wiring import build_chat_model, build_database, build_embedder

_REFUSAL = re.compile(
    r"(couldn'?t find|could not find|not (?:available|covered|mentioned|found)|"
    r"unavailable|no information|"
    r"(?:does|do) not (?:contain|include|provide|cover|mention)|"
    r"(?:doesn|don)'?t (?:contain|include|provide|cover|mention)|"
    r"not enough information|"
    r"cannot (?:answer|find)|can'?t (?:answer|find))",
    re.I,
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def is_refusal(answer: str) -> bool:
    return answer.strip() == NO_ANSWER or bool(_REFUSAL.search(answer))


_UNITS = {
    w: i
    for i, w in enumerate(
        ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine"),
        start=1,
    )
}
_TEENS = {
    w: 10 + i
    for i, w in enumerate(
        (
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        )
    )
}
_TENS = {
    w: 20 + 10 * i
    for i, w in enumerate(
        ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
    )
}
_WORD_NUMBER = re.compile(
    r"\b(?:(" + "|".join(_TENS) + r")(?:[- ](" + "|".join(_UNITS) + r"))?"
    r"|(" + "|".join(_TEENS) + r")"
    r"|(" + "|".join(_UNITS) + r"))\b",
    re.I,
)


def _spelled_numbers(text: str, *, smallest: int) -> set[str]:
    """Numbers written as words ("fifty", "twenty-five", "fourteen") as digit strings.

    The sample corpus spells figures out while models usually write digits, so a
    digit-only comparison calls correct answers unsupported. ``smallest`` keeps
    ordinary prose out ("in two steps" is not a figure when checking an answer).
    """
    found: set[str] = set()
    for tens, unit, teen, single in _WORD_NUMBER.findall(text.lower()):
        if tens:
            value = _TENS[tens] + (_UNITS[unit] if unit else 0)
        elif teen:
            value = _TEENS[teen]
        else:
            value = _UNITS[single]
        if value >= smallest:
            found.add(str(value))
    return found


def unsupported_numbers(answer: str, context: str, question: str = "") -> list[str]:
    """Figures in the answer that appear nowhere in the sources or in the user's
    own question (citation markers like ``[2]`` and list numbering are not figures).

    A figure counts as supported when the sources give it as digits or as words.
    A figure the user typed and the answer repeats ("a purchase of fifteen
    thousand") is not an invented number, so the question's figures count as known
    too. In the answer, words for ten and above also count as figures, so spelling
    out an invented number does not hide it."""
    body = re.sub(r"\[\d{1,2}\]", "", answer)
    body = re.sub(r"(?m)^\s*\d+[.)]\s+", "", body)
    known = set(_NUMBER.findall(context)) | _spelled_numbers(context, smallest=1)
    known |= set(_NUMBER.findall(question)) | _spelled_numbers(question, smallest=1)
    stated = set(_NUMBER.findall(body)) | _spelled_numbers(body, smallest=10)
    return sorted(n for n in stated if n not in known)


_STOPWORDS = frozenset(
    [
        "that",
        "this",
        "with",
        "from",
        "have",
        "your",
        "will",
        "would",
        "should",
        "could",
        "about",
        "which",
        "their",
        "there",
        "these",
        "those",
        "them",
        "they",
        "then",
        "than",
        "what",
        "when",
        "where",
        "into",
        "only",
        "also",
        "each",
        "must",
        "need",
        "needs",
        "used",
        "using",
        "make",
        "made",
        "does",
        "done",
        "been",
        "being",
        "were",
        "more",
        "most",
        "other",
        "such",
        "very",
        "just",
        "like",
        "over",
        "under",
        "after",
        "before",
    ]
)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_CITATION = re.compile(r"\[(\d{1,2})\]")
# A sentence that cites a source should share at least this much of its content
# words with it. Calibrated on recorded answers (docs/EVALUATION.md): correct
# citations sit well above it; a citation pasted onto an unrelated sentence does not.
GROUNDING_MINIMUM = 0.25


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in _STOPWORDS}


def grounding(answer: str, sources: Sequence[str]) -> list[tuple[int, float]]:
    """For every citation in the answer: its number and the share of the citing
    sentence's content words that also appear in the cited source (deterministic,
    no model). ``sources`` are the texts shown to the model, numbered from 1."""
    scores: list[tuple[int, float]] = []
    for sentence in _SENTENCES.split(answer):
        numbers = {int(n) for n in _CITATION.findall(sentence)}
        words = _content_words(_CITATION.sub("", sentence))
        if not numbers or not words:
            continue
        for number in sorted(numbers):
            if 1 <= number <= len(sources):
                shared = words & _content_words(sources[number - 1])
                scores.append((number, len(shared) / len(words)))
    return scores


def ungrounded_citations(
    answer: str, sources: Sequence[str], minimum: float = GROUNDING_MINIMUM
) -> list[int]:
    """Citation numbers whose sentence shares too little with the cited source."""
    return [n for n, share in grounding(answer, sources) if share < minimum]


@dataclass
class CaseResult:
    case: GoldenCase
    answer: str = ""
    ungrounded: list[int] = field(default_factory=list)  # weakly supported citations
    sources: list[str] = field(default_factory=list)  # corpus ids shown to the model
    cited: list[str] = field(default_factory=list)  # corpus ids the answer cites
    refused: bool = False
    leaked: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    model_error: str | None = None
    note: str = ""

    @property
    def passed(self) -> bool:
        kind = self.case.kind
        if self.leaked or self.model_error:
            return False
        if kind == "answerable":
            return (
                not self.refused
                and bool(set(self.cited) & set(self.case.expect))
                and not self.unsupported
            )
        if kind == "unanswerable":
            return self.refused
        return not (set(self.sources + self.cited) & set(self.case.forbid))


@dataclass
class AnswerReport:
    results: list[CaseResult]

    @property
    def summary(self) -> dict[str, float]:
        def rate(selected: Sequence[CaseResult], test: Any) -> float:
            return (
                sum(1 for r in selected if test(r)) / len(selected) if selected else 1.0
            )

        by = {
            kind: [r for r in self.results if r.case.kind == kind]
            for kind in ("answerable", "restricted", "unanswerable")
        }
        scored = [r for r in by["answerable"] if not r.model_error]
        return {
            "cases": float(len(self.results)),
            "model_errors": float(sum(1 for r in self.results if r.model_error)),
            "answer_rate": rate(scored, lambda r: not r.refused),
            "citation_rate": rate(scored, lambda r: bool(r.cited)),
            "citation_accuracy": rate(
                scored, lambda r: bool(set(r.cited) & set(r.case.expect))
            ),
            "number_support": rate(scored, lambda r: not r.unsupported),
            "grounded_rate": rate(
                [r for r in scored if r.cited], lambda r: not r.ungrounded
            ),
            "refusal_rate": rate(by["unanswerable"], lambda r: r.refused),
            "restricted_pass": rate(by["restricted"], lambda r: r.passed),
            "acl_leaks": float(sum(len(r.leaked) for r in self.results)),
        }

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]


def run_answer_eval(
    cases: Sequence[GoldenCase],
    *,
    search: SearchIndex,
    database: Database,
    model: ChatModel,
    settings: Settings,
    refs: Mapping[uuid.UUID, str],
    acl: Mapping[str, frozenset[str]],
) -> AnswerReport:
    results: list[CaseResult] = []
    for case in cases:
        result = CaseResult(case)
        results.append(result)
        principal = case.principal
        prepared = retrieve(
            case.question,
            principal,
            settings=settings,
            search=search,
            database=database,
            model=None,  # evaluate the question as asked, not a rewrite
            history=[],
        )
        result.sources = [refs.get(hit.document_id, "?") for hit in prepared.hits]
        result.leaked = [
            ref
            for ref in result.sources
            if ref in acl and not can_read(principal, acl[ref])
        ]
        if not prepared.hits:
            result.answer, result.refused = NO_ANSWER, True
            continue
        try:
            raw = generate_answer(
                case.question, prepared.context, prepared.source, model=model
            )
        except ModelUnavailableError as exc:
            result.model_error = exc.reason
            continue
        text, citations = finalize_answer(raw, prepared)
        result.answer = text
        result.cited = [
            refs.get(uuid.UUID(c["document_id"]), "?") for c in citations if c["cited"]
        ]
        result.refused = is_refusal(text)
        result.unsupported = unsupported_numbers(text, prepared.context, case.question)
        result.ungrounded = ungrounded_citations(
            text,
            [
                f"{hit.title}\n{hit.heading or ''}\n{hit.content}"
                for hit in prepared.hits
            ],
        )
        if case.kind == "restricted":
            named = [
                ref
                for ref in case.forbid
                if _names_document(text, ref, acl, refs, database)
            ]
            result.note = f"names forbidden documents: {named}" if named else ""
            result.sources += named
    return AnswerReport(results)


def _names_document(
    text: str,
    ref: str,
    acl: Mapping[str, frozenset[str]],
    refs: Mapping[uuid.UUID, str],
    database: Database,
) -> bool:
    """Does the answer mention a forbidden document by its title?"""
    from sqlalchemy import select

    from leyline.db.models import Document

    with database.session() as session:
        title = session.scalar(select(Document.title).where(Document.source_ref == ref))
    return title is not None and title.lower() in text.lower()


def format_report(report: AnswerReport, *, verbose: bool = False) -> str:
    s = report.summary
    lines = [
        f"cases: {int(s['cases'])}   model errors: {int(s['model_errors'])}",
        f"  answered (answerable)        {s['answer_rate']:.3f}",
        f"  cites a source               {s['citation_rate']:.3f}",
        f"  cites an expected document   {s['citation_accuracy']:.3f}",
        f"  figures supported by sources {s['number_support']:.3f}",
        f"  citations grounded in source {s['grounded_rate']:.3f}",
        f"  refused (unanswerable)       {s['refusal_rate']:.3f}",
        f"  restricted pass              {s['restricted_pass']:.3f}",
        f"  ACL leaks                    {int(s['acl_leaks'])}",
    ]
    if verbose:
        lines.append("\nfailing cases:")
        for r in report.failures:
            detail = r.model_error or r.note or ", ".join(r.unsupported) or "-"
            lines.append(
                f"  {r.case.id} [{r.case.kind}/{r.case.persona}] "
                f"cited={r.cited} refused={r.refused} ({detail})"
            )
            lines.append(f"      {r.answer[:160]!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m leyline.evals.answers")
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--kinds", default="answerable,restricted,unanswerable")
    parser.add_argument("--max-cases", type=int, default=0, help="0 = all")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--record-answers",
        type=Path,
        default=None,
        help="also save the live model's answers here, for offline replay in CI",
    )
    parser.add_argument(
        "--replay-answers",
        type=Path,
        default=None,
        help="use these recorded answers instead of a live model",
    )
    args = parser.parse_args(argv)
    if args.record_answers and args.replay_answers:
        parser.error("--record-answers and --replay-answers are exclusive")

    settings = get_settings()
    database = build_database(settings)
    model: ChatModel | None
    recorder: RecordingChatModel | None = None
    if args.replay_answers:
        model = ReplayChatModel(AnswerCache.load(args.replay_answers))
    else:
        model = build_chat_model(settings)
        if args.record_answers and model is not None:
            recorder = RecordingChatModel(
                model, AnswerCache(model.model, prompt_version=PROMPT_VERSION)
            )
            model = recorder
    if database is None or model is None:
        print(
            "needs DATABASE_URL and a chat model (LLM_BASE_URL, LLM_MODEL)",
            file=sys.stderr,
        )
        return 2
    try:
        acl = load_acl_oracle(args.corpus or settings.corpus_dir)
        cases = [
            c
            for c in load_golden(args.golden, acl)
            if c.kind in set(args.kinds.split(","))
        ]
    except GoldenError as exc:
        print(f"golden set error: {exc}", file=sys.stderr)
        return 2
    if args.max_cases:
        cases = cases[: args.max_cases]

    try:
        refs = document_refs(database)
        search = PostgresSearchIndex(
            database, build_embedder(settings), SearchOptions.from_settings(settings)
        )
        report = run_answer_eval(
            cases,
            search=search,
            database=database,
            model=model,
            settings=settings,
            refs=refs,
            acl=acl,
        )
    finally:
        database.dispose()

    if recorder is not None and args.record_answers:
        recorder.cache.save(args.record_answers)
        print(f"recorded {len(recorder.cache)} answers -> {args.record_answers}")
    print(f"model: {model.model}")
    print(format_report(report, verbose=args.verbose))
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "model": model.model,
                    "summary": report.summary,
                    "failures": [
                        {
                            "id": r.case.id,
                            "answer": r.answer,
                            "cited": r.cited,
                            "note": r.note,
                        }
                        for r in report.failures
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 1 if report.summary["acl_leaks"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
