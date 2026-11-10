"""Every route x every persona, for both allowed and denied paths.

``POLICY`` is the single declaration of who may call what. A test fails if the
application registers a route that is not listed here (or lists one that no
longer exists), so a new endpoint can never ship without an explicit access
decision.
"""

import uuid

import pytest

from tests.support import CSRF_HEADER, PERSONAS, login_as

PUBLIC, USER, AUDITOR, KB_ADMIN = "public", "user", "auditor", "kb_admin"

SOME_ID = str(uuid.uuid4())

# (method, url rule) -> (who may call it, a concrete path to request)
POLICY = {
    ("GET", "/healthz"): (PUBLIC, "/healthz"),
    ("GET", "/readyz"): (PUBLIC, "/readyz"),
    ("GET", "/login"): (PUBLIC, "/login"),
    ("GET", "/auth/callback"): (PUBLIC, "/auth/callback"),
    ("GET", "/"): (USER, "/"),
    ("GET", "/me"): (USER, "/me"),
    ("POST", "/chat"): (USER, "/chat"),
    ("POST", "/api/chat/stream"): (USER, "/api/chat/stream"),
    ("GET", "/api/conversations"): (USER, "/api/conversations"),
    ("GET", "/api/conversations/<uuid:conversation_id>"): (
        USER,
        f"/api/conversations/{SOME_ID}",
    ),
    ("DELETE", "/api/conversations/<uuid:conversation_id>"): (
        USER,
        f"/api/conversations/{SOME_ID}",
    ),
    ("POST", "/api/messages/<uuid:message_id>/feedback"): (
        USER,
        f"/api/messages/{SOME_ID}/feedback",
    ),
    ("POST", "/logout"): (USER, "/logout"),
    ("GET", "/monitoring"): (AUDITOR, "/monitoring"),
    ("GET", "/monitoring/summary"): (AUDITOR, "/monitoring/summary"),
    ("GET", "/monitoring/events"): (AUDITOR, "/monitoring/events"),
    ("GET", "/admin/documents"): (KB_ADMIN, "/admin/documents"),
    ("GET", "/api/admin/documents"): (KB_ADMIN, "/api/admin/documents"),
    ("POST", "/api/admin/documents"): (KB_ADMIN, "/api/admin/documents"),
    ("GET", "/api/admin/documents/<uuid:document_id>"): (
        KB_ADMIN,
        f"/api/admin/documents/{SOME_ID}",
    ),
    ("DELETE", "/api/admin/documents/<uuid:document_id>"): (
        KB_ADMIN,
        f"/api/admin/documents/{SOME_ID}",
    ),
    ("POST", "/api/admin/documents/<uuid:document_id>/versions"): (
        KB_ADMIN,
        f"/api/admin/documents/{SOME_ID}/versions",
    ),
    ("PUT", "/api/admin/documents/<uuid:document_id>/acl"): (
        KB_ADMIN,
        f"/api/admin/documents/{SOME_ID}/acl",
    ),
    ("POST", "/api/admin/documents/<uuid:document_id>/reindex"): (
        KB_ADMIN,
        f"/api/admin/documents/{SOME_ID}/reindex",
    ),
}

PERSONA_NAMES = ["anonymous", *PERSONAS]
# Which personas hold each application role (see tests/support.py).
HOLDERS = {AUDITOR: {"auditor"}, KB_ADMIN: {"kb_admin"}}


def expected_allowed(kind: str, persona: str) -> bool:
    if kind == PUBLIC:
        return True
    if persona == "anonymous":
        return False
    return kind == USER or persona in HOLDERS[kind]


def call(client, method, path, token):
    headers = {"Accept": "application/json"}
    kwargs = {}
    if method in {"POST", "PUT", "DELETE"}:
        headers[CSRF_HEADER] = token
    if path in ("/chat", "/api/chat/stream"):
        kwargs["json"] = {"message": "How do I set up the VPN?"}
    if path.endswith("/feedback"):
        kwargs["json"] = {"rating": 1}
    if method == "PUT":
        kwargs["json"] = {"groups": ["staff"]}
    return client.open(path, method=method, headers=headers, **kwargs)


def test_every_registered_route_has_an_explicit_policy(app):
    registered = {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static"
        for method in rule.methods - {"HEAD", "OPTIONS"}
    }
    assert registered == set(POLICY), (
        "routes without a declared access policy: "
        f"{sorted(registered - set(POLICY))}; "
        f"stale policy entries: {sorted(set(POLICY) - registered)}"
    )


@pytest.mark.parametrize("persona", PERSONA_NAMES)
@pytest.mark.parametrize(("method", "rule"), sorted(POLICY))
def test_access_matrix(db_app, persona, method, rule):
    kind, path = POLICY[(method, rule)]
    client = db_app.test_client()
    token = "" if persona == "anonymous" else login_as(client, PERSONAS[persona])

    response = call(client, method, path, token)

    if kind == PUBLIC:
        # Reachable by everyone; what it returns is covered by other tests.
        assert response.status_code not in (401, 403)
    elif expected_allowed(kind, persona):
        # Allowed through the access check. Admin routes may still answer 400
        # or 404 (no file / unknown id): that is the handler, not a denial.
        assert response.status_code not in (401, 403), response.get_data(as_text=True)
        if path.startswith("/api/conversations/") or path.endswith("/feedback"):
            # An id nobody owns: the handler answers 404, which is not a denial.
            assert response.status_code == 404
        elif (kind in (USER, AUDITOR) and method == "GET") or path in (
            "/chat",
            "/api/chat/stream",
            "/logout",
        ):
            assert response.status_code == 200
    elif persona == "anonymous":
        assert response.status_code == 401
    else:
        assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/monitoring", "/admin/documents"])
def test_anonymous_browser_navigation_redirects_to_login(client, path):
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")


def test_forbidden_browser_navigation_is_403_not_a_login_loop(client):
    login_as(client, PERSONAS["employee"])
    response = client.get("/monitoring", headers={"Accept": "text/html"})
    assert response.status_code == 403


def test_denied_access_is_logged_with_the_caller_and_requirement(client, events):
    login_as(client, PERSONAS["employee"])
    client.get("/monitoring/events", headers={"Accept": "application/json"})
    denied = [e for e in events if e["event_type"] == "ACCESS_DENIED"]
    assert len(denied) == 1
    assert denied[0]["user_id"] == "u-employee"
    assert denied[0]["details"]["required_any"] == ["security_auditor"]


def test_admin_role_does_not_grant_monitoring_or_vice_versa(db_app):
    for persona, path, expected in [
        ("kb_admin", "/monitoring/events", 403),
        ("auditor", "/api/admin/documents", 403),
        ("security", "/api/admin/documents", 403),
        ("support_agent", "/api/admin/documents", 403),
    ]:
        client = db_app.test_client()
        login_as(client, PERSONAS[persona])
        response = client.get(path, headers={"Accept": "application/json"})
        assert response.status_code == expected, (persona, path)


def test_removed_debug_routes_stay_removed_even_for_privileged_users(client):
    login_as(client, PERSONAS["auditor"])
    for path in (
        "/documents",
        "/documents/security_admin",
        "/search/security_admin?q=network",
    ):
        assert client.get(path).status_code == 404
