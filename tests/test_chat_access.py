"""Chat end to end: real corpus, real Postgres search, real authorization.

These replace the original audit regressions ("an employee must never receive
restricted content") now that access is decided by per-document group ACLs.
"""

from pathlib import Path

import pytest

from leyline.app import create_app
from leyline.seed import seed_corpus
from tests.support import CSRF_HEADER, PERSONAS, login_as, make_settings

REPO_CORPUS = Path(__file__).resolve().parent.parent / "corpus"
NETWORK_QUESTION = "Describe the internal network architecture segments"
VPN_QUESTION = "How do I set up the VPN?"


@pytest.fixture
def sample_corpus(ingestion, drain):
    """The five original sample documents (3 general, 2 restricted)."""
    for folder in ("general", "restricted"):
        seed_corpus(ingestion, REPO_CORPUS / folder)
    drain()


def chat(client, persona, message, *, query="", body=None, headers=None):
    token = login_as(client, PERSONAS[persona])
    payload = {"message": message, **(body or {})}
    return client.post(
        f"/chat{query}",
        json=payload,
        headers={CSRF_HEADER: token, **(headers or {})},
    )


def sources_sent_to_model(llm_calls):
    return {s.strip() for call in llm_calls for s in call["source"].split(",")}


# --- the original regressions, now on real data -----------------------------------


def test_an_employee_never_receives_restricted_content(
    db_client, sample_corpus, llm_calls
):
    response = chat(db_client, "employee", NETWORK_QUESTION)

    assert response.status_code == 200
    for call in llm_calls:
        assert "RES-NETWORK-001" not in call["context"]
        assert "Internal Network Architecture" not in call["context"]
        assert "Internal Network Architecture" not in call["source"]
        assert (
            "segments" not in call["context"].lower()
            or "restricted" not in call["context"]
        )


def test_a_security_group_member_can_retrieve_restricted_content(
    db_client, sample_corpus, llm_calls
):
    """The allowed path: authorization narrows access, it must not break it."""
    assert chat(db_client, "security", NETWORK_QUESTION).status_code == 200
    assert "Internal Network Architecture Overview" in sources_sent_to_model(llm_calls)
    assert any(
        "network segments" in c["context"].lower() or "segments" in c["context"].lower()
        for c in llm_calls
    )


def test_an_app_role_alone_does_not_unlock_restricted_documents(
    db_client, sample_corpus, llm_calls
):
    chat(
        db_client, "auditor", NETWORK_QUESTION
    )  # holds security_auditor, not the group
    chat(db_client, "kb_admin", NETWORK_QUESTION)  # can manage documents, not read them
    assert "Internal Network Architecture Overview" not in sources_sent_to_model(
        llm_calls
    )


def test_identity_cannot_be_injected_through_the_request(
    db_client, sample_corpus, llm_calls
):
    response = chat(
        db_client,
        "employee",
        NETWORK_QUESTION,
        query="?role=security_admin&user_id=u-security&groups=security",
        body={
            "role": "security_admin",
            "user_id": "u-security",
            "groups": ["security"],
        },
        headers={
            "X-User": "u-security",
            "X-Forwarded-User": "u-security",
            "X-Groups": "security",
            "Remote-User": "u-security",
        },
    )
    assert response.status_code == 200
    assert "Internal Network Architecture Overview" not in sources_sent_to_model(
        llm_calls
    )


def test_general_questions_are_answered_for_everyone(
    db_client, sample_corpus, llm_calls
):
    for persona in ("employee", "security", "kb_admin"):
        assert chat(db_client, persona, VPN_QUESTION).status_code == 200
    sources = sources_sent_to_model(llm_calls)
    assert "Corporate VPN Setup Guide" in sources
    assert not any("Network Architecture" in source for source in sources)
    assert len(llm_calls) == 3


# --- changes apply to the next question ---------------------------------------------


def test_granting_access_takes_effect_on_the_next_question(
    db_client, ingestion, sample_corpus, llm_calls
):
    network = ingestion.find_by_source_ref("RES-NETWORK-001")
    chat(db_client, "employee", NETWORK_QUESTION)
    assert "Internal Network Architecture Overview" not in sources_sent_to_model(
        llm_calls
    )

    ingestion.set_acl(actor_id="a", document_id=network.id, groups=["staff"])
    chat(db_client, "employee", NETWORK_QUESTION)

    assert "Internal Network Architecture Overview" in sources_sent_to_model(llm_calls)


