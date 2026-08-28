import re


def tokenize(text: str) -> set:
    """
    Convert text into lowercase words.
    """

    return set(
        re.findall(
            r"\b\w+\b",
            text.lower()
        )
    )


def calculate_score(
    query: str,
    document: dict
) -> int:
    """
    Calculate a simple keyword overlap score.
    """

    query_words = tokenize(query)

    document_text = (
        document.get("title", "")
        + " "
        + document.get("content", "")
    )

    document_words = tokenize(
        document_text
    )

    return len(
        query_words.intersection(
            document_words
        )
    )


def retrieve_documents(
    query: str,
    documents: list,
    top_k: int = 3
) -> list:
    """
    Return the most relevant documents.

    Only documents passed into this function are searched.
    Authorization must therefore happen before retrieval.
    """

    scored_documents = []

    for document in documents:

        score = calculate_score(
            query,
            document
        )

        if score > 0:

            scored_documents.append({
                **document,
                "score": score
            })

    scored_documents.sort(
        key=lambda document: document["score"],
        reverse=True
    )

    return scored_documents[:top_k]