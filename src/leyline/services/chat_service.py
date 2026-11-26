from __future__ import annotations

import time
import uuid
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from typing import Any

from leyline import telemetry
from leyline.auth.principal import Principal
from leyline.config import Settings
from leyline.db.database import Database
from leyline.ports.llm import ChatModel, ModelUnavailableError, Usage
from leyline.ports.rerank import Reranker
from leyline.ports.search import SearchHit, SearchIndex
from leyline.ratelimit import RedisRateLimiter
from leyline.services.authorization import authorize_hits
from leyline.services.conversations import ConversationService
from leyline.services.guardrails import (
    PROMPT_LEAK_REPLY,
    StreamRedactor,
    assess_message,
    build_context,
    estimate_tokens,
    leaks_prompt,
    redact,
    verify_citations,
)
from leyline.services.llm import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    generate_answer,
    rewrite_query,
    stream_answer,
)
from leyline.services.logger import log_security_event
from leyline.services.reranking import rerank_hits
from leyline.services.rewrite_policy import needs_rewrite

NO_ANSWER = "I couldn't find relevant information in the available knowledge base."
MODEL_UNAVAILABLE = (
    "The assistant is temporarily unavailable. Please try again in a few minutes."
)


@dataclass(frozen=True)
class Prepared:
    """Everything needed to generate an answer, after authorization."""

    query: str  # what retrieval searched for (the question, possibly rewritten)
    hits: tuple[SearchHit, ...]  # authorized sources that fit the budget, in order
    context: str
    source: str
    citations: tuple[dict[str, Any], ...]  # one entry per source, numbered from 1
    # Tokens the provider reported for the follow-up rewrite (empty if none was made).
    rewrite_usage: Usage = field(default_factory=Usage)


def _rejection(error: str, reason: str, status: int) -> dict[str, Any]:
    return {"allowed": False, "error": error, "reason": reason, "status_code": status}


def admit(
    message: str,
    principal: Principal,
    *,
    rate_limiter: RedisRateLimiter,
    settings: Settings,
) -> dict[str, Any] | None:
    """Per-user rate limit, then injection scoring. None means admitted."""
    user_id = principal.user_id

    if not rate_limiter.is_allowed(
        f"user:{user_id}",
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    ):
        log_security_event(
            event_type="RATE_LIMIT_EXCEEDED",
            user_id=user_id,
            status_code=429,
            reason="rate_limit_exceeded",
        )
        telemetry.chat_outcome("rate_limited")
        return _rejection(
            "Too many requests. Please try again later.", "rate_limit_exceeded", 429
        )

    assessment = assess_message(message)
    if assessment.signals:
        blocked = assessment.blocked(settings.injection_block_score)
        log_security_event(
            event_type="SECURITY_RULE_TRIGGERED" if blocked else "INJECTION_SIGNAL",
            user_id=user_id,
            status_code=403 if blocked else 200,
            reason=",".join(assessment.signals),
            details={"score": assessment.score, "blocked": blocked},
        )
        if blocked:
            telemetry.chat_outcome("blocked")
            return _rejection(
                "Your request was blocked by a security rule.",
                "security_rule_triggered",
                403,
            )
    return None


def _standalone_query(
    message: str,
    history: list[str],
    model: ChatModel | None,
    user_id: str,
    *,
    rewrite_mode: str = "auto",
    usage: Usage | None = None,
) -> tuple[str, bool]:
    """Rewrite a follow-up so retrieval can understand it. Any failure falls
    back to the user's own words; the rewrite never needs to succeed.

    Returns the query and whether a rewrite was skipped because the question
    already stands alone (``rewrite_mode="auto"``), which saves a model call."""
    if not history or model is None:
        return message, False
    if rewrite_mode == "auto" and not needs_rewrite(message):
        return message, True
    try:
        with telemetry.stage("rewrite"):
            rewritten = rewrite_query(history, message, model=model, usage=usage)
    except ModelUnavailableError as exc:
        log_security_event(
            event_type="MODEL_ERROR",
            user_id=user_id,
            status_code=200,
            reason=f"rewrite_{exc.reason}",
        )
        return message, False
    return rewritten[:500] or message, False


