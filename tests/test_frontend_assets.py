"""Serving the compiled frontend: asset tags, CSP, caching and exemptions."""

import contextlib
import json

import pytest
from pydantic import ValidationError

from leyline import frontend
from leyline.app import create_app
from leyline.config import Settings
from leyline.frontend import vite_assets
from tests.support import make_settings

MANIFEST = {
    "src/entries/chat.ts": {
        "file": "assets/chat-AAA.js",
        "isEntry": True,
        "css": ["assets/chat-BBB.css"],
        "imports": ["_shared-CCC.js"],
    },
    "_shared-CCC.js": {
        "file": "assets/shared-CCC.js",
        "css": ["assets/shared-DDD.css"],
        "imports": ["_deep-EEE.js"],
    },
    "_deep-EEE.js": {"file": "assets/deep-EEE.js"},
}


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    return path


def test_an_entry_gets_its_styles_preloads_and_script(manifest):
    html = str(vite_assets(make_settings(), "chat", manifest_path=manifest))
    assert '<link rel="stylesheet" href="/static/dist/assets/chat-BBB.css">' in html
    assert '<link rel="stylesheet" href="/static/dist/assets/shared-DDD.css">' in html
    assert '<link rel="modulepreload" href="/static/dist/assets/shared-CCC.js">' in html
    assert '<link rel="modulepreload" href="/static/dist/assets/deep-EEE.js">' in html
    assert html.rstrip().endswith(
        '<script type="module" src="/static/dist/assets/chat-AAA.js"></script>'
    )
    # styles first, so the page never paints unstyled
    assert html.index("stylesheet") < html.index("<script")


def test_no_manifest_means_no_tags_instead_of_an_error(tmp_path):
    assert (
        str(vite_assets(make_settings(), "chat", manifest_path=tmp_path / "none.json"))
        == ""
    )


def test_an_unknown_entry_emits_nothing(manifest):
    assert str(vite_assets(make_settings(), "nope", manifest_path=manifest)) == ""


def test_file_names_are_escaped(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps({"src/entries/x.ts": {"file": 'a"><script>alert(1)</script>.js'}}),
        encoding="utf-8",
    )
    html = str(vite_assets(make_settings(), "x", manifest_path=path))
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_the_manifest_is_re_read_when_it_changes(manifest):
    first = str(vite_assets(make_settings(), "chat", manifest_path=manifest))
    changed = json.loads(manifest.read_text(encoding="utf-8"))
    changed["src/entries/chat.ts"]["file"] = "assets/chat-NEW.js"
    manifest.write_text(json.dumps(changed), encoding="utf-8")
    import os

    stat = manifest.stat()
    os.utime(manifest, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    second = str(vite_assets(make_settings(), "chat", manifest_path=manifest))
    assert "chat-AAA.js" in first and "chat-NEW.js" in second


# --- development server ----------------------------------------------------------------


def test_the_dev_server_serves_scripts_with_hot_reload():
    html = str(
        vite_assets(make_settings(frontend_dev_server="http://localhost:5173/"), "chat")
    )
    assert 'src="http://localhost:5173/@vite/client"' in html
    assert 'src="http://localhost:5173/src/entries/chat.ts"' in html


def test_production_refuses_a_dev_server():
    from tests.test_config import FULL_PRODUCTION

    with pytest.raises(ValidationError, match="FRONTEND_DEV_SERVER"):
        Settings(
            _env_file=None,
            **{**FULL_PRODUCTION, "frontend_dev_server": "http://x:5173"},
        )


# --- CSP, caching, exemptions ------------------------------------------------------------


def app_for(redis, **overrides):
    app = create_app(make_settings(**overrides), redis_client=redis)
    app.config.update(TESTING=True)
    return app


def csp(client):
    return client.get("/healthz").headers["Content-Security-Policy"]


def test_the_csp_allows_only_self_hosted_fonts_and_no_unsafe_inline(redis):
    policy = csp(app_for(redis).test_client())
    assert "font-src 'self'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "localhost:5173" not in policy


def test_the_dev_origin_is_added_only_when_configured(redis):
    policy = csp(
        app_for(redis, frontend_dev_server="http://localhost:5173").test_client()
    )
    assert "script-src 'self'" in policy and "http://localhost:5173" in policy
    assert "ws://localhost:5173" in policy


@pytest.fixture
def asset_file():
    folder = frontend.STATIC_DIST / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "test-asset-abc123.js"
    path.write_text("console.log(1)", encoding="utf-8")
    other = frontend.STATIC_DIST / "plain.txt"
    other.write_text("x", encoding="utf-8")
    yield path
    path.unlink()
    other.unlink()
    for parent in (folder, frontend.STATIC_DIST):
        with contextlib.suppress(OSError):
            parent.rmdir()


def test_hashed_assets_are_cached_for_good_and_nothing_else_is(redis, asset_file):
    client = app_for(redis).test_client()
    hashed = client.get("/static/dist/assets/test-asset-abc123.js")
    assert hashed.status_code == 200
    assert hashed.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    plain = client.get("/static/dist/plain.txt")
    assert (
        plain.status_code == 200 and "immutable" not in plain.headers["Cache-Control"]
    )


def test_static_files_need_no_session_and_are_not_rate_limited(redis, asset_file):
    client = app_for(redis, rate_limit_ip_requests=1).test_client()
    statuses = [
        client.get("/static/dist/assets/test-asset-abc123.js").status_code
        for _ in range(6)
    ]
    assert statuses == [200] * 6
    assert (
        "Set-Cookie"
        not in client.get("/static/dist/assets/test-asset-abc123.js").headers
    )
    # the limit still applies to everything else
    assert [
        client.get("/me", headers={"Accept": "application/json"}).status_code
        for _ in range(3)
    ][-1] == 429


def test_static_files_cannot_be_used_to_read_outside_the_folder(redis):
    client = app_for(redis).test_client()
    for path in (
        "/static/../config.py",
        "/static/%2e%2e/config.py",
        "/static/..%2fconfig.py",
    ):
        assert client.get(path).status_code == 404


# --- the shared layout -------------------------------------------------------------------


def test_every_signed_in_page_shows_the_app_header_with_the_right_navigation(redis):
    """The header and nav come from one base template, driven by the session, so
    no page can forget to pass the user (monitoring once did)."""
    from tests.support import PERSONAS, login_as

    app = create_app(make_settings(), redis_client=redis, database=None)
    app.config.update(TESTING=True)
    expectations = {
        "employee": ("/", ["Ask", "Tickets"], ["Knowledge", "Signals"]),
        "kb_admin": ("/admin/documents", ["Ask", "Tickets", "Knowledge"], ["Signals"]),
        "auditor": ("/monitoring", ["Ask", "Tickets", "Signals"], ["Knowledge"]),
    }
    for persona, (path, present, absent) in expectations.items():
        client = app.test_client()
        login_as(client, PERSONAS[persona])
        html = client.get(path, headers={"Accept": "text/html"}).get_data(as_text=True)
        assert 'id="signout-button"' in html, (persona, path)
        assert "{Sign in}" not in html, (persona, path)
        for label in present:
            assert f">{label}</a>" in html or f">{label}</a" in html, (persona, label)
        for label in absent:
            assert f">{label}</a>" not in html, (persona, label)


def test_signed_out_visitors_get_the_public_header(redis):
    client = app_for(redis).test_client()
    html = client.get("/welcome", headers={"Accept": "text/html"}).get_data(
        as_text=True
    )
    assert "{Sign in}" in html and 'id="signout-button"' not in html