def test_revoking_access_takes_effect_on_the_next_question(
    db_client, ingestion, sample_corpus, llm_calls
):
    network = ingestion.find_by_source_ref("RES-NETWORK-001")
    chat(db_client, "security", NETWORK_QUESTION)
    assert "Internal Network Architecture Overview" in sources_sent_to_model(llm_calls)
    llm_calls.clear()

    ingestion.set_acl(actor_id="a", document_id=network.id, groups=[])
    chat(db_client, "security", NETWORK_QUESTION)

    assert "Internal Network Architecture Overview" not in sources_sent_to_model(
        llm_calls
    )


def test_a_deleted_document_is_no_longer_used_to_answer(
    db_client, ingestion, sample_corpus, llm_calls
):
    vpn = ingestion.find_by_source_ref("GEN-VPN-001")
    ingestion.delete_document(actor_id="a", document_id=vpn.id)

    response = chat(db_client, "employee", VPN_QUESTION)

    assert "Corporate VPN Setup Guide" not in sources_sent_to_model(llm_calls)
    assert response.status_code == 200


# --- defence in depth: a lagging index cannot leak ----------------------------------


class FrozenIndex:
    """An index that is behind: it returns hits captured earlier."""

    def __init__(self, hits):
        self.hits = hits

    def search(self, principal, query, *, limit):
        return list(self.hits)


def test_hits_from_a_lagging_index_are_rechecked_before_reaching_the_model(
    db, redis, blobs, embedder, ingestion, search, drain, llm_calls, events
):
    seed_corpus(ingestion, REPO_CORPUS / "restricted")
    drain()
    network = ingestion.find_by_source_ref("RES-NETWORK-001")
    security = PERSONAS["security"]
    stale = search.search(security, NETWORK_QUESTION, limit=5)
    assert stale, "setup: the security user could find the document"

    ingestion.set_acl(actor_id="a", document_id=network.id, groups=[])  # revoked
    app = create_app(
        make_settings(),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
        search_index=FrozenIndex(stale),
    )
    client = app.test_client()
    token = login_as(client, security)

    response = client.post(
        "/chat", json={"message": NETWORK_QUESTION}, headers={CSRF_HEADER: token}
    )

    assert response.status_code == 200
    # Re-authorization is selective: the revoked document is dropped...
    sources = sources_sent_to_model(llm_calls)
    assert "Internal Network Architecture Overview" not in sources
    assert all(
        "RES-NETWORK-001" not in c["context"] and "network segments" not in c["context"]
        for c in llm_calls
    )
    # ...while a document this user may still read is kept.
    assert "Internal Employee Security Policy" in sources
    retrieval = next(e for e in events if e["event_type"] == "RETRIEVAL")
    details = retrieval["details"]
    assert details["candidate_count"] == len(stale) >= 2
    assert details["returned_count"] == details["candidate_count"] - 1


# --- pipeline behaviour -------------------------------------------------------------


def test_the_model_receives_titles_headings_and_text(db_client, add_doc, llm_calls):
    add_doc(
        "Printer guide",
        "# Printers\n\n## Jams\nOpen the tray and remove the jammedpaper.",
        ["*"],
    )
    chat(db_client, "employee", "How do I clear jammedpaper?")
    [call] = llm_calls
    assert "Source: Printer guide (Printers > Jams)" in call["context"]
    assert "jammedpaper" in call["context"]
    assert call["source"] == "Printer guide"
    assert call["query"] == "How do I clear jammedpaper?"


def test_no_relevant_documents_means_no_model_call(db_client, sample_corpus, llm_calls):
    response = chat(db_client, "employee", "What is the airspeed of a swallow?")
    assert response.status_code == 200
    assert "couldn't find relevant information" in response.get_json()["response"]
    assert llm_calls == []


def test_retrieval_is_logged_with_metrics_and_the_session_identity(
    db_client, sample_corpus, events
):
    chat(db_client, "employee", VPN_QUESTION)

    [retrieval] = [e for e in events if e["event_type"] == "RETRIEVAL"]
    details = retrieval["details"]
    assert retrieval["user_id"] == PERSONAS["employee"].user_id
    assert details["selected_source"] == "Corporate VPN Setup Guide"
    assert details["returned_count"] >= 1 and details["top_score"] > 0
    assert details["candidate_count"] >= details["returned_count"]
    assert details["latency_ms"] >= 0
    assert {e["user_id"] for e in events} == {PERSONAS["employee"].user_id}


def test_restricted_titles_never_appear_in_events_seen_by_other_users(
    db_client, sample_corpus, events
):
    chat(db_client, "employee", NETWORK_QUESTION)
    for event in events:
        assert "Network Architecture" not in str(event.get("details"))
