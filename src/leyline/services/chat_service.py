from __future__ import annotations

import time
import uuid
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from typing import Any

from leyline import telemetry
from leyline.auth.principal import Principal
from leyline.config import Settings
from leyline.db.database import Database
from leyline.ports.llm import ChatModel, ModelUnavailableError
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
    SYSTEM_PROMPT,
    generate_answer,
    rewrite_query,
    stream_answer,
)
from leyline.services.logger import log_security_event

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
) -> str:
    """Rewrite a follow-up so retrieval can understand it. Any failure falls
    back to the user's own words; the rewrite never needs to succeed."""
    if not history or model is None:
        return message
    try:
        with telemetry.stage("rewrite"):
            rewritten = rewrite_query(history, message, model=model)
    except ModelUnavailableError as exc:
        log_security_event(
            event_type="MODEL_ERROR",
            user_id=user_id,
            status_code=200,
            reason=f"rewrite_{exc.reason}",
        )
        return message
    return rewritten[:500] or message


def retrieve(
    message: str,
    principal: Principal,
    *,
    settings: Settings,
    search: SearchIndex,
    database: Database,
    model: ChatModel | None,
    history: list[str],
) -> Prepared:
    """Permission-aware retrieval for one turn.

    Chunks are selected only from documents the principal may read, then
    verified again against the database, and only then placed in a prompt.
    History is the user's own earlier questions (never assistant text).
    """
    user_id = principal.user_id
    query = _standalone_query(message, history, model, user_id)

    started = time.perf_counter()
    with telemetry.stage("retrieve"):
        candidates = search.search(principal, query, limit=settings.search_top_k)
        hits = authorize_hits(database, principal, candidates)

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
        return Prepared(query, (), "", "", ())

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
    )
    if not prepared.hits:
        telemetry.chat_outcome("refused")
        return {"allowed": True, "response": NO_ANSWER, "status_code": 200}

    try:
        if model is None:
            raise ModelUnavailableError("not_configured")
        with telemetry.stage("generate"):
            raw = generate_answer(
                query=message,
                context=prepared.context,
                source=prepared.source,
                model=model,
            )
        telemetry.tokens("input", estimate_tokens(prepared.context + message))
        telemetry.tokens("output", estimate_tokens(raw))
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
    ) -> None:
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
        stream: Iterator[str] | None = None
        generating = telemetry.start_span("chat.generate")
        generate_started = time.perf_counter()
        first = True
        try:
            if self._model is None:
                raise ModelUnavailableError("not_configured")
            stream = stream_answer(
                self._message, prepared.context, prepared.source, model=self._model
            )
            for piece in stream:
                raw.append(piece)
                safe = redactor.feed(piece)
                if safe:
                    if first:
                        telemetry.first_token(self._started)
                        first = False
                    shown.append(safe)
                    yield {"type": "delta", "text": safe}
            tail = redactor.flush()
            if tail:
                if first:
                    telemetry.first_token(self._started)
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
        telemetry.tokens("input", estimate_tokens(prepared.context + self._message))
        telemetry.tokens("output", estimate_tokens("".join(raw)))
        telemetry.chat_outcome("answered")
        telemetry.turn_finished(self._started, "answered")
        log_security_event(
            event_type="NORMAL_REQUEST",
            user_id=user_id,
            status_code=200,
            reason="request_allowed",
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
    )
