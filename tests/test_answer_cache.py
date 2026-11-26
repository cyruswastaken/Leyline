"""Recording and replaying a chat model's answers."""

import pytest

from leyline.evals.answer_cache import (
    AnswerCache,
    MissingAnswerError,
    RecordingChatModel,
    ReplayChatModel,
    prompt_key,
)
from leyline.ports.llm import ModelUnavailableError
from tests.support import FakeChatModel


def test_the_key_depends_on_both_prompts_and_keeps_them_apart():
    assert prompt_key("a", "b") == prompt_key("a", "b")
    assert prompt_key("a", "b") != prompt_key("a", "c")
    assert prompt_key("ab", "") != prompt_key("a", "b")  # no concatenation clash


def test_a_recording_run_can_be_replayed_exactly(tmp_path):
    inner = FakeChatModel(answer="Reset it in the portal [1].")
    recorder = RecordingChatModel(inner, AnswerCache("fake-chat", prompt_version="v1"))
    assert recorder.complete("SYS", "USER") == "Reset it in the portal [1]."
    assert recorder.model == "fake-chat"

    path = tmp_path / "answers.json"
    recorder.cache.save(path)
    replay = ReplayChatModel(AnswerCache.load(path))

    assert replay.model == "fake-chat"
    assert replay.complete("SYS", "USER") == "Reset it in the portal [1]."
    assert AnswerCache.load(path).prompt_version == "v1"


def test_a_prompt_that_was_never_recorded_fails_with_instructions(tmp_path):
    replay = ReplayChatModel(AnswerCache("m", {prompt_key("S", "U"): "ok"}))
    with pytest.raises(MissingAnswerError, match="--record-answers"):
        replay.complete("S", "different user prompt")
    with pytest.raises(MissingAnswerError):
        list(replay.stream("S", "different user prompt"))


def test_streaming_a_recording_returns_the_same_text_in_pieces():
    text = "Open Settings, then press connect [1]."
    replay = ReplayChatModel(AnswerCache("m", {prompt_key("S", "U"): text}))
    pieces = list(replay.stream("S", "U"))
    assert len(pieces) > 3 and "".join(pieces) == text


def test_recording_a_stream_keeps_the_whole_answer():
    inner = FakeChatModel(pieces=["Use the ", "portal ", "[1]."])
    recorder = RecordingChatModel(inner, AnswerCache("m"))
    assert "".join(recorder.stream("S", "U")) == "Use the portal [1]."
    assert recorder.cache.answers[prompt_key("S", "U")] == "Use the portal [1]."


def test_an_empty_recorded_answer_replays_as_a_model_failure():
    replay = ReplayChatModel(AnswerCache("m", {prompt_key("S", "U"): "  "}))
    with pytest.raises(ModelUnavailableError):
        replay.complete("S", "U")


def test_an_unknown_file_format_is_rejected(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text('{"format": 99, "model": "m", "answers": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported format"):
        AnswerCache.load(path)
