"""The answer-quality metrics, with a scripted model and real Postgres."""

import pytest

from leyline.adapters.search_postgres import PostgresSearchIndex
from leyline.evals.answers import (
    CaseResult,
    format_report,
    is_refusal,
    run_answer_eval,
    unsupported_numbers,
)
from leyline.evals.golden import GoldenCase
from tests.support import FakeChatModel, make_settings

# --- pure helpers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I couldn't find relevant information in the available knowledge base.",
        "That information is unavailable in the knowledge base.",
        "The documents do not contain details about that.",
    ],
)
def test_refusals_are_recognised(text):
    assert is_refusal(text)


def test_an_actual_answer_is_not_a_refusal():
    assert not is_refusal("Open Settings, choose VPN and press connect [1].")


def test_invented_figures_are_found_but_citations_and_numbering_are_not_figures():
    context = "You get 25 days of leave, carrying over up to 5."
    answer = "1. You get 25 days [1].\n2. Carry over 5 days or 12 days [1]."
    assert unsupported_numbers(answer, context) == ["12"]
    assert unsupported_numbers("You get 25 days [1].", context) == []


# --- end to end over a tiny corpus ------------------------------------------------------


def case(id, persona, question, kind, **kw):
    return GoldenCase(id=id, persona=persona, question=question, kind=kind, **kw)


@pytest.fixture
def world(db, add_doc):
    leave = add_doc(
        "Leave policy", "# Leave\n\nStaff receive 25 days of annual leave.", ["*"]
    )
    rota = add_doc(
        "Hunting rota",
        "# Rota\n\nThe quarterly hunting rota lists 7 analysts.",
        ["security"],
    )
    refs = {leave: "LEAVE", rota: "ROTA"}
    acl = {"LEAVE": frozenset({"*"}), "ROTA": frozenset({"security"})}
    return refs, acl


def evaluate(db, world, cases, model):
    refs, acl = world
    return run_answer_eval(
        cases,
        search=PostgresSearchIndex(db),
        database=db,
        model=model,
        settings=make_settings(),
        refs=refs,
        acl=acl,
    )


def test_a_grounded_cited_answer_passes(db, world):
    report = evaluate(
        db,
        world,
        [case("a1", "employee", "annual leave days", "answerable", expect=("LEAVE",))],
        FakeChatModel(pieces=[], answer="Staff receive 25 days [1]."),
    )
    [result] = report.results
    assert result.passed and result.cited == ["LEAVE"]
    assert report.summary["citation_accuracy"] == 1.0
    assert report.summary["number_support"] == 1.0


def test_an_invented_figure_fails_the_case(db, world):
    report = evaluate(
        db,
        world,
        [case("a1", "employee", "annual leave days", "answerable", expect=("LEAVE",))],
        FakeChatModel(pieces=[], answer="Staff receive 40 days [1]."),
    )
    assert not report.results[0].passed
    assert report.results[0].unsupported == ["40"]
    assert report.summary["number_support"] == 0.0


def test_an_answer_citing_the_wrong_document_is_not_accurate(db, world):
    report = evaluate(
        db,
        world,
        [
            case(
                "a1",
                "security",
                "hunting rota analysts",
                "answerable",
                expect=("LEAVE",),
            )
        ],
        FakeChatModel(pieces=[], answer="There are 7 analysts [1]."),
    )
    assert report.summary["citation_accuracy"] == 0.0


def test_an_uncited_answer_is_counted_as_such(db, world):
    report = evaluate(
        db,
        world,
        [case("a1", "employee", "annual leave days", "answerable", expect=("LEAVE",))],
        FakeChatModel(pieces=[], answer="Staff receive 25 days."),
    )
    assert report.summary["citation_rate"] == 0.0


def test_unanswerable_questions_pass_when_refused_without_the_model(db, world):
    model = FakeChatModel(pieces=[], answer="should not be used")
    report = evaluate(
        db,
        world,
        [case("u1", "employee", "airspeed of a swallow", "unanswerable")],
        model,
    )
    assert report.results[0].passed and model.complete_calls == []
    assert report.summary["refusal_rate"] == 1.0


def test_restricted_questions_never_show_the_forbidden_document_to_the_model(db, world):
    model = FakeChatModel(pieces=[], answer="should not be used")
    report = evaluate(
        db,
        world,
        [
            case(
                "r1",
                "employee",
                "quarterly hunting rota analysts",
                "restricted",
                forbid=("ROTA",),
            )
        ],
        model,
    )
    assert report.results[0].passed
    assert report.summary["restricted_pass"] == 1.0
    assert report.summary["acl_leaks"] == 0.0
    assert model.complete_calls == []


def test_a_source_the_asker_may_not_read_is_counted_as_a_leak(db, world):
    refs, _ = world
    broken_acl = {"LEAVE": frozenset({"hr"}), "ROTA": frozenset({"security"})}
    report = run_answer_eval(
        [case("a1", "employee", "annual leave days", "answerable", expect=("LEAVE",))],
        search=PostgresSearchIndex(db),
        database=db,
        model=FakeChatModel(pieces=[], answer="Staff receive 25 days [1]."),
        settings=make_settings(),
        refs=refs,
        acl=broken_acl,  # the oracle says the employee may NOT read LEAVE
    )
    assert report.summary["acl_leaks"] == 1.0
    assert not report.results[0].passed


def test_model_outages_are_reported_not_scored_as_bad_answers(db, world):
    report = evaluate(
        db,
        world,
        [case("a1", "employee", "annual leave days", "answerable", expect=("LEAVE",))],
        FakeChatModel(pieces=[], fail_complete=True),
    )
    assert report.summary["model_errors"] == 1.0
    assert report.summary["answer_rate"] == 1.0  # nothing scored
    assert not report.results[0].passed


def test_the_report_lists_failing_cases(db, world):
    report = evaluate(
        db,
        world,
        [case("a1", "employee", "annual leave days", "answerable", expect=("LEAVE",))],
        FakeChatModel(pieces=[], answer="Staff receive 40 days [1]."),
    )
    text = format_report(report, verbose=True)
    assert "a1" in text and "40" in text and "cites an expected document" in text


def test_case_result_defaults_describe_an_unanswered_case():
    result = CaseResult(case("x", "employee", "q", "unanswerable"))
    assert not result.passed  # not refused yet
