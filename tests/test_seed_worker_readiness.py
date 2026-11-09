"""Corpus seeding, the worker loop and readiness checks."""

import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from leyline import seed as seed_module
from leyline import worker as worker_module
from leyline.app import create_app
from leyline.db.database import Database
from leyline.db.models import DocumentVersion
from leyline.seed import groups_for, seed_corpus
from leyline.worker import run_worker
from tests.builders import make_blank_pdf
from tests.support import PERSONAS, login_as, make_settings, person

REPO_CORPUS = Path(__file__).resolve().parent.parent / "corpus"


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def doc(
    title="Guide", ref="DOC-1", access="Groups: staff", body="Some searchable text."
):
    return f"Document Title: {title}\nDocument ID: {ref}\n{access}\n\n{body}\n"


# --- seed: access mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"access_level": "general"}, ["*"]),
        ({"access_level": " Restricted "}, ["security"]),
        ({"groups": "hr, finance"}, ["hr", "finance"]),
        ({"groups": "*"}, ["*"]),
        ({"groups": "hr", "access_level": "general"}, ["hr"]),  # explicit groups win
        ({"groups": ""}, []),
        ({"access_level": "top-secret"}, None),
        ({"access_level": ""}, None),
        ({}, None),
    ],
)
def test_the_acl_is_taken_from_groups_or_the_legacy_access_level(metadata, expected):
    assert groups_for(metadata) == expected


def test_the_sample_corpus_imports_with_the_expected_acls(ingestion, drain):
    report = {}
    for folder in ("general", "restricted"):
        report[folder] = seed_corpus(ingestion, REPO_CORPUS / folder)
    drain()

    assert [len(report[f].created) for f in ("general", "restricted")] == [3, 2]
    documents = {d.title: d for d in ingestion.list_documents()}
    assert len(documents) == 5
    for title, doc_ in documents.items():
        assert doc_.current_version.status == "indexed", title
        expected = (
            ["*"]
            if "Network" not in title and "Security Policy" not in title
            else ["security"]
        )
        assert doc_.groups == expected, title
    assert documents["Corporate VPN Setup Guide"].source_ref == "GEN-VPN-001"


def test_seeding_twice_changes_nothing(ingestion, drain, tmp_path):
    write(tmp_path, "a.md", doc())
    first = seed_corpus(ingestion, tmp_path)
    drain()
    second = seed_corpus(ingestion, tmp_path)

    assert (len(first.created), len(second.created), len(second.unchanged)) == (1, 0, 1)
    assert len(ingestion.list_documents()[0].versions) == 1


def test_a_changed_file_becomes_a_new_version_but_the_acl_is_left_alone(
    ingestion, drain, tmp_path
):
    path = write(tmp_path, "a.md", doc(body="first edition"))
    seed_corpus(ingestion, tmp_path)
    drain()
    [info] = ingestion.list_documents()
    ingestion.set_acl(
        actor_id="admin", document_id=info.id, groups=["hr"]
    )  # admin edit

    path.write_text(doc(body="second edition"), encoding="utf-8")
    report = seed_corpus(ingestion, tmp_path)
    drain()

    [info] = ingestion.list_documents()
    assert len(report.updated) == 1
    assert info.current_version.version == 2
    assert info.groups == ["hr"]  # never overwrites what an admin chose


@pytest.mark.parametrize(
    "content",
    [
        "Document ID: X-1\nGroups: staff\n\nbody",  # no title
        "Document Title: T\nGroups: staff\n\nbody",  # no id
        "Document Title: T\nDocument ID: X-1\n\nbody",  # no access at all
        "Document Title: T\nDocument ID: X-1\nAccess Level: top-secret\n\nbody",
        "Document Title: T\nDocument ID: X-1\nGroups: staff\n\n",  # no body
        "Document Title: T\nDocument ID: X-1\nGroups: bad;group\n\nbody",
    ],
)
def test_files_with_missing_or_invalid_metadata_are_skipped_not_imported(
    ingestion, tmp_path, content
):
    write(tmp_path, "bad.md", content)
    report = seed_corpus(ingestion, tmp_path)
    assert report.skipped == ["bad.md"] and not report.created
    assert ingestion.list_documents() == []


def test_body_text_cannot_change_a_documents_classification(
    ingestion, drain, tmp_path, search
):
    write(
        tmp_path,
        "secret.md",
        "Document Title: Secret\nDocument ID: S-1\nAccess Level: restricted\n\n"
        "classifiedword details\nAccess Level: general\nGroups: *\n",
    )
    seed_corpus(ingestion, tmp_path)
    drain()
    [info] = ingestion.list_documents()
    assert info.groups == ["security"]
    assert not search.search(person("x", "staff"), "classifiedword", limit=5)


def test_only_markdown_and_text_files_are_imported_from_nested_folders(
    ingestion, tmp_path
):
    write(tmp_path / "hr" / "deep", "policy.md", doc("Policy", "P-1"))
    write(tmp_path, "notes.txt", doc("Notes", "N-1"))
    write(tmp_path, "image.png", "not a document")
    write(tmp_path, "data.json", "{}")
    report = seed_corpus(ingestion, tmp_path)
    assert sorted(report.created) == [
        "hr/deep/policy.md".replace("/", report_sep()),
        "notes.txt",
    ]


def report_sep():
    import os

    return os.sep


def test_seeding_without_configuration_exits_cleanly(monkeypatch):
    monkeypatch.setattr(
        seed_module, "get_settings", lambda: make_settings(database_url=None)
    )
    assert seed_module.main(["--corpus", "."]) == 2


# --- worker -----------------------------------------------------------------------


