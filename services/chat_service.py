from services.logger import log_security_event
from services.rate_limiter import rate_limiter
from services.security import check_message
from services.document_loader import load_documents
from services.authorization import filter_authorized_documents
from services.retrieval import retrieve_documents
from services.LLM import generate_answer


def process_message(
    message: str,
    user_id: str,
    user_role: str
) -> dict:
    """
    Process a user's chat message through the security,
    authorization, and retrieval pipeline.
    """

    # Layer 1: Rate limiting
    if not rate_limiter.is_allowed(user_id):

        log_security_event(
            event_type="RATE_LIMIT_EXCEEDED",
            user_id=user_id,
            status_code=429,
            reason="rate_limit_exceeded"
        )

        return {
            "allowed": False,
            "error": "Too many requests. Please try again later.",
            "reason": "rate_limit_exceeded",
            "status_code": 429
        }

    # Layer 2: Input security check
    blocked_pattern = check_message(message)

    if blocked_pattern:

        log_security_event(
            event_type="SECURITY_RULE_TRIGGERED",
            user_id=user_id,
            status_code=403,
            reason="security_rule_triggered"
        )

        return {
            "allowed": False,
            "error": "Request not allowed.",
            "reason": "security_rule_triggered",
            "status_code": 403
        }

    # Layer 3: Load knowledge base
    documents = load_documents()

    # Layer 4: Authorization BEFORE retrieval
    authorized_documents = filter_authorized_documents(
        documents,
        user_role
    )

    # Layer 5: Retrieve relevant documents
    results = retrieve_documents(
        message,
        authorized_documents
    )

    # No relevant knowledge found
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
                "I couldn't find relevant information "
                "in the available knowledge base."
            ),
            "status_code": 200
        }

    # Layer 6: Select the most relevant document
    best_document = results[0]

    context = best_document["content"]
    source = best_document["title"]

    # Layer 7: Generate response
    response = generate_answer(
        query=message,
        context=context,
        source=source
    )

    # Log successful request
    log_security_event(
        event_type="NORMAL_REQUEST",
        user_id=user_id,
        status_code=200,
        reason="request_allowed"
    )

    return {
        "allowed": True,
        "response": response,
        "status_code": 200
    }