def retrieve(
    message: str,
    principal: Principal,
    *,
    settings: Settings,
    search: SearchIndex,
    database: Database,
    model: ChatModel | None,
    history: list[str],
    reranker: Reranker | None = None,
) -> Prepared:
    """Permission-aware retrieval for one turn.

    Chunks are selected only from documents the principal may read, then
    verified again against the database, and only then placed in a prompt.
    History is the user's own earlier questions (never assistant text).
    A reranker, when configured, runs last: after that re-check, so it only ever
    sees text the asker may read, and it can reorder or drop but never add.
    """
    user_id = principal.user_id
    rewrite_usage = Usage()
    query, rewrite_skipped = _standalone_query(
        message,
        history,
        model,
        user_id,
        rewrite_mode=settings.rewrite_mode,
        usage=rewrite_usage,
    )

    started = time.perf_counter()
    with telemetry.stage("retrieve"):
        # A reranker picks the best few from a wider list, so ask for more.
        wanted = (
            max(settings.search_top_k, settings.rerank_candidates)
            if reranker is not None
            else settings.search_top_k
        )
        candidates = search.search(principal, query, limit=wanted)
        hits = authorize_hits(database, principal, candidates)

    rerank_details: dict[str, Any] = {}
    if reranker is not None and hits:
        before = len(hits)
        with telemetry.stage("rerank"):
            hits, rerank_failure = rerank_hits(
                reranker,
                query,
                hits,
                keep=settings.search_top_k,
                min_score=settings.rerank_min_score,
            )
        rerank_details = {
            "reranked": rerank_failure is None,
            "rerank_candidates": before,
            "rerank_kept": len(hits),
        }
        if rerank_failure:
            rerank_details["rerank_failure"] = rerank_failure

    log_security_event(
        event_type="RETRIEVAL",
        user_id=user_id,
        status_code=200,
        reason="retrieval_completed",
        details={
            "selected_source": hits[0].title if hits else None,
            "top_score": round(hits[0].score, 4) if hits else 0,
            "top_similarity": (
                round(hits[0].similarity, 4)
                if hits and hits[0].similarity is not None
                else None
            ),
            "returned_count": len(hits),
            "candidate_count": len(candidates),
            "rewritten": query != message,
            "rewrite_skipped": rewrite_skipped,
            **rerank_details,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )

    if not hits:
        log_security_event(
            event_type="NO_RESULTS_FOUND",
            user_id=user_id,
            status_code=200,
            reason="no_relevant_documents",
        )
        return Prepared(query, (), "", "", (), rewrite_usage)

    built = build_context(hits, token_budget=settings.llm_context_token_budget)
    citations = tuple(
        {
            "n": number,
            "document_id": str(hit.document_id),
            "chunk_id": str(hit.chunk_id),
            "title": hit.title,
            "heading": hit.heading,
        }
        for number, hit in enumerate(built.used, start=1)
    )
    return Prepared(
        query=query,
        hits=built.used,
        context=built.text,
        source=", ".join(dict.fromkeys(hit.title for hit in built.used)),
        citations=citations,
        rewrite_usage=rewrite_usage,
    )


def finalize_answer(
    raw: str, prepared: Prepared, user_id: str = "unknown"
) -> tuple[str, list[dict[str, Any]]]:
    """Redact credentials, drop citations to sources that were not retrieved,
    and mark which of the retrieved sources the answer actually cites. An answer
    that reproduces the system prompt is replaced by a refusal."""
    if leaks_prompt(raw, SYSTEM_PROMPT):
        log_security_event(
            event_type="PROMPT_LEAK_BLOCKED",
            user_id=user_id,
            status_code=200,
            reason="system_prompt_reproduced",
        )
        return PROMPT_LEAK_REPLY, [{**c, "cited": False} for c in prepared.citations]
    text, cited = verify_citations(redact(raw), len(prepared.citations))
    citations = [{**c, "cited": c["n"] in cited} for c in prepared.citations]
    return text.strip(), citations


def record_usage(
    settings: Settings,
    model: ChatModel | None,
    prepared: Prepared,
    message: str,
    answer: str,
    answer_usage: Usage,
    *,
    started: float,
    first_token_ms: float | None = None,
) -> dict[str, Any]:
    """Count the tokens and cost of an answered turn, emit them as metrics, and
    return the fields stored on the turn's ``NORMAL_REQUEST`` event.

    Counts are the provider's own (the follow-up rewrite plus the answer). A model
    that reports none (some local servers) falls back to a character-based
    estimate, and the event says which it was. Only numbers and names are recorded,
    never prompt or answer text."""
    total = Usage()
    total.merge(prepared.rewrite_usage)
    total.merge(answer_usage)
    source = "provider" if answer_usage.reported else "estimate"
    if not answer_usage.reported:
        total.prompt_tokens += estimate_tokens(
            SYSTEM_PROMPT + prepared.context + message
        )
        total.completion_tokens += estimate_tokens(answer)
    cost = (
        total.prompt_tokens * settings.llm_price_input_per_million
        + total.completion_tokens * settings.llm_price_output_per_million
    ) / 1_000_000
    telemetry.tokens("input", total.prompt_tokens, source)
    telemetry.tokens("output", total.completion_tokens, source)
    telemetry.cost(cost)
    details: dict[str, Any] = {
        "model": model.model if model is not None else "",
        "prompt_version": PROMPT_VERSION,
        "prompt_tokens": total.prompt_tokens,
        "completion_tokens": total.completion_tokens,
        "token_source": source,
        "cost_usd": round(cost, 6),
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    if first_token_ms is not None:
        # Streamed turns only. The first-token alert reads this (docs/SLO.md).
        details["first_token_ms"] = round(first_token_ms, 1)
    return details


def _model_error(user_id: str, reason: str) -> None:
    log_security_event(
        event_type="MODEL_ERROR", user_id=user_id, status_code=503, reason=reason
    )


def process_message(
    message: str,
    principal: Principal,
    *,
    rate_limiter: RedisRateLimiter,
    settings: Settings,
    search: SearchIndex,
    database: Database,
    model: ChatModel | None,
    reranker: Reranker | None = None,
) -> dict[str, Any]:
    """One stateless question: admit -> retrieve -> authorize -> answer.

    Authorization is part of the search itself and is verified again against
    the database before anything is placed in the prompt, so unauthorized
    content never reaches the model.
    """
    user_id = principal.user_id
    rejected = admit(message, principal, rate_limiter=rate_limiter, settings=settings)
    if rejected:
        return rejected

    prepared = retrieve(
        message,
        principal,
        settings=settings,
        search=search,
        database=database,
        model=model,
        history=[],
        reranker=reranker,
    )
    if not prepared.hits:
        telemetry.chat_outcome("refused")
        return {"allowed": True, "response": NO_ANSWER, "status_code": 200}

    started = time.perf_counter()
    answer_usage = Usage()
    try:
        if model is None:
            raise ModelUnavailableError("not_configured")
        with telemetry.stage("generate"):
            raw = generate_answer(
                query=message,
                context=prepared.context,
                source=prepared.source,
                model=model,
                usage=answer_usage,
            )
        details = record_usage(
            settings, model, prepared, message, raw, answer_usage, started=started
        )
    except ModelUnavailableError as exc:
        # Degraded mode: retrieval worked, the model did not. Say so plainly
        # instead of a generic 500, and never fall back to unvetted text.
        _model_error(user_id, exc.reason)
        telemetry.chat_outcome("model_error")
        return _rejection(MODEL_UNAVAILABLE, "model_unavailable", 503)

    text, citations = finalize_answer(raw, prepared, user_id)
    telemetry.chat_outcome("answered")
    log_security_event(
        event_type="NORMAL_REQUEST",
        user_id=user_id,
        status_code=200,
        reason="request_allowed",
        details=details,
    )
    return {
        "allowed": True,
        "response": text,
        "citations": citations,
        "status_code": 200,
    }


# --- conversations with streaming ----------------------------------------------------


class StreamingTurn:
    """An admitted, authorized turn ready to stream. Created by ``begin_turn``."""

    def __init__(
        self,
        *,
        principal: Principal,
        conversation_id: uuid.UUID,
        prepared: Prepared,
        message: str,
        model: ChatModel | None,
        conversations: ConversationService,
        started: float,
        settings: Settings,
    ) -> None:
        self._settings = settings
        self._started = started
        self._principal = principal
        self._conversation_id = conversation_id
        self._prepared = prepared
        self._message = message
        self._model = model
        self._conversations = conversations

    def _save(
        self, content: str, status: str, citations: list[dict[str, Any]]
    ) -> uuid.UUID | None:
        return self._conversations.add_message(
            self._principal.user_id,
            self._conversation_id,
            role="assistant",
            content=content,
            status=status,
            citations=citations,
        )

    def events(self) -> Generator[dict[str, Any], None, None]:
        """meta, then delta*, then done | error. Closing the iterator early
        (the client went away) cancels the model call and keeps what was said."""
        user_id = self._principal.user_id
        prepared = self._prepared
        yield {"type": "meta", "conversation_id": str(self._conversation_id)}

        if not prepared.hits:
            message_id = self._save(NO_ANSWER, "complete", [])
            telemetry.chat_outcome("refused")
            telemetry.turn_finished(self._started, "refused")
            yield {"type": "delta", "text": NO_ANSWER}
            yield {
                "type": "done",
                "message_id": str(message_id),
                "content": NO_ANSWER,
                "citations": [],
            }
            return

        redactor = StreamRedactor()
        raw: list[str] = []
        shown: list[str] = []
        answer_usage = Usage()
        stream: Iterator[str] | None = None
        generating = telemetry.start_span("chat.generate")
        generate_started = time.perf_counter()
        first = True
        first_token_ms: float | None = None
        try:
            if self._model is None:
                raise ModelUnavailableError("not_configured")
            stream = stream_answer(
                self._message,
                prepared.context,
                prepared.source,
                model=self._model,
                usage=answer_usage,
            )
            for piece in stream:
                raw.append(piece)
                safe = redactor.feed(piece)
                if safe:
                    if first:
                        telemetry.first_token(self._started)
                        first_token_ms = (time.perf_counter() - self._started) * 1000
                        first = False
                    shown.append(safe)
                    yield {"type": "delta", "text": safe}
            tail = redactor.flush()
            if tail:
                if first:
                    telemetry.first_token(self._started)
                    first_token_ms = (time.perf_counter() - self._started) * 1000
                    first = False
                shown.append(tail)
                yield {"type": "delta", "text": tail}
        except ModelUnavailableError as exc:
            _model_error(user_id, exc.reason)
            telemetry.chat_outcome("model_error")
            telemetry.turn_finished(self._started, "model_error")
            partial = "".join(shown).strip()
            self._save(partial or MODEL_UNAVAILABLE, "error", [])
            yield {
                "type": "error",
                "reason": "model_unavailable",
                "error": MODEL_UNAVAILABLE,
            }
            return
        except GeneratorExit:
            # Cancelled by the client: keep the partial answer, mark it so.
            tail = redactor.flush()
            partial = ("".join(shown) + tail).strip()
            if partial:
                self._save(partial, "cancelled", [])
            telemetry.chat_outcome("cancelled")
            telemetry.turn_finished(self._started, "cancelled")
            log_security_event(
                event_type="CHAT_CANCELLED",
                user_id=user_id,
                status_code=200,
                reason="client_cancelled",
            )
            raise
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
            telemetry.stage_duration("generate", generate_started)
            generating.end()

        text, citations = finalize_answer("".join(raw), prepared, user_id)
        message_id = self._save(text, "complete", citations)
        details = record_usage(
            self._settings,
            self._model,
            prepared,
            self._message,
            "".join(raw),
            answer_usage,
            started=self._started,
            first_token_ms=first_token_ms,
        )
        telemetry.chat_outcome("answered")
        telemetry.turn_finished(self._started, "answered")
        log_security_event(
            event_type="NORMAL_REQUEST",
            user_id=user_id,
            status_code=200,
            reason="request_allowed",
            details=details,
        )
        yield {
            "type": "done",
            "message_id": str(message_id),
            "content": text,
            "citations": citations,
        }


def begin_turn(
    message: str,
    principal: Principal,
    conversation_id: uuid.UUID | None,
    *,
    rate_limiter: RedisRateLimiter,
    settings: Settings,
    search: SearchIndex,
    database: Database,
    model: ChatModel | None,
    conversations: ConversationService,
    reranker: Reranker | None = None,
) -> StreamingTurn | dict[str, Any]:
    """Admit, authorize and persist the question; returns a stream to run, or a
    rejection dict (rate limit, blocked, unknown conversation) with its status."""
    started = time.perf_counter()
    rejected = admit(message, principal, rate_limiter=rate_limiter, settings=settings)
    if rejected:
        return rejected

    user_id = principal.user_id
    history: list[str] = []
    if conversation_id is None:
        conversation_id = conversations.start(user_id, message)
    else:
        if not conversations.owns(user_id, conversation_id):
            # Same answer whether it is missing or someone else's.
            return _rejection("Conversation not found", "conversation_not_found", 404)
        history = conversations.recent_user_messages(
            user_id, conversation_id, limit=settings.chat_history_messages
        )

    prepared = retrieve(
        message,
        principal,
        settings=settings,
        search=search,
        database=database,
        model=model,
        history=history,
        reranker=reranker,
    )
    conversations.add_message(user_id, conversation_id, role="user", content=message)
    return StreamingTurn(
        principal=principal,
        conversation_id=conversation_id,
        prepared=prepared,
        message=message,
        model=model,
        conversations=conversations,
        started=started,
        settings=settings,
    )
