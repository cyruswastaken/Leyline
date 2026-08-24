from services.logger import log_security_event
from services.rate_limiter import rate_limiter
from services.security import check_message


def process_message(message: str, user_id: str) -> dict:
    """
    Process a user's chat message through the application's
    security pipeline.
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

    # Allowed request
    log_security_event(
        event_type="NORMAL_REQUEST",
        user_id=user_id,
        status_code=200,
        reason="request_allowed"
    )

    return {
        "allowed": True,
        "response": f"Temporary response: You said '{message}'",
        "status_code": 200
    }