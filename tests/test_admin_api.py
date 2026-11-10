"""The knowledge-base admin API over HTTP, against a real Postgres."""

import io
import json
import re
import uuid

import pytest
from sqlalchemy import select

from leyline.app import create_app
from leyline.db.models import AuditEvent
from tests.builders import make_pdf
from tests.support import CSRF_HEADER, PERSONAS, login_as, make_settings

GUIDE = b"# VPN\n\n## Setup\nInstall the zebracrossing client."
JSON = {"Accept": "application/json"}


@pytest.fixture
def admin(db_client):
    token = login_as(db_client, PERSONAS["kb_admin"])
    db_client.csrf = token
    return db_client


def upload(
    client,
    *,
    data=GUIDE,
    filename="guide.md",
    title="VPN guide",
    groups="staff",
    **extra,
):
    form = {"file": (io.BytesIO(data), filename), "title": title, "groups": groups}
    form.update(extra)
    return client.post(
        "/api/admin/documents",
        data=form,
        content_type="multipart/form-data",
        headers={CSRF_HEADER: client.csrf, **JSON},
    )


def listing(client):
    return client.get("/api/admin/documents", headers=JSON).get_json()["documents"]


# --- upload -------------------------------------------------------------------


def test_uploading_a_document_queues_it_then_the_worker_indexes_it(admin, drain):
    response = upload(admin, groups="staff, hr")
    body = response.get_json()
    assert response.status_code == 202
    assert body["version"] == 1 and body["duplicate"] is False
    uuid.UUID(body["document_id"])  # a real id

    [doc] = listing(admin)
    assert doc["title"] == "VPN guide"
    assert doc["groups"] == ["hr", "staff"]
    assert doc["latest_version"]["status"] == "pending"

    drain()

    [doc] = listing(admin)
    assert doc["latest_version"]["status"] == "indexed"
    assert doc["current_version"]["chunk_count"] == 1
    assert doc["needs_reindex"] is False


def test_the_same_file_again_is_acknowledged_without_a_new_version(admin, drain):
    document_id = upload(admin).get_json()["document_id"]
    drain()
    again = admin.post(
        f"/api/admin/documents/{document_id}/versions",
        data={"file": (io.BytesIO(GUIDE), "guide.md")},
        content_type="multipart/form-data",
        headers={CSRF_HEADER: admin.csrf},
    )
    assert again.status_code == 200
    assert again.get_json()["duplicate"] is True
    assert len(listing(admin)[0]["versions"]) == 1


def test_a_new_version_can_be_uploaded_and_retitled(admin, drain):
    document_id = upload(admin).get_json()["document_id"]
    drain()
    response = admin.post(
        f"/api/admin/documents/{document_id}/versions",
        data={
            "file": (io.BytesIO(b"# VPN\n\nnewcontent"), "v2.md"),
            "title": "VPN guide v2",
        },
        content_type="multipart/form-data",
        headers={CSRF_HEADER: admin.csrf},
    )
    assert response.status_code == 202 and response.get_json()["version"] == 2
    drain()
    [doc] = listing(admin)
    assert doc["title"] == "VPN guide v2"
    assert doc["current_version"]["version"] == 2
    assert doc["groups"] == ["staff"]  # a new version never changes who can read it


def test_pdf_uploads_work_end_to_end(admin, drain):
    response = upload(
        admin,
        data=make_pdf(["Quarterly narwhalbudget summary."]),
        filename="budget.pdf",
        title="Budget",
    )
    assert response.status_code == 202
    drain()
    assert listing(admin)[0]["current_version"]["chunk_count"] == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"filename": "evil.exe", "data": b"MZ"}, "Unsupported"),
        ({"filename": "fake.pdf", "data": b"not a pdf at all"}, "not a valid PDF"),
        ({"filename": "fake.docx", "data": b"not a zip"}, "not a valid DOCX"),
        ({"filename": "empty.txt", "data": b""}, "empty"),
        ({"data": b"binary\x00data", "filename": "x.txt"}, "binary"),
        ({"title": "   "}, "title is required"),
        ({"groups": "ok, bad;group"}, "Invalid group"),
        ({"groups": ",".join(f"g{i}" for i in range(40))}, "At most"),
    ],
)
def test_bad_uploads_are_rejected_with_a_clear_message_and_store_nothing(
    admin, kwargs, message
):
    response = upload(admin, **kwargs)
    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert listing(admin) == []


