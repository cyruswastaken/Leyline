"""Every route x every persona, for both allowed and denied paths.

``POLICY`` is the single declaration of who may call what. A test fails if the
application registers a route that is not listed here, so a new endpoint can
never ship without an explicit access decision.
"""

import pytest

from tests.support import CSRF_HEADER, PERSONAS, login_as

PUBLIC, USER, AUDITOR = "public", "user", "auditor"

# (method, path) -> who may call it
POLICY = {
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/readyz"): PUBLIC,
    ("GET", "/login"): PUBLIC,
    ("GET", "/auth/callback"): PUBLIC,
    ("GET", "/"): USER,
    ("GET", "/me"): USER,
    ("POST", "/chat"): USER,
    ("POST", "/logout"): USER,
    ("GET", "/monitoring"): AUDITOR,
    ("GET", "/monitoring/summary"): AUDITOR,
    ("GET", "/monitoring/events"): AUDITOR,
}

PERSONA_NAMES = ["anonymous", *PERSONAS]
# Only this persona holds the security_auditor app role.
AUDITORS = {"auditor"}


def expected_allowed(kind: str, persona: str) -> bool:
    if kind == PUBLIC:
        return True
    if persona == "anonymous":
        return False
    return kind == USER or (kind == AUDITOR and persona in AUDITORS)


def call(client, method, path, token):
    headers = {"Accept": "application/json"}
    kwargs = {}
    if method == "POST":
        headers[CSRF_HEADER] = token
        if path == "/chat":
            kwargs["json"] = {"message": "How do I set up the VPN?"}
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
@pytest.mark.parametrize(("method", "path"), sorted(POLICY))
def test_access_matrix(app, persona, method, path):
    client = app.test_client()
    token = "" if persona == "anonymous" else login_as(client, PERSONAS[persona])

    response = call(client, method, path, token)
    kind = POLICY[(method, path)]

    if kind == PUBLIC:
        # Reachable by everyone; what it returns is covered by other tests.
        assert response.status_code not in (401, 403)
    elif expected_allowed(kind, persona):
        assert response.status_code == 200, response.get_data(as_text=True)
    elif persona == "anonymous":
        assert response.status_code == 401
    else:
        assert response.status_code == 403


@pytest.mark.parametrize("path", ["/", "/monitoring"])
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


def test_removed_debug_routes_stay_removed_even_for_privileged_users(client):
    login_as(client, PERSONAS["auditor"])
    for path in (
        "/documents",
        "/documents/security_admin",
        "/search/security_admin?q=network",
    ):
        assert client.get(path).status_code == 404
