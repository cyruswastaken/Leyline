"""End-to-end check of the full stack: real OIDC login, document ingestion and
permission-aware retrieval.

Requires ``docker compose up`` (app, worker, Postgres, Redis, Keycloak,
Azurite) and the sample corpus (``docker compose run --rm app python -m
leyline.seed``). Logs in as each demo user from
deploy/keycloak/leyline-realm.json and asserts what each can reach, then
uploads a document as an administrator and watches access change as its ACL
is edited. Exits non-zero on the first deviation.

Run: python scripts/e2e_login.py
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from collections.abc import Callable

import requests

APP = "http://localhost:8000"
# Throwaway local-dev password, defined in deploy/keycloak/leyline-realm.json.
PASSWORD = "Leyline-dev-1!"  # noqa: S105

# username -> (expected roles, expected groups, may open /monitoring)
EXPECTED = {
    "alice.employee": ({"user"}, {"staff"}, False),
    "sam.security": ({"user"}, {"staff", "security"}, False),
    "ada.auditor": ({"user", "security_auditor"}, {"staff"}, True),
    "kim.admin": ({"user", "kb_admin"}, {"staff"}, False),
    "sue.agent": ({"user", "support_agent"}, {"staff"}, False),
    "hank.hr": ({"user"}, {"staff", "hr"}, False),
    "fiona.finance": ({"user"}, {"staff", "finance"}, False),
}


class Failure(SystemExit):
    pass


ANY = object()  # "no particular document expected"


def check(condition: object, message: str) -> None:
    if not condition:
        raise Failure(f"FAIL: {message}")


def login(username: str) -> requests.Session:
    session = requests.Session()
    page = session.get(
        f"{APP}/login?next=/", headers={"Accept": "text/html"}, timeout=15
    )
    # Browsers treat localhost as a secure context; python-requests does not,
    # so let it send Keycloak's Secure-flagged flow cookies over plain HTTP.
    for cookie in session.cookies:
        cookie.secure = False
    form = re.search(r'<form[^>]*action="([^"]+)"', page.text)
    check(form, f"{username}: no Keycloak login form at {page.url}")
    done = session.post(
        html.unescape(form.group(1)),  # type: ignore[union-attr]
        data={"username": username, "password": PASSWORD},
        timeout=15,
    )
    check(
        done.url.startswith(APP),
        f"{username}: login did not return to the app: {done.url}",
    )
    return session


def me(session: requests.Session) -> dict:
    return session.get(f"{APP}/me", timeout=15).json()


def wait_for(
    description: str, probe: Callable[[], bool], timeout: float = 120.0
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe():
            return
        time.sleep(1.0)
    raise Failure(f"FAIL: timed out waiting for {description}")


class Stack:
    def __init__(self) -> None:
        self.sessions: dict[str, requests.Session] = {}
        self.ids: dict[str, str] = {}
        self.csrf: dict[str, str] = {}

    def sign_in_everyone(self) -> None:
        for username, (roles, groups, monitoring_ok) in EXPECTED.items():
            session = login(username)
            profile = me(session)
            check(
                set(profile["roles"]) == roles and set(profile["groups"]) == groups,
                f"{username}: unexpected identity {profile}",
            )
            status = session.get(f"{APP}/monitoring/summary", timeout=15).status_code
            check(
                status == (200 if monitoring_ok else 403),
                f"{username}: monitoring {status}",
            )
            self.sessions[username] = session
            self.ids[username] = profile["user_id"]
            self.csrf[username] = profile["csrf_token"]
            print(f"ok  {username:15} roles={sorted(roles)} monitoring={status}")

    def post(self, username: str, path: str, **kwargs) -> requests.Response:
        headers = {"X-CSRF-Token": self.csrf[username]}
        return self.sessions[username].post(
            f"{APP}{path}", headers=headers, timeout=240, **kwargs
        )

    def retrieved(self, username: str, question: str) -> str | None:
        """What this user's question retrieved first, read from the audit trail.

        Retrieval is logged before the model is called (and the model may be
        slow, absent or down), so the audit trail tells us exactly which
        document the permission-aware search selected.
        """
        auditor = self.sessions["ada.auditor"]

        def events() -> list[dict]:
            return auditor.get(f"{APP}/monitoring/events", timeout=15).json()

        before = max((e["timestamp"] for e in events()), default="")
        self.post(username, "/chat", json={"message": question})
        for event in events():
            if (
                event["event_type"] == "RETRIEVAL"
                and event["user_id"] == self.ids[username]
                and event["timestamp"] > before
            ):
                return event["details"]["selected_source"]
        raise Failure(f"FAIL: no retrieval event for {username}: {question!r}")

    def stream(
        self, username: str, message: str, conversation_id: str | None = None
    ) -> list[tuple[str, dict]]:
        """One conversation turn over server-sent events."""
        response = self.post(
            username,
            "/api/chat/stream",
            json={"message": message, "conversation_id": conversation_id},
        )
        check(
            response.status_code == 200,
            f"{username}: stream returned {response.status_code} {response.text[:200]}",
        )
        events = []
        for block in response.text.strip().split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((fields["event"], json.loads(fields["data"])))
        return events

    def get_json(self, username: str, path: str) -> requests.Response:
        return self.sessions[username].get(
            f"{APP}{path}", headers={"Accept": "application/json"}, timeout=30
        )

    def documents(self) -> list[dict]:
        response = self.sessions["kim.admin"].get(
            f"{APP}/api/admin/documents",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        check(
            response.status_code == 200,
            f"admin listing returned {response.status_code}",
        )
        return response.json()["documents"]


def main() -> int:
    stack = Stack()
    stack.sign_in_everyone()

    # --- the sample corpus is indexed and access follows the ACLs ---------------
    wait_for(
        "the seeded corpus to be indexed",
        lambda: (
            len(docs := stack.documents()) >= 49
            and all(d["latest_version"]["status"] == "indexed" for d in docs)
        ),
    )
    print(f"ok  {len(stack.documents())} documents indexed")

    network = "internal network architecture segments administrative interfaces"
    incident = "blameless review severity one on-call lead isolate hosts"
    review = "calibration sessions five-point scale ratings manager chain"
    procurement = "competitive quote finance director splitting purchase threshold"
    leave = "annual leave carry over twenty-five days entitlement"
    # (user, question, expected top source, sources they must never be given)
    cases = [
        ("sam.security", network, "Internal Network Architecture Overview", set()),
        ("alice.employee", network, ANY, {"Internal Network Architecture Overview"}),
        ("sam.security", incident, "Security Incident Response Playbook", set()),
        ("alice.employee", incident, ANY, {"Security Incident Response Playbook"}),
        ("hank.hr", review, "Performance Review Process", set()),
        ("alice.employee", review, ANY, {"Performance Review Process"}),
        ("sam.security", review, ANY, {"Performance Review Process"}),
        ("fiona.finance", procurement, "Procurement Approval Thresholds", set()),
        ("hank.hr", procurement, ANY, {"Procurement Approval Thresholds"}),
        ("alice.employee", leave, "Annual Leave Policy", set()),
        ("sam.security", leave, "Annual Leave Policy", set()),
    ]
    for user, question, expected, forbidden in cases:
        got = stack.retrieved(user, question)
        check(
            expected is ANY or got == expected,
            f"{user} asked {question!r}: expected {expected!r}, got {got!r}",
        )
        check(
            got not in forbidden,
            f"{user} was given a document they may not read: {got!r}",
        )
    print(f"ok  {len(cases)} access checks on the sample corpus")

    # --- semantic retrieval (needs an embedding model; opt in with E2E_SEMANTIC=1) -
    if os.environ.get("E2E_SEMANTIC") == "1":
        ready = requests.get(f"{APP}/readyz", timeout=15).json()
        check(
            ready["optional"]["embeddings_configured"],
            "E2E_SEMANTIC=1 but the app has no embedding model configured",
        )
        meaning_cases = [
            # (user, question sharing no wording with the answer, expected top document)
            (
                "alice.employee",
                "Someone stole my backpack with my work laptop inside",
                "Reporting a Lost or Stolen Device",
            ),
            (
                "alice.employee",
                "My partner and I are expecting a baby, how much time off can we take?",
                "Parental Leave",
            ),
            (
                "alice.employee",
                "How do I get a guest into the building for a meeting?",
                "Registering Visitors",
            ),
            (
                "hank.hr",
                "How are performance ratings compared fairly across teams?",
                "Performance Review Process",
            ),
            ("alice.employee", "What is the capital of Australia?", None),
        ]
        for user, question, expected in meaning_cases:
            got = stack.retrieved(user, question)
            check(
                got == expected,
                f"{user} asked {question!r}: expected {expected!r}, got {got!r}",
            )
        # the same paraphrase must not unlock a document the asker may not read
        leaked = stack.retrieved(
            "alice.employee",
            "How are performance ratings compared fairly across teams?",
        )
        check(
            leaked != "Performance Review Process",
            "semantic search leaked an HR-only document",
        )
        print(
            f"ok  {len(meaning_cases) + 1} semantic retrieval checks (paraphrases, refusal, no leak)"
        )

    # --- conversations with a real chat model (opt in with E2E_CHAT=1) ------------
    if os.environ.get("E2E_CHAT") == "1":
        ready = requests.get(f"{APP}/readyz", timeout=15).json()
        check(
            ready["checks"]["llm_configured"],
            "E2E_CHAT=1 but no chat model is configured",
        )

        events = stack.stream(
            "alice.employee", "How do I connect to the corporate VPN?"
        )
        kinds = [kind for kind, _ in events]
        check(
            kinds[0] == "meta" and kinds[-1] == "done",
            f"turn did not complete: {kinds[-3:]}",
        )
        done = events[-1][1]
        check(len(done["content"]) > 20, f"answer is empty: {done['content']!r}")
        titles = {c["title"] for c in done["citations"]}
        check(
            "Corporate VPN Setup Guide" in titles, f"VPN guide not retrieved: {titles}"
        )
        print(
            f"ok  streamed answer ({kinds.count('delta')} chunks, sources={sorted(titles)})"
        )
        conversation_id = events[0][1]["conversation_id"]

        follow = stack.stream(
            "alice.employee", "and what if it asks for a code?", conversation_id
        )
        check(follow[-1][0] == "done", f"follow-up did not complete: {follow[-1]}")
        saved = stack.get_json(
            "alice.employee", f"/api/conversations/{conversation_id}"
        )
        roles = [m["role"] for m in saved.json()["messages"]]
        check(roles == ["user", "assistant", "user", "assistant"], f"history: {roles}")
        print("ok  follow-up turn saved in the same conversation")

        rated = stack.post(
            "alice.employee",
            f"/api/messages/{done['message_id']}/feedback",
            json={"rating": 1},
        )
        check(rated.status_code == 200, f"feedback returned {rated.status_code}")

        other = stack.get_json("sam.security", f"/api/conversations/{conversation_id}")
        check(
            other.status_code == 404,
            f"another user read the conversation: {other.status_code}",
        )

        employee_net = stack.stream("alice.employee", network)
        leaked = [c["title"] for c in employee_net[-1][1].get("citations", [])]
        check(
            "Internal Network Architecture Overview" not in leaked,
            f"employee was cited a restricted document: {leaked}",
        )
        security_net = stack.stream("sam.security", network)
        cited = [c["title"] for c in security_net[-1][1].get("citations", [])]
        check(
            "Internal Network Architecture Overview" in cited,
            f"security was not given the document they may read: {cited}",
        )
        refused = stack.stream("alice.employee", "What is the capital of Australia?")
        check(
            "couldn't find" in refused[-1][1]["content"],
            f"out-of-scope question was answered: {refused[-1][1]['content'][:100]!r}",
        )
        print("ok  chat feedback, ownership, permissions and refusals")

    # --- ticket escalation (no model needed) ---------------------------------------
    marker_subject = f"E2E printer {int(time.time())}"
    created = stack.post(
        "alice.employee",
        "/api/tickets",
        json={"subject": marker_subject, "description": "The printer on 3 is jammed."},
    )
    check(created.status_code == 201, f"ticket create returned {created.status_code}")
    ticket_id = created.json()["id"]
    check(
        stack.get_json("sam.security", f"/api/tickets/{ticket_id}").status_code == 404,
        "another employee could read the ticket",
    )
    check(
        stack.get_json("sam.security", "/api/agent/tickets").status_code == 403,
        "an employee reached the support queue",
    )
    queue = stack.get_json("sue.agent", "/api/agent/tickets?status=open").json()
    check(
        ticket_id in {t["id"] for t in queue["tickets"]},
        "the agent queue does not show the new ticket",
    )
    check(
        stack.post(
            "sue.agent", f"/api/agent/tickets/{ticket_id}/assign", json={"assign": True}
        ).status_code
        == 200,
        "agent assign failed",
    )
    check(
        stack.post(
            "sue.agent",
            f"/api/tickets/{ticket_id}/comments",
            json={"body": "On my way."},
        ).status_code
        == 200,
        "agent comment failed",
    )
    check(
        stack.post(
            "sue.agent", f"/api/tickets/{ticket_id}/status", json={"status": "resolved"}
        ).status_code
        == 200,
        "agent resolve failed",
    )
    seen = stack.get_json("alice.employee", f"/api/tickets/{ticket_id}").json()
    check(
        seen["status"] == "resolved" and seen["assignee_name"],
        f"requester view is wrong: {seen['status']}",
    )
    closed = stack.post(
        "alice.employee", f"/api/tickets/{ticket_id}/status", json={"status": "closed"}
    )
    check(closed.status_code == 200, f"requester close returned {closed.status_code}")
    print(
        "ok  ticket flow: create, privacy, agent queue, assign, comment, resolve, close"
    )

    # --- administrators: authorization and validation ----------------------------
    check(
        stack.post("alice.employee", "/api/admin/documents").status_code == 403,
        "employee upload",
    )
    check(
        stack.sessions["kim.admin"]
        .post(f"{APP}/api/admin/documents", timeout=15)
        .status_code
        == 403,
        "upload without a CSRF token",
    )
    bad = stack.post(
        "kim.admin",
        "/api/admin/documents",
        files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
        data={"title": "x", "groups": "staff"},
    )
    check(
        bad.status_code == 400 and "Unsupported" in bad.text,
        f"exe upload: {bad.status_code}",
    )
    print("ok  admin authorization and validation")

    # --- upload -> index -> ACL changes -> delete --------------------------------
    marker = f"e2eplatypus{int(time.time())}"
    question = marker  # a single unique word: only this document can match
    upload = stack.post(
        "kim.admin",
        "/api/admin/documents",
        files={
            "file": (
                "note.md",
                f"# Note\n\nThe {marker} procedure is documented here.".encode(),
                "text/markdown",
            )
        },
        data={"title": "E2E note", "groups": "hr"},
    )
    check(
        upload.status_code == 202,
        f"upload returned {upload.status_code}: {upload.text}",
    )
    document_id = upload.json()["document_id"]

    def indexed() -> bool:
        found = [d for d in stack.documents() if d["id"] == document_id]
        return bool(found) and found[0]["latest_version"]["status"] == "indexed"

    wait_for("the uploaded document to be indexed", indexed)
    check(
        stack.retrieved("hank.hr", question) == "E2E note",
        "hr member should find the upload",
    )
    check(
        stack.retrieved("alice.employee", question) is None,
        "staff must not find an hr-only upload",
    )
    check(
        stack.retrieved("sam.security", question) is None,
        "security must not find an hr-only upload",
    )
    print("ok  uploaded document is searchable by its ACL group only")

    def set_acl(groups: list[str]) -> None:
        response = stack.sessions["kim.admin"].put(
            f"{APP}/api/admin/documents/{document_id}/acl",
            json={"groups": groups},
            headers={"X-CSRF-Token": stack.csrf["kim.admin"]},
            timeout=15,
        )
        check(
            response.status_code == 200, f"ACL update returned {response.status_code}"
        )

    set_acl(["staff"])
    check(
        stack.retrieved("alice.employee", question) == "E2E note",
        "granting staff takes effect immediately",
    )
    set_acl(["security"])
    check(
        stack.retrieved("alice.employee", question) is None,
        "revoking staff takes effect immediately",
    )
    check(
        stack.retrieved("hank.hr", question) is None,
        "hr lost access when the ACL changed",
    )
    check(
        stack.retrieved("sam.security", question) == "E2E note",
        "security gained access",
    )
    set_acl([])
    check(
        stack.retrieved("sam.security", question) is None,
        "an empty ACL hides the document from everyone",
    )
    print("ok  ACL changes apply to the very next question")

    set_acl(["*"])
    check(stack.retrieved("alice.employee", question) == "E2E note", "everyone entry")
    deleted = stack.sessions["kim.admin"].delete(
        f"{APP}/api/admin/documents/{document_id}",
        headers={"X-CSRF-Token": stack.csrf["kim.admin"]},
        timeout=15,
    )
    check(deleted.status_code == 204, f"delete returned {deleted.status_code}")
    check(
        stack.retrieved("alice.employee", question) is None,
        "deleted documents are unsearchable at once",
    )
    check(
        document_id not in {d["id"] for d in stack.documents()},
        "deleted document still listed",
    )
    print("ok  deletion is immediate")

    print("\nALL END-TO-END CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