def test_a_request_without_a_file_is_rejected(admin):
    response = admin.post(
        "/api/admin/documents",
        data={"title": "x", "groups": "staff"},
        content_type="multipart/form-data",
        headers={CSRF_HEADER: admin.csrf},
    )
    assert response.status_code == 400
    assert "Choose a file" in response.get_json()["error"]


def test_files_over_the_upload_limit_are_rejected(
    db, redis, blobs, embedder, llm_calls
):
    app = create_app(
        make_settings(max_upload_bytes=2048),
        redis_client=redis,
        database=db,
        blob_store=blobs,
        embedder=embedder,
    )
    client = app.test_client()
    client.csrf = login_as(client, PERSONAS["kb_admin"])

    just_over = upload(client, data=b"x" * 2049, filename="big.txt")
    assert just_over.status_code in (400, 413)
    assert upload(client, data=b"x" * 2000, filename="ok.txt").status_code == 202
    way_over = upload(client, data=b"x" * (3 * 1024 * 1024), filename="huge.txt")
    assert way_over.status_code == 413  # refused by the web layer before parsing
    assert len(listing(client)) == 1


def test_the_small_global_body_limit_still_protects_json_endpoints(admin):
    document_id = upload(admin).get_json()["document_id"]
    response = admin.put(
        f"/api/admin/documents/{document_id}/acl",
        data=json.dumps({"groups": ["x" * 30_000]}),
        content_type="application/json",
        headers={CSRF_HEADER: admin.csrf},
    )
    assert response.status_code == 413


# --- authorization ------------------------------------------------------------------


@pytest.mark.parametrize(
    "persona", ["employee", "security", "auditor", "support_agent"]
)
def test_only_kb_admins_may_use_the_admin_api(db_client, persona):
    token = login_as(db_client, PERSONAS[persona])
    db_client.csrf = token
    assert upload(db_client).status_code == 403
    assert db_client.get("/api/admin/documents", headers=JSON).status_code == 403
    assert (
        db_client.get("/admin/documents", headers={"Accept": "text/html"}).status_code
        == 403
    )


def test_anonymous_callers_get_401(db_client):
    db_client.csrf = ""
    assert upload(db_client).status_code == 401


def test_uploads_need_a_csrf_token_and_are_not_even_parsed_without_one(admin):
    response = admin.post(
        "/api/admin/documents",
        data={"file": (io.BytesIO(GUIDE), "guide.md"), "title": "t"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    assert listing(admin) == []


def test_a_cross_site_upload_is_rejected_even_with_a_valid_token(admin):
    response = admin.post(
        "/api/admin/documents",
        data={"file": (io.BytesIO(GUIDE), "guide.md"), "title": "t"},
        content_type="multipart/form-data",
        headers={CSRF_HEADER: admin.csrf, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403


# --- acl, reindex, delete ------------------------------------------------------------


def test_the_acl_can_be_replaced_and_the_change_is_audited(admin, db):
    document_id = upload(admin, groups="staff").get_json()["document_id"]

    response = admin.put(
        f"/api/admin/documents/{document_id}/acl",
        json={"groups": ["hr", "*"]},
        headers={CSRF_HEADER: admin.csrf},
    )

    assert response.status_code == 200 and response.get_json() == {
        "groups": ["*", "hr"]
    }
    assert listing(admin)[0]["groups"] == ["*", "hr"]
    with db.session() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "document.acl_changed")
        ).one()
    assert event.actor_id == PERSONAS["kb_admin"].user_id
    assert event.details == {"before": ["staff"], "after": ["*", "hr"]}


@pytest.mark.parametrize(
    "body",
    [{}, {"groups": "staff"}, {"groups": [1, 2]}, {"groups": ["bad;group"]}, [], "x"],
)
def test_malformed_acl_requests_are_rejected(admin, body):
    document_id = upload(admin, groups="staff").get_json()["document_id"]
    response = admin.put(
        f"/api/admin/documents/{document_id}/acl",
        json=body,
        headers={CSRF_HEADER: admin.csrf},
    )
    assert response.status_code == 400
    assert listing(admin)[0]["groups"] == ["staff"]


def test_reindex_is_queued_once(admin, drain):
    document_id = upload(admin).get_json()["document_id"]
    drain()
    first = admin.post(
        f"/api/admin/documents/{document_id}/reindex", headers={CSRF_HEADER: admin.csrf}
    )
    second = admin.post(
        f"/api/admin/documents/{document_id}/reindex", headers={CSRF_HEADER: admin.csrf}
    )
    assert (first.status_code, first.get_json()) == (202, {"queued": True})
    assert (second.status_code, second.get_json()) == (200, {"queued": False})


def test_deleting_removes_the_document_from_the_list_and_detail(admin, drain):
    document_id = upload(admin).get_json()["document_id"]
    drain()

    response = admin.delete(
        f"/api/admin/documents/{document_id}", headers={CSRF_HEADER: admin.csrf}
    )

    assert response.status_code == 204 and response.data == b""
    assert listing(admin) == []
    assert (
        admin.get(f"/api/admin/documents/{document_id}", headers=JSON).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("delete", ""), ("post", "/reindex")],
)
def test_unknown_documents_are_404(admin, method, suffix):
    response = getattr(admin, method)(
        f"/api/admin/documents/{uuid.uuid4()}{suffix}",
        headers={CSRF_HEADER: admin.csrf, **JSON},
    )
    assert response.status_code == 404


def test_a_malformed_document_id_is_404_not_a_server_error(admin):
    assert admin.get("/api/admin/documents/not-a-uuid", headers=JSON).status_code == 404


# --- what the admin API never reveals --------------------------------------------------


def test_no_admin_response_contains_document_text_or_storage_keys(admin, drain):
    document_id = upload(admin).get_json()["document_id"]
    drain()
    bodies = [
        admin.get("/api/admin/documents", headers=JSON).get_data(as_text=True),
        admin.get(f"/api/admin/documents/{document_id}", headers=JSON).get_data(
            as_text=True
        ),
        admin.get("/admin/documents", headers={"Accept": "text/html"}).get_data(
            as_text=True
        ),
    ]
    for body in bodies:
        assert "zebracrossing" not in body  # document content
        assert "blob_key" not in body and "documents/" + document_id not in body


def test_hostile_titles_and_filenames_come_back_as_plain_json_strings(admin):
    title = '<img src=x onerror="alert(1)">'
    upload(admin, title=title, filename="<img onerror=alert(1)>.md")
    response = admin.get("/api/admin/documents", headers=JSON)
    assert response.mimetype == "application/json"
    [doc] = response.get_json()["documents"]
    assert doc["title"] == title
    assert doc["latest_version"]["filename"] == "<img onerror=alert(1)>.md"


def test_the_admin_page_has_no_inline_code_and_loads_its_assets_from_static(admin):
    html = admin.get("/admin/documents", headers={"Accept": "text/html"}).get_data(
        as_text=True
    )
    assert not re.search(r"<script(?![^>]*src=)[^>]*>", html)  # no inline script
    assert "<style" not in html and " style=" not in html
    assert " onclick=" not in html and " onsubmit=" not in html
    assert "innerHTML" not in html  # titles and filenames are rendered via textContent
    assert "Knowledge" in html and 'id="documents"' in html


def test_every_admin_action_is_audited_with_the_actor(admin, db, drain):
    document_id = upload(admin).get_json()["document_id"]
    drain()
    admin.put(
        f"/api/admin/documents/{document_id}/acl",
        json={"groups": ["hr"]},
        headers={CSRF_HEADER: admin.csrf},
    )
    admin.post(
        f"/api/admin/documents/{document_id}/reindex", headers={CSRF_HEADER: admin.csrf}
    )
    admin.delete(
        f"/api/admin/documents/{document_id}", headers={CSRF_HEADER: admin.csrf}
    )

    with db.session() as session:
        events = session.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    assert [e.action for e in events] == [
        "document.upload",
        "document.acl_changed",
        "document.reindex",
        "document.delete",
    ]
    assert {e.actor_id for e in events} == {PERSONAS["kb_admin"].user_id}
    assert {e.target_id for e in events} == {document_id}
