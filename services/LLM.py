import re


def generate_answer(
    query: str,
    context: str,
    source: str
) -> str:
    """
    Temporary response generator.

    This will later be replaced by a real LLM.
    For now, it returns only the most relevant
    retrieved document in a clean format.
    """

    if not context:
        return (
            "I couldn't find relevant information "
            "in the available knowledge base."
        )

    return (
        f"{context}\n\n"
        f"Source: {source}"
    )