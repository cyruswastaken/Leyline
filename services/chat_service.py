from services.logger import log_security_event
from services.rate_limiter import rate_limiter
from services.security import check_message
from services.document_loader import (
    load_documents,
    chunk_document
)
from services.authorization import filter_authorized_documents
from services.retrieval import retrieve_documents
from services.LLM import generate_answer


def process_message(
    message: str,
    user_id: str,
    user_role: str
) -> dict:
    """
    Process an employee request through the
    secure Leyline pipeline.

    Pipeline:

    Rate limiting
        ↓
    Security validation
        ↓
    Document loading
        ↓
    Authorization
        ↓
    Chunking
        ↓
    Retrieval
        ↓
    Azure OpenAI
        ↓
    Grounded response
    """

    # ==========================================
    # 1. RATE LIMITING
    # ==========================================

    if not rate_limiter.is_allowed(
        user_id
    ):

        log_security_event(
            event_type="RATE_LIMIT_EXCEEDED",
            user_id=user_id,
            status_code=429,
            reason="rate_limit_exceeded"
        )

        return {
            "allowed": False,
            "error": (
                "Too many requests. "
                "Please try again later."
            ),
            "reason": "rate_limit_exceeded",
            "status_code": 429
        }


    # ==========================================
    # 2. SECURITY VALIDATION
    # ==========================================

    blocked_pattern = check_message(
        message
    )

    if blocked_pattern:

        log_security_event(
            event_type="SECURITY_RULE_TRIGGERED",
            user_id=user_id,
            status_code=403,
            reason=blocked_pattern
        )

        return {
            "allowed": False,
            "error": (
                "Your request was blocked "
                "by a security rule."
            ),
            "reason": "security_rule_triggered",
            "status_code": 403
        }


    # ==========================================
    # 3. LOAD DOCUMENTS
    # ==========================================

    documents = load_documents()


    # ==========================================
    # 4. AUTHORIZATION
    # ==========================================
    #
    # IMPORTANT:
    # Authorization happens BEFORE
    # chunking and retrieval.
    #
    # Unauthorized documents therefore
    # never reach the retrieval layer
    # or the LLM.
    #

    authorized_documents = (
        filter_authorized_documents(
            documents,
            user_role
        )
    )


    # ==========================================
    # 5. CHUNK AUTHORIZED DOCUMENTS
    # ==========================================

    authorized_chunks = []

    for document in authorized_documents:

        chunks = chunk_document(
            document
        )

        authorized_chunks.extend(
            chunks
        )


    # ==========================================
    # 6. RETRIEVAL
    # ==========================================

    results = retrieve_documents(
        message,
        authorized_chunks
    )


    # ==========================================
    # 7. NO RESULTS
    # ==========================================

    if not results:

        log_security_event(
            event_type="NO_RESULTS_FOUND",
            user_id=user_id,
            status_code=200,
            reason="no_relevant_documents"
        )

        return {
            "allowed": True,
            "response": (
                "I couldn't find relevant "
                "information in the available "
                "knowledge base."
            ),
            "status_code": 200
        }


    # ==========================================
    # 8. BUILD LLM CONTEXT
    # ==========================================
    #
    # Instead of sending only results[0],
    # combine the retrieved authorized chunks.
    #

    context_parts = []
    source_parts = []

    for document in results:

        context_parts.append(
            f"""
Source: {document["title"]}

{document["content"]}
"""
        )

        source_parts.append(
            document["title"]
        )


    context = "\n\n".join(
        context_parts
    )

    source = ", ".join(
        dict.fromkeys(source_parts)
    )


    # ==========================================
    # 9. GENERATE GROUNDED ANSWER
    # ==========================================

    response = generate_answer(
        query=message,
        context=context,
        source=source
    )


    # ==========================================
    # 10. LOG SUCCESSFUL REQUEST
    # ==========================================

    log_security_event(
        event_type="NORMAL_REQUEST",
        user_id=user_id,
        status_code=200,
        reason="request_allowed"
    )


    # ==========================================
    # 11. RETURN RESPONSE
    # ==========================================

    return {
        "allowed": True,
        "response": response,
        "status_code": 200
    }