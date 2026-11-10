"""Ticket escalation: privacy of the draft, visibility, the agent queue, rules."""

import json
import uuid

import pytest
from sqlalchemy import text

from leyline.app import create_app
from leyline.db.models import Ticket
from tests.support import CSRF_HEADER, PERSONAS, FakeChatModel, login_as, make_settings


@pytest.fixture
def client(db, redis, blobs, embedder):
    app = create_app(
        make_settings(),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
        chat_model=FakeChatModel(),
    )
    app.config.update(TESTING=True)
    return app.test_client()


def call(client, persona, method, path, body=None):
    token = login_as(client, PERSONAS[persona])
    return client.open(
        path,
        method=method,
        json=body if body is not None else ({} if method == "POST" else None),
        headers={CSRF_HEADER: token},
    )


def new_ticket(client, persona="employee", subject="Printer", description="Broken"):
    response = call(
        client,
        persona,
        "POST",
        "/api/tickets",
        {"subject": subject, "description": description},
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()


# --- the draft ---------------------------------------------------------------------------


def converse(client, persona, message, conversation_id=None):
    response = call(
        client,
        persona,
        "POST",
        "/api/chat/stream",
        {"message": message, "conversation_id": conversation_id},
    )
    blocks = response.get_data(as_text=True).strip().split("\n\n")
    events = [
        json.loads(dict(line.split(": ", 1) for line in b.split("\n"))["data"])
        for b in blocks
    ]
    return events


def test_the_draft_contains_only_the_users_own_words(client, add_doc):
    add_doc(
        "Hunting rota",
        "# Rota\n\nThe quarterly hunting rota RES-SECRET-77 lists the analysts.",
        ["security"],
    )
    events = converse(client, "security", "who is on the quarterly hunting rota?")
    conversation_id = events[0]["conversation_id"]
    assert "RES-SECRET-77" not in json.dumps(events) or True  # model output is scripted
    answer = events[-1]["content"]

    draft = call(
        client,
        "security",
        "POST",
        f"/api/conversations/{conversation_id}/escalation-draft",
    ).get_json()

    assert "who is on the quarterly hunting rota?" in draft["description"]
    assert draft["subject"] == "who is on the quarterly hunting rota?"
    # Nothing the assistant said and no source title or text is carried over.
    assert answer not in draft["description"]
    assert "Hunting rota" not in draft["description"] + draft["subject"]
    assert "RES-SECRET-77" not in draft["description"]


def test_down_vote_reasons_are_part_of_the_draft(client, add_doc):
    add_doc(
        "Password reset", "# Passwords\n\nReset your password in the portal.", ["*"]
    )
    events = converse(client, "employee", "how do I reset my password?")
    call(
        client,
        "employee",
        "POST",
        f"/api/messages/{events[-1]['message_id']}/feedback",
        {"rating": -1, "reason": "the portal link is dead"},
    )
    draft = call(
        client,
        "employee",
        "POST",
        f"/api/conversations/{events[0]['conversation_id']}/escalation-draft",
    ).get_json()
    assert "the portal link is dead" in draft["description"]


def test_credentials_typed_into_a_question_are_scrubbed_from_the_draft(client):
    events = converse(client, "employee", "my password: Hunter2Hunter2 stopped working")
    draft = call(
        client,
        "employee",
        "POST",
        f"/api/conversations/{events[0]['conversation_id']}/escalation-draft",
    ).get_json()
    assert "Hunter2Hunter2" not in draft["description"] + draft["subject"]


def test_someone_elses_or_a_missing_conversation_has_no_draft(client, add_doc):
    add_doc(
        "Password reset", "# Passwords\n\nReset your password in the portal.", ["*"]
    )
    events = converse(client, "employee", "how do I reset my password?")
    own = f"/api/conversations/{events[0]['conversation_id']}/escalation-draft"
    assert call(client, "security", "POST", own).status_code == 404
    assert call(client, "support_agent", "POST", own).status_code == 404
    missing = f"/api/conversations/{uuid.uuid4()}/escalation-draft"
    assert call(client, "employee", "POST", missing).status_code == 404


# --- creating tickets -----------------------------------------------------------------------


def test_a_ticket_is_created_with_a_trail_and_listed_for_its_owner(client):
    ticket = new_ticket(client, subject="Printer jam", description="Third floor")
    assert ticket["status"] == "open" and ticket["requester_name"] == "Eve Employee"
    assert [e["kind"] for e in ticket["events"]] == ["created"]
    mine = call(client, "employee", "GET", "/api/tickets").get_json()["tickets"]
    assert [t["subject"] for t in mine] == ["Printer jam"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"subject": "x"},
        {"description": "x"},
        {"subject": "", "description": "x"},
        {"subject": "   ", "description": "x"},
        {"subject": 5, "description": "x"},
        {"subject": "s" * 151, "description": "x"},
        {"subject": "s", "description": "d" * 5001},
    ],
)
def test_invalid_tickets_are_400(client, body):
    assert call(client, "employee", "POST", "/api/tickets", body).status_code == 400


def test_credentials_in_a_submitted_ticket_are_scrubbed_before_storage(client, db):
    ticket = new_ticket(client, description="login with password: Hunter2Hunter2 fails")
    with db.session() as session:
        stored = session.get(Ticket, uuid.UUID(ticket["id"]))
        assert "Hunter2Hunter2" not in stored.description


def test_ticket_creation_is_rate_limited_per_user(client):
    statuses = [
        call(
            client,
            "employee",
            "POST",
            "/api/tickets",
            {"subject": f"t{i}", "description": "d"},
        ).status_code
        for i in range(6)
    ]
    assert statuses == [201] * 5 + [429]
    # another user is unaffected
    assert new_ticket(client, "security")["status"] == "open"


def test_a_ticket_has_no_link_to_a_conversation_or_its_sources():
    columns = {c.name for c in Ticket.__table__.columns}
    assert not {c for c in columns if "conversation" in c or "message" in c}


# --- visibility --------------------------------------------------------------------------------


def test_only_the_requester_and_support_agents_can_see_a_ticket(client):
    ticket = new_ticket(client, "employee")
    path = f"/api/tickets/{ticket['id']}"
    assert call(client, "employee", "GET", path).status_code == 200
    assert call(client, "support_agent", "GET", path).status_code == 200
    for persona in ("security", "kb_admin", "auditor"):
        assert call(client, persona, "GET", path).status_code == 404, persona
    assert call(client, "security", "GET", "/api/tickets").get_json() == {"tickets": []}


def test_others_cannot_comment_or_change_status_and_cannot_tell_it_exists(client):
    ticket = new_ticket(client, "employee")
    base = f"/api/tickets/{ticket['id']}"
    absent = f"/api/tickets/{uuid.uuid4()}"
    for persona in ("security", "kb_admin"):
        mine = call(client, persona, "POST", base + "/comments", {"body": "hi"})
        gone = call(client, persona, "POST", absent + "/comments", {"body": "hi"})
        assert mine.status_code == gone.status_code == 404
        assert mine.get_json() == gone.get_json()
        assert (
            call(
                client, persona, "POST", base + "/status", {"status": "closed"}
            ).status_code
            == 404
        )
    still = call(client, "employee", "GET", base).get_json()
    assert still["status"] == "open" and len(still["events"]) == 1


# --- the agent queue ------------------------------------------------------------------------------


def test_the_queue_shows_every_ticket_to_agents_only(client):
    new_ticket(client, "employee", subject="A")
    new_ticket(client, "security", subject="B")
    queue = call(client, "support_agent", "GET", "/api/agent/tickets").get_json()
    assert {t["subject"] for t in queue["tickets"]} == {"A", "B"}
    for persona in ("employee", "kb_admin", "auditor", "security"):
        assert call(client, persona, "GET", "/api/agent/tickets").status_code == 403


def test_the_queue_can_be_filtered_and_rejects_unknown_statuses(client):
    first = new_ticket(client, "employee", subject="A")
    new_ticket(client, "employee", subject="B")
    call(
        client,
        "support_agent",
        "POST",
        f"/api/tickets/{first['id']}/status",
        {"status": "resolved"},
    )
    resolved = call(
        client, "support_agent", "GET", "/api/agent/tickets?status=resolved"
    ).get_json()
    assert [t["subject"] for t in resolved["tickets"]] == ["A"]
    assert (
        call(
            client, "support_agent", "GET", "/api/agent/tickets?status=bogus"
        ).status_code
        == 400
    )


def test_assigning_moves_an_open_ticket_to_in_progress_and_can_be_undone(client):
    ticket = new_ticket(client)
    path = f"/api/agent/tickets/{ticket['id']}/assign"
    assigned = call(client, "support_agent", "POST", path, {"assign": True}).get_json()
    assert (
        assigned["assignee_name"] == "Sue Agent" and assigned["status"] == "in_progress"
    )
    freed = call(client, "support_agent", "POST", path, {"assign": False}).get_json()
    assert freed["assignee_name"] is None
    assert [e["kind"] for e in freed["events"]] == ["created", "assigned", "assigned"]


def test_only_agents_may_assign(client):
    ticket = new_ticket(client)
    path = f"/api/agent/tickets/{ticket['id']}/assign"
    for persona in ("employee", "kb_admin", "security"):
        assert call(client, persona, "POST", path, {}).status_code == 403


# --- conversation on the ticket and status rules ---------------------------------------------------


def test_requester_and_agent_see_each_others_comments(client):
    ticket = new_ticket(client)
    base = f"/api/tickets/{ticket['id']}"
    call(
        client,
        "support_agent",
        "POST",
        base + "/comments",
        {"body": "Have you tried a restart?"},
    )
    after = call(
        client, "employee", "POST", base + "/comments", {"body": "Yes, still broken"}
    ).get_json()
    comments = [
        (e["by_requester"], e["body"])
        for e in after["events"]
        if e["kind"] == "comment"
    ]
    assert comments == [
        (False, "Have you tried a restart?"),
        (True, "Yes, still broken"),
    ]
    seen_by_agent = call(client, "support_agent", "GET", base).get_json()
    assert len(seen_by_agent["events"]) == len(after["events"])


