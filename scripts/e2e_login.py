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
            f"{APP}{path}", headers=headers, timeout=30, **kwargs
        )

    def retrieved(self, username: str, question: str) -> str | None:
        """What this user's question retrieved first, read from the audit trail.

        Chat may answer 500 here (no model is configured in the dev stack);
        retrieval is logged before the model is called, so it still tells us
        exactly which document the permission-aware search selected.
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