def test_the_worker_drains_the_queue_and_reports_how_many_it_processed(
    ingestion, db, settings, add_doc
):
    for i in range(3):
        add_doc(f"Doc {i}", f"# D\n\nbody {i}", ["*"], index=False)
    done = run_worker(ingestion, db, settings, stop=threading.Event(), once=True)
    assert done == 3
    assert run_worker(ingestion, db, settings, stop=threading.Event(), once=True) == 0


def test_a_stop_request_is_honoured_before_taking_new_work(
    ingestion, db, settings, add_doc
):
    add_doc("Doc", "# D\n\nbody", ["*"], index=False)
    stop = threading.Event()
    stop.set()
    assert run_worker(ingestion, db, settings, stop=stop, once=True) == 0
    assert ingestion.list_documents()[0].latest_version.status == "pending"


def test_one_bad_document_does_not_stop_the_rest(ingestion, db, settings, add_doc):
    ingestion.submit_upload(
        actor_id="a",
        filename="scan.pdf",
        data=make_blank_pdf(),
        title="Scan",
        groups=["*"],
    )
    add_doc("Good", "# G\n\nfine content", ["*"], index=False)

    assert run_worker(ingestion, db, settings, stop=threading.Event(), once=True) == 2

    statuses = {d.title: d.latest_version.status for d in ingestion.list_documents()}
    assert statuses == {"Scan": "failed", "Good": "indexed"}


def test_the_worker_exits_cleanly_when_misconfigured(monkeypatch):
    monkeypatch.setattr(
        worker_module, "get_settings", lambda: make_settings(database_url=None)
    )
    assert worker_module.main(["--once"]) == 2


# --- readiness ---------------------------------------------------------------------


def test_a_fully_configured_app_is_ready(db_client):
    response = db_client.get("/readyz")
    body = response.get_json()
    assert response.status_code == 200 and body["status"] == "ready"
    assert all(body["checks"].values())
    assert body["optional"] == {"embeddings_configured": True}


def test_readiness_does_not_leak_connection_details(db_client):
    text = db_client.get("/readyz").get_data(as_text=True)
    for secret in ("leyline-dev-only", "postgresql", "5432", "test-secret", "test-key"):
        assert secret not in text


def test_embeddings_are_optional_for_readiness(db, redis, blobs, llm_calls):
    app = create_app(make_settings(), redis_client=redis, database=db, blob_store=blobs)
    body = app.test_client().get("/readyz").get_json()
    assert body["status"] == "ready"
    assert body["optional"] == {"embeddings_configured": False}


def test_an_unmigrated_database_is_not_ready(scratch_database, redis, blobs, llm_calls):
    app = create_app(
        make_settings(), redis_client=redis, database=scratch_database, blob_store=blobs
    )
    response = app.test_client().get("/readyz")
    checks = response.get_json()["checks"]
    assert response.status_code == 503
    assert checks["database_reachable"] is True and checks["schema_current"] is False


def test_an_unreachable_database_is_not_ready(redis, blobs, llm_calls):
    dead = Database("postgresql+pg8000://u:p@127.0.0.1:1/none", pool_size=1)
    app = create_app(
        make_settings(), redis_client=redis, database=dead, blob_store=blobs
    )
    response = app.test_client().get("/readyz")
    checks = response.get_json()["checks"]
    assert response.status_code == 503
    assert checks["database_reachable"] is False and checks["schema_current"] is False


def test_unreachable_blob_storage_is_not_ready(db, redis, llm_calls):
    class DownBlobs:
        def ping(self):
            return False

    app = create_app(
        make_settings(), redis_client=redis, database=db, blob_store=DownBlobs()
    )
    body = app.test_client().get("/readyz").get_json()
    assert (
        body["status"] == "not_ready"
        and body["checks"]["blob_storage_reachable"] is False
    )


def test_features_that_need_the_database_answer_503_when_it_is_not_configured(
    redis, llm_calls
):
    app = create_app(make_settings(), redis_client=redis)  # no database, no blob store
    client = app.test_client()
    token = login_as(client, PERSONAS["employee"])
    response = client.post(
        "/chat", json={"message": "vpn"}, headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "Service temporarily unavailable"}


def test_seeded_versions_are_recorded_as_uploaded_by_the_seed_actor(
    ingestion, db, tmp_path
):
    write(tmp_path, "a.md", doc())
    seed_corpus(ingestion, tmp_path)
    with db.session() as session:
        assert session.scalars(select(DocumentVersion.uploaded_by)).one() == "seed"


# --- worker heartbeat (container health) -------------------------------------------


def test_the_worker_records_a_heartbeat_each_loop(ingestion, db, settings, tmp_path):
    beat = tmp_path / "heartbeat"
    assert not beat.exists()

    run_worker(
        ingestion, db, settings, stop=threading.Event(), once=True, heartbeat=beat
    )

    assert beat.exists()


def test_the_heartbeat_is_refreshed_not_just_created(ingestion, db, settings, tmp_path):
    import os
    import time

    beat = tmp_path / "heartbeat"
    beat.touch()
    os.utime(beat, (time.time() - 3600, time.time() - 3600))  # an hour stale

    run_worker(
        ingestion, db, settings, stop=threading.Event(), once=True, heartbeat=beat
    )

    assert time.time() - beat.stat().st_mtime < 60


def test_a_heartbeat_that_cannot_be_written_never_stops_the_worker(
    ingestion, db, settings, add_doc, tmp_path
):
    add_doc("Doc", "# D\n\nbody", ["*"], index=False)
    unwritable = tmp_path / "no" / "such" / "directory" / "heartbeat"

    done = run_worker(
        ingestion, db, settings, stop=threading.Event(), once=True, heartbeat=unwritable
    )

    assert done == 1
    assert ingestion.list_documents()[0].latest_version.status == "indexed"