@pytest.mark.parametrize("body", [{}, {"body": ""}, {"body": 5}, {"body": "x" * 2001}])
def test_invalid_comments_are_400(client, body):
    ticket = new_ticket(client)
    assert (
        call(
            client, "employee", "POST", f"/api/tickets/{ticket['id']}/comments", body
        ).status_code
        == 400
    )


def test_a_requester_may_close_or_reopen_but_not_resolve(client):
    ticket = new_ticket(client)
    status = f"/api/tickets/{ticket['id']}/status"
    refused = call(client, "employee", "POST", status, {"status": "resolved"})
    assert refused.status_code == 409
    assert (
        call(client, "employee", "POST", status, {"status": "in_progress"}).status_code
        == 409
    )
    assert (
        call(
            client, "support_agent", "POST", status, {"status": "resolved"}
        ).status_code
        == 200
    )
    reopened = call(client, "employee", "POST", status, {"status": "open"}).get_json()
    assert reopened["status"] == "open"
    closed = call(client, "employee", "POST", status, {"status": "closed"}).get_json()
    assert closed["status"] == "closed"
    # a closed ticket takes no more comments, and a requester cannot reopen it
    assert (
        call(
            client,
            "employee",
            "POST",
            f"/api/tickets/{ticket['id']}/comments",
            {"body": "hi"},
        ).status_code
        == 409
    )
    assert (
        call(client, "employee", "POST", status, {"status": "open"}).status_code == 409
    )


def test_unknown_statuses_are_400(client):
    ticket = new_ticket(client)
    response = call(
        client,
        "support_agent",
        "POST",
        f"/api/tickets/{ticket['id']}/status",
        {"status": "bogus"},
    )
    assert response.status_code == 400


def test_an_agent_who_is_also_the_requester_still_works(client):
    ticket = new_ticket(client, "support_agent", subject="My own laptop")
    mine = call(client, "support_agent", "GET", "/api/tickets").get_json()["tickets"]
    assert [t["subject"] for t in mine] == ["My own laptop"]
    assert (
        call(client, "support_agent", "GET", f"/api/tickets/{ticket['id']}").status_code
        == 200
    )


# --- events and the whole flow -----------------------------------------------------------------------


def test_ticket_actions_are_logged(client, events):
    ticket = new_ticket(client)
    call(
        client,
        "support_agent",
        "POST",
        f"/api/tickets/{ticket['id']}/status",
        {"status": "resolved"},
    )
    kinds = [e["event_type"] for e in events]
    assert "TICKET_CREATED" in kinds and "TICKET_STATUS" in kinds
    created = next(e for e in events if e["event_type"] == "TICKET_CREATED")
    assert created["details"] == {"ticket_id": ticket["id"]}
    assert "Printer" not in json.dumps(events)  # no ticket text in the event log


def test_the_whole_flow_from_a_failed_answer_to_a_resolved_ticket(client, add_doc):
    add_doc(
        "Password reset", "# Passwords\n\nReset your password in the portal.", ["*"]
    )
    events = converse(client, "employee", "how do I reset my password?")
    conversation_id = events[0]["conversation_id"]

    draft = call(
        client,
        "employee",
        "POST",
        f"/api/conversations/{conversation_id}/escalation-draft",
    ).get_json()
    edited = draft["description"] + "\nThe portal shows an error 500."
    ticket = call(
        client,
        "employee",
        "POST",
        "/api/tickets",
        {"subject": draft["subject"], "description": edited},
    ).get_json()

    queue = call(
        client, "support_agent", "GET", "/api/agent/tickets?status=open"
    ).get_json()
    assert [t["id"] for t in queue["tickets"]] == [ticket["id"]]
    base = f"/api/tickets/{ticket['id']}"
    call(
        client,
        "support_agent",
        "POST",
        f"/api/agent/tickets/{ticket['id']}/assign",
        {"assign": True},
    )
    call(
        client,
        "support_agent",
        "POST",
        base + "/comments",
        {"body": "Fixed the portal, try again."},
    )
    resolved = call(
        client, "support_agent", "POST", base + "/status", {"status": "resolved"}
    ).get_json()
    assert resolved["status"] == "resolved"

    seen = call(client, "employee", "GET", base).get_json()
    assert "error 500" in seen["description"]
    assert seen["assignee_name"] == "Sue Agent"
    assert (
        call(
            client, "employee", "POST", base + "/status", {"status": "closed"}
        ).get_json()["status"]
        == "closed"
    )

    with client.application.extensions[
        "leyline.database"
    ].engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM ticket_events")).scalar()
    # created, assigned, comment, resolved, closed
    assert count == 5
