"""When a follow-up is rewritten, and when the model call is skipped."""

import pytest

from leyline.services.rewrite_policy import needs_rewrite

STANDS_ALONE = [
    "How do I connect to the corporate VPN?",
    "How many days of annual leave do I get?",
    "What are the rules for expenses on a business trip?",
    "Is there a guest Wi-Fi in the office?",
    "How do I get a guest into the building?",
    "Can I install my own software on a company laptop?",
]

DEPENDS_ON_EARLIER = [
    "and on a Mac?",
    "what about Windows?",
    "how about for contractors?",
    "Why?",
    "Does that apply to contractors too?",
    "How long does it take?",
    "Can I do the same on my phone?",
    "What is the other option?",
    "and where is that?",
    "reset my password again",
    "But what if I forgot the answer",
    "Which of those is faster to set up?",
]


@pytest.mark.parametrize("message", STANDS_ALONE)
def test_a_question_that_stands_alone_is_searched_as_written(message):
    assert needs_rewrite(message) is False


@pytest.mark.parametrize("message", DEPENDS_ON_EARLIER)
def test_a_question_that_leans_on_earlier_ones_is_rewritten(message):
    assert needs_rewrite(message) is True


def test_a_very_short_question_is_rewritten_even_without_a_reference_word():
    assert needs_rewrite("VPN on Linux") is True
    assert needs_rewrite("") is True


def test_the_check_ignores_case_and_punctuation():
    assert needs_rewrite("WHAT ABOUT WINDOWS???") is True
    assert needs_rewrite("HOW DO I RESET MY PASSWORD?!") is False
