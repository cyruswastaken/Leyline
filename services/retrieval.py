import re
import time


# Common words that do not contribute much
# to document relevance.
STOP_WORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "am",
    "was",
    "were",
    "be",
    "to",
    "of",
    "for",
    "in",
    "on",
    "at",
    "and",
    "or",
    "but",
    "do",
    "does",
    "did",
    "how",
    "what",
    "why",
    "when",
    "where",
    "which",
    "can",
    "could",
    "would",
    "should",
    "i",
    "me",
    "my",
    "you",
    "your",
}


# Simple enterprise IT terminology expansion.
# This helps bridge common user language
# with terminology used in IT documentation.
TERM_EXPANSIONS = {
    "remote": {
        "vpn",
        "remote",
        "access",
        "connection",
    },

    "access": {
        "access",
        "login",
        "authentication",
        "vpn",
        "connection",
    },

    "connect": {
        "connect",
        "connection",
        "vpn",
        "network",
    },

    "connection": {
        "connection",
        "connect",
        "vpn",
        "network",
    },

    "login": {
        "login",
        "authentication",
        "credentials",
        "access",
    },

    "signin": {
        "login",
        "authentication",
        "credentials",
        "access",
    },

    "mfa": {
        "mfa",
        "multi",
        "factor",
        "authentication",
        "verification",
    },

    "password": {
        "password",
        "credentials",
        "authentication",
    },
}


def tokenize(text: str) -> set:
    """
    Convert text into normalized lowercase words.
    """

    words = re.findall(
        r"\b\w+\b",
        text.lower()
    )

    return {
        word
        for word in words
        if word not in STOP_WORDS
    }


def expand_query_words(
    query_words: set
) -> set:
    """
    Expand common IT terminology
    into related terms.
    """

    expanded_words = set(
        query_words
    )

    for word in query_words:

        related_words = TERM_EXPANSIONS.get(
            word,
            set()
        )

        expanded_words.update(
            related_words
        )

    return expanded_words


def calculate_score(
    query: str,
    document: dict
) -> int:
    """
    Calculate a relevance score using
    normalized keyword overlap and
    lightweight terminology expansion.
    """

    query_words = tokenize(
        query
    )

    expanded_query_words = (
        expand_query_words(
            query_words
        )
    )

    document_text = (
        document.get("title", "")
        + " "
        + document.get("content", "")
    )

    document_words = tokenize(
        document_text
    )

    # Direct query matches are weighted more heavily.
    direct_matches = (
        query_words.intersection(
            document_words
        )
    )

    # Expanded terminology matches
    # receive a smaller contribution.
    expanded_matches = (
        expanded_query_words.intersection(
            document_words
        )
    )

    expanded_only_matches = (
        expanded_matches - direct_matches
    )

    score = (
        len(direct_matches) * 3
        + len(expanded_only_matches)
    )

    # Give title matches additional weight.
    title_words = tokenize(
        document.get("title", "")
    )

    title_matches = (
        query_words.intersection(
            title_words
        )
    )

    score += (
        len(title_matches) * 2
    )

    return score


def retrieve_documents(
    query: str,
    documents: list,
    top_k: int = 3
) -> list:
    """
    Return the most relevant documents.

    Only documents passed into this function
    are searched.

    Authorization must therefore happen
    before retrieval.
    """

    start_time = time.perf_counter()

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
        key=lambda document:
            document["score"],
        reverse=True
    )

    results = scored_documents[:top_k]

    latency_ms = (
        time.perf_counter() - start_time
    ) * 1000

    return results