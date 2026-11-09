from __future__ import annotations

import time
from typing import Any

from leyline.auth.principal import Principal
from leyline.config import Settings
from leyline.db.database import Database
from leyline.ports.search import SearchIndex
from leyline.ratelimit import RedisRateLimiter
from leyline.services.authorization import authorize_hits
from leyline.services.llm import generate_answer
from leyline.services.logger import log_security_event
from leyline.services.security import check_message

NO_ANSWER = "I couldn't find relevant information in the available knowledge base."


def process_message(
    message: str,
    principal: Principal,
    *,
    rate_limiter: RedisRateLimiter,
    settings: Settings,
    search: SearchIndex,
    database: Database,
) -> dict[str, Any]:
    """Run one question through the secure pipeline.

    rate limit -> input rules -> permission-aware search -> re-authorization
    -> grounded answer -> audit event

    Authorization is part of the search itself (only readable chunks are
    selected) and is then verified again against the database before anything
    is placed in the prompt, so unauthorized content never reaches the model.
    """
    user_id = principal.user_id

    # 1. Rate limit (per user) ------------------------------------------------
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
        return {
            "allowed": False,
            "error": "Too many requests. Please try again later.",
            "reason": "rate_limit_exceeded",
            "status_code": 429,
        }

    # 2. Input rules -----------------------------------------------------------
    blocked_pattern = check_message(message)
    if blocked_pattern:
        log_security_event(
            event_type="SECURITY_RULE_TRIGGERED",
            user_id=user_id,
            status_code=403,
            reason=blocked_pattern,
        )
        return {
            "allowed": False,
            "error": "Your request was blocked by a security rule.",
            "reason": "security_rule_triggered",
            "status_code": 403,
        }

    # 3. Retrieval: only chunks this principal may read ---------------------------
    started = time.perf_counter()
    candidates = search.search(principal, message, limit=settings.search_top_k)
    # 4. Re-check against the database: honours ACL changes and deletions
    #    made after the index was written.
    hits = authorize_hits(database, principal, candidates)

    log_security_event(
        event_type="RETRIEVAL",
        user_id=user_id,
        status_code=200,
        reason="retrieval_completed",
        details={
            "selected_source": hits[0].title if hits else None,
            "top_score": round(hits[0].score, 4) if hits else 0,
            "returned_count": len(hits),
            "candidate_count": len(candidates),
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
        return {"allowed": True, "response": NO_ANSWER, "status_code": 200}

    # 5. Grounded answer ---------------------------------------------------------
    context = "\n\n".join(
        f"Source: {hit.title}"
        + (f" ({hit.heading})" if hit.heading else "")
        + f"\n\n{hit.content}"
        for hit in hits
    )
    source = ", ".join(dict.fromkeys(hit.title for hit in hits))

    response = generate_answer(query=message, context=context, source=source)

    log_security_event(
        event_type="NORMAL_REQUEST",
        user_id=user_id,
        status_code=200,
        reason="request_allowed",
    )
    return {"allowed": True, "response": response, "status_code": 200}
