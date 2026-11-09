"""End-to-end check of the real OIDC login flow against the local stack.

Requires `docker compose up` (app + Redis + Keycloak). Logs in as each demo
user from deploy/keycloak/leyline-realm.json and asserts what each can reach.
Exits non-zero on the first deviation. Run: python scripts/e2e_login.py
"""

from __future__ import annotations

import html
import re
import sys

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
}


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
    if not form:
        raise SystemExit(f"{username}: no Keycloak login form at {page.url}")
    done = session.post(
        html.unescape(form.group(1)),
        data={"username": username, "password": PASSWORD},
        timeout=15,
    )
    if not done.url.startswith(APP):
        raise SystemExit(f"{username}: login did not return to the app: {done.url}")
    return session


def main() -> int:
    sessions: dict[str, requests.Session] = {}
    for username, (roles, groups, monitoring_ok) in EXPECTED.items():
        session = login(username)
        sessions[username] = session
        me = session.get(f"{APP}/me", timeout=15).json()
        if set(me["roles"]) != roles or set(me["groups"]) != groups:
            raise SystemExit(f"{username}: unexpected identity {me}")
        status = session.get(f"{APP}/monitoring/summary", timeout=15).status_code
        if status != (200 if monitoring_ok else 403):
            raise SystemExit(f"{username}: /monitoring/summary returned {status}")
        print(f"ok  {username:15} roles={sorted(roles)} monitoring={status}")

    # Authorization before retrieval, observed through the audit trail: ask
    # the same question as an employee and as a security-group member, then
    # read what each retrieval actually selected.
    question = {"message": "Describe the internal network architecture segments"}
    for username in ("alice.employee", "sam.security"):
        session = sessions[username]
        token = session.get(f"{APP}/me", timeout=15).json()["csrf_token"]
        session.post(
            f"{APP}/chat", json=question, headers={"X-CSRF-Token": token}, timeout=30
        )  # may be 500 without a model; retrieval is logged before that call

    events = sessions["ada.auditor"].get(f"{APP}/monitoring/events", timeout=15).json()
    selected = {
        event["user_id"]: event["details"]["selected_source"]
        for event in reversed(events)
        if event["event_type"] == "RETRIEVAL"
    }
    ids = {
        u: sessions[u].get(f"{APP}/me", timeout=15).json()["user_id"]
        for u in ("alice.employee", "sam.security")
    }
    if selected.get(ids["sam.security"]) != "Internal Network Architecture Overview":
        raise SystemExit(f"security user did not retrieve restricted doc: {selected}")
    if "Network Architecture" in str(selected.get(ids["alice.employee"])):
        raise SystemExit(f"employee retrieved a restricted doc: {selected}")
    print("ok  restricted retrieval: security-group member yes, employee no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
