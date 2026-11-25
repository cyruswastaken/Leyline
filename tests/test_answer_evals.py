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


def test_figures_written_as_words_in_the_sources_support_digits_in_the_answer():
    context = "Each mailbox holds fifty gigabytes. Leave is twenty-five days; notice is fourteen days."
    assert unsupported_numbers("It holds 50 gigabytes [1].", context) == []
    assert (
        unsupported_numbers("25 days of leave and 14 days notice [1].", context) == []
    )
    # a wrong digit is still caught, whether or not the sources spell numbers out
    assert unsupported_numbers("It holds 500 gigabytes [1].", context) == ["500"]
    assert unsupported_numbers("Leave is 24 days [1].", context) == ["24"]


def test_a_figure_the_user_typed_and_the_answer_repeats_is_not_invented():
    context = "Purchases up to twenty thousand need the finance director."
    question = "Who has to approve a purchase of fifteen thousand?"
    answer = "A purchase of fifteen thousand needs the finance director [1]."
    assert unsupported_numbers(answer, context) == ["15"]  # without the question
    assert unsupported_numbers(answer, context, question) == []
    assert (
        unsupported_numbers("Is 12 fine? Yes, 12 is fine [1].", context, "Is 12 fine?")
        == []
    )
    # a figure that is neither in the sources nor in the question is still caught
    assert unsupported_numbers("It costs 99 [1].", context, question) == ["99"]


def test_a_citation_on_a_supported_sentence_is_grounded():
    from leyline.evals.answers import grounding, ungrounded_citations

    source = "Reset your password in the self-service portal at the helpdesk."
    answer = "Reset your password in the self-service portal [1]."
    [(number, share)] = grounding(answer, [source])
    assert number == 1 and share > 0.8
    assert ungrounded_citations(answer, [source]) == []


def test_a_citation_pasted_onto_an_unrelated_sentence_is_flagged():
    from leyline.evals.answers import ungrounded_citations

    source = "Reset your password in the self-service portal at the helpdesk."
    answer = "Parking permits are renewed every January for company vehicles [1]."
    assert ungrounded_citations(answer, [source]) == [1]


def test_grounding_is_checked_per_sentence_and_per_cited_source():
    from leyline.evals.answers import ungrounded_citations

    sources = [
        "Reset your password in the self-service portal.",
        "Annual leave is booked through the people system.",
    ]
    answer = (
        "Reset your password in the self-service portal [1].\n"
        "Annual leave is booked through the people system [2].\n"
        "Reset your password in the self-service portal [2]."  # wrong source
    )
    assert ungrounded_citations(answer, sources) == [2]


def test_text_without_citations_or_with_unknown_numbers_has_nothing_to_check():
    from leyline.evals.answers import grounding

    assert grounding("No citations here at all.", ["source text"]) == []
    assert grounding("Something said [7].", ["only one source"]) == []


def test_spelling_out_an_invented_figure_does_not_hide_it():
    context = "You get 25 days of leave."
    assert unsupported_numbers("You get forty days [1].", context) == ["40"]
    assert unsupported_numbers("You get twenty-five days [1].", context) == []
    # ordinary small words are prose, not figures
    assert (
        unsupported_numbers("Follow these two steps, then one more [1].", context) == []
    )


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
