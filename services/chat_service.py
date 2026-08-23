from services.security import check_message


def process_message(message: str) -> str:
    """
    Process a user's chat message.

    The request first passes through the input security layer.
    Later this function will coordinate authorization,
    retrieval, and LLM generation.
    """

    blocked_pattern = check_message(message)

    if blocked_pattern:
        return (
            "Request not allowed. "
            "This message triggered a security rule."
        )

    return f"Temporary response: You said '{message}'"