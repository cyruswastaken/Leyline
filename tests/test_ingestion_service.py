"""The document lifecycle against a real Postgres: upload, index, re-version,
retry, lease takeover, delete, re-index and concurrent workers."""

import threading
import uuid

import pytest
from sqlalchemy import select, text, update

from leyline.db.models import (
    AuditEvent,
    Chunk,
    Document,
    DocumentAcl,
    DocumentVersion,
    IngestionJob,
)
from leyline.ingestion import jobs
from leyline.ingestion.parsers import InvalidDocumentError
from leyline.ingestion.service import (
    DocumentNotFoundError,
    DocumentStateError,
    IngestionService,
)
from leyline.worker import run_worker
from tests.builders import make_blank_pdf, make_pdf
from tests.support import FakeEmbedder, make_settings, person

READER = person("reader", "staff")

GUIDE = """# VPN guide

## Setup
Install the zebracrossing client and sign in with your corporate account.

## Troubleshooting
Restart the client if the tunnel drops.
"""


def make_service(db, blobs, embedder=None, **settings):
    return IngestionService(db, blobs, embedder, make_settings(**settings))


def drain_with(service, db, settings=None):
    return run_worker(
        service,
        db,
        settings or make_settings(),
        stop=threading.Event(),
        once=True,
    )


def rows(db, model, *where):
    with db.session() as session:
        return list(session.scalars(select(model).where(*where)))


def version_of(db, document_id, number=1):
    return rows(
        db,
        DocumentVersion,
        DocumentVersion.document_id == document_id,
        DocumentVersion.version == number,
    )[0]


def upload(service, text_=GUIDE, *, title="VPN guide", groups=("*",), **kwargs):
    return service.submit_upload(
        actor_id="admin-1",
        filename=kwargs.pop("filename", "guide.md"),
        data=text_.encode() if isinstance(text_, str) else text_,
        title=title,
        groups=list(groups),
        **kwargs,
    )


def found(search, query, principal=READER):
    return {hit.title for hit in search.search(principal, query, limit=20)}


# --- the happy path ----------------------------------------------------------


def test_an_uploaded_document_becomes_searchable_after_the_worker_runs(
    ingestion, db, drain, search
):
    result = upload(ingestion)
    assert found(search, "zebracrossing") == set()  # queued, not indexed yet

    assert drain() == 1

    assert found(search, "zebracrossing") == {"VPN guide"}
    version = version_of(db, result.document_id)
    assert version.status == "indexed"
    assert version.chunk_count == 2
    assert version.indexed_at is not None
    assert version.embedding_model == "fake-embed-1"
    assert version.embedding_dimensions == 768
    assert rows(db, IngestionJob)[0].state == "succeeded"


def test_chunks_carry_headings_and_vectors(ingestion, db, drain):
    document_id = upload(ingestion).document_id
    drain()
    chunks = sorted(
        rows(db, Chunk, Chunk.document_id == document_id), key=lambda c: c.chunk_index
    )
    assert [c.heading for c in chunks] == [
        "VPN guide > Setup",
        "VPN guide > Troubleshooting",
    ]
    assert all(c.embedding is not None and len(c.embedding) == 768 for c in chunks)


def test_without_an_embedder_documents_are_indexed_for_keyword_search_only(
    db, blobs, search
):
    service = make_service(db, blobs, embedder=None)
    document_id = upload(service).document_id
    drain_with(service, db)

    assert found(search, "zebracrossing") == {"VPN guide"}
    version = version_of(db, document_id)
    assert version.embedding_model is None and version.embedding_dimensions is None
    assert all(c.embedding is None for c in rows(db, Chunk))


def test_the_original_file_is_stored_under_a_key_that_never_contains_the_filename(
    ingestion, db, blobs
):
    upload(ingestion, filename="../../etc/passwd.md")
    version = rows(db, DocumentVersion)[0]
    assert version.filename == "passwd.md"
    assert "passwd" not in version.blob_key and ".." not in version.blob_key
    assert blobs.get(version.blob_key) == GUIDE.encode()


def test_acl_and_audit_are_recorded_with_the_upload(ingestion, db):
    result = upload(ingestion, groups=["hr", "finance"])
    acl = {
        a.group_id
        for a in rows(db, DocumentAcl, DocumentAcl.document_id == result.document_id)
    }
    assert acl == {"hr", "finance"}
    event = rows(db, AuditEvent)[0]
    assert (event.actor_id, event.action) == ("admin-1", "document.upload")
    assert event.target_id == str(result.document_id)
    assert event.details["groups"] == ["finance", "hr"]


def test_a_new_document_requires_a_title_and_a_supported_file(ingestion):
    with pytest.raises(InvalidDocumentError, match="title"):
        upload(ingestion, title="  ")
    with pytest.raises(InvalidDocumentError, match="Unsupported"):
        upload(ingestion, filename="evil.exe")
    with pytest.raises(InvalidDocumentError, match="Invalid group"):
        upload(ingestion, groups=["bad;group"])


def test_failed_validation_leaves_nothing_behind(ingestion, db, blobs):
    with pytest.raises(InvalidDocumentError):
        upload(ingestion, filename="evil.exe")
    assert (
        rows(db, Document) == []
        and rows(db, IngestionJob) == []
        and blobs.keys() == set()
    )


# --- versions ---------------------------------------------------------------


def test_uploading_identical_content_again_is_a_no_op(ingestion, db, drain, blobs):
    first = upload(ingestion)
    drain()
    blob_count = len(blobs.keys())

    again = ingestion.submit_upload(
        actor_id="admin-1",
        filename="guide-copy.md",
        data=GUIDE.encode(),
        document_id=first.document_id,
    )

    assert again.duplicate is True and again.version == 1
    assert len(rows(db, DocumentVersion)) == 1
    assert len(blobs.keys()) == blob_count
    assert len(rows(db, IngestionJob)) == 1


def test_a_new_version_replaces_the_old_one_only_once_it_is_indexed(
    ingestion, db, drain, search
):
    first = upload(ingestion)
    drain()

    second = ingestion.submit_upload(
        actor_id="admin-2",
        filename="guide.md",
        data=b"# VPN guide\n\n## Setup\nUse the quokkaclient instead.",
        document_id=first.document_id,
    )
    assert second.version == 2 and not second.duplicate

    # Before the worker runs, the old version keeps serving.
    assert found(search, "zebracrossing") == {"VPN guide"}
    assert found(search, "quokkaclient") == set()

    drain()

    assert found(search, "zebracrossing") == set()  # old content is gone
    assert found(search, "quokkaclient") == {"VPN guide"}
    assert version_of(db, first.document_id, 1).status == "superseded"
    assert version_of(db, first.document_id, 2).status == "indexed"
    with db.session() as session:
        document = session.get(Document, first.document_id)
        assert document.current_version_id == version_of(db, first.document_id, 2).id
    chunk_versions = {c.version_id for c in rows(db, Chunk)}
    assert chunk_versions == {version_of(db, first.document_id, 2).id}


def test_a_failing_new_version_never_disturbs_the_live_one(db, blobs, search):
    good = make_service(db, blobs, FakeEmbedder())
    document_id = upload(good).document_id
    drain_with(good, db)

    broken = make_service(db, blobs, FakeEmbedder(fail_times=99), ingest_max_attempts=2)
    broken.submit_upload(
        actor_id="a",
        filename="g.md",
        data=b"# Guide\n\nbrand new platypusword content",
        document_id=document_id,
    )
    drain_with(broken, db, make_settings(ingest_max_attempts=2))

    assert found(search, "zebracrossing") == {"VPN guide"}  # still the old text
    assert found(search, "platypusword") == set()
    assert version_of(db, document_id, 1).status == "indexed"
    assert version_of(db, document_id, 2).status == "failed"


def claim_job_for(db, document_id, version_number):
    """Claim one specific version's job, as if its worker had picked it up."""
    token = uuid.uuid4().hex
    with db.session() as session:
        version = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.version == version_number,
            )
        ).one()
        job = session.scalars(
            select(IngestionJob).where(IngestionJob.version_id == version.id)
        ).one()
        job.state, job.locked_by = "running", token
        job.attempts += 1
        return jobs.ClaimedJob(
            job.id,
            job.kind,
            job.document_id,
            job.version_id,
            job.attempts,
            job.max_attempts,
            token,
        )


def test_a_slow_older_version_cannot_overwrite_a_newer_one(ingestion, db, search):
    first = upload(ingestion)
    for body in (b"# G\n\nsecond edition", b"# G\n\nthird edition"):
        ingestion.submit_upload(
            actor_id="a", filename="g.md", data=body, document_id=first.document_id
        )

    # Workers finish out of order: v3, then v2, then v1.
    outcomes = [
        ingestion.run_job(claim_job_for(db, first.document_id, number))
        for number in (3, 2, 1)
    ]

    assert outcomes == ["indexed", "superseded", "superseded"]
    assert found(search, "third") == {"VPN guide"}
    assert found(search, "second") == set()
    assert found(search, "zebracrossing") == set()
    assert version_of(db, first.document_id, 3).status == "indexed"
    assert version_of(db, first.document_id, 2).status == "superseded"
    assert version_of(db, first.document_id, 1).status == "superseded"
    assert len(rows(db, Chunk)) == 1


def test_uploading_a_version_to_an_unknown_or_deleted_document_fails(ingestion):
    with pytest.raises(DocumentNotFoundError):
        ingestion.submit_upload(
            actor_id="a", filename="g.md", data=b"hello", document_id=uuid.uuid4()
        )
    document_id = upload(ingestion).document_id
    ingestion.delete_document(actor_id="a", document_id=document_id)
    with pytest.raises(DocumentNotFoundError):
        ingestion.submit_upload(
            actor_id="a", filename="g.md", data=b"hello", document_id=document_id
        )


# --- failures and retries -----------------------------------------------------


def test_an_unusable_file_fails_permanently_without_retrying(db, blobs, search):
    service = make_service(db, blobs, FakeEmbedder())
    result = upload(service, make_blank_pdf(), filename="scan.pdf", title="Scanned")
    drain_with(service, db)

    job = rows(db, IngestionJob)[0]
    version = version_of(db, result.document_id)
    assert (job.state, job.attempts) == ("failed", 1)  # no pointless retries
    assert version.status == "failed" and "scanned" in version.error.lower()
    assert found(search, "anything") == set()
    with db.session() as session:
        assert session.get(Document, result.document_id).current_version_id is None


def test_transient_failures_are_retried_until_they_succeed(db, blobs, search):
    flaky = FakeEmbedder(fail_times=2)
    service = make_service(db, blobs, flaky)
    upload(service)

    processed = drain_with(service, db)

    assert processed == 3  # failed, failed, succeeded
    job = rows(db, IngestionJob)[0]
    assert (job.state, job.attempts) == ("succeeded", 3)
    assert found(search, "zebracrossing") == {"VPN guide"}


def test_retries_stop_after_the_attempt_limit(db, blobs, search):
    service = make_service(
        db, blobs, FakeEmbedder(fail_times=99), ingest_max_attempts=3
    )
    result = upload(service)
    drain_with(service, db, make_settings(ingest_max_attempts=3))

    job = rows(db, IngestionJob)[0]
    version = version_of(db, result.document_id)
    assert (job.state, job.attempts, job.max_attempts) == ("failed", 3, 3)
    assert "provider unavailable" in job.last_error
    assert version.status == "failed" and "provider unavailable" in version.error
    assert found(search, "zebracrossing") == set()


def test_a_failed_job_waits_before_the_next_attempt(db, blobs):
    settings = make_settings(ingest_backoff_seconds=60)
    service = IngestionService(db, blobs, FakeEmbedder(fail_times=1), settings)
    upload(service)

    assert drain_with(service, db, settings) == 1  # one failure, then nothing runnable

    job = rows(db, IngestionJob)[0]
    assert (job.state, job.attempts) == ("queued", 1)
    with db.session() as session:
        wait = session.execute(
            text("SELECT extract(epoch FROM (run_after - now())) FROM ingestion_jobs")
        ).scalar()
    assert 50 < wait <= 60
    assert version_of(db, job.document_id).status == "pending"


def test_backoff_doubles_each_attempt_up_to_a_cap(db, blobs):
    service = make_service(db, blobs, FakeEmbedder())
    upload(service)
    with db.session() as session:
        claimed = jobs.claim(session, lease_seconds=300)
    for attempts, expected in [(1, 30), (2, 60), (3, 120), (12, 3600)]:
        job = jobs.ClaimedJob(
            claimed.id,
            claimed.kind,
            claimed.document_id,
            claimed.version_id,
            attempts,
            99,
            claimed.token,
        )
        with db.session() as session:
            session.execute(
                update(IngestionJob).values(locked_by=job.token, state="running")
            )
            jobs.fail(session, job, error="x", retry=True, backoff_seconds=30)
        with db.session() as session:
            wait = session.execute(
                text(
                    "SELECT extract(epoch FROM (run_after - now())) FROM ingestion_jobs"
                )
            ).scalar()
        assert expected - 5 < wait <= expected, (attempts, wait)


def test_a_tampered_stored_file_is_rejected(ingestion, db, blobs, drain):
    result = upload(ingestion)
    key = version_of(db, result.document_id).blob_key
    blobs.put(key, b"# Something else entirely", "text/markdown")

    drain()

    job = rows(db, IngestionJob)[0]
    assert (job.state, job.attempts) == ("failed", 1)
    assert "integrity" in version_of(db, result.document_id).error


def test_a_missing_stored_file_fails_permanently(ingestion, db, blobs, drain):
    result = upload(ingestion)
    blobs.delete(version_of(db, result.document_id).blob_key)
    drain()
    assert "missing" in version_of(db, result.document_id).error
    assert rows(db, IngestionJob)[0].attempts == 1


# --- leases and fencing -------------------------------------------------------


def age_lock(db, minutes=10):
    with db.session() as session:
        session.execute(
            update(IngestionJob).values(
                locked_at=text(f"now() - interval '{minutes} minutes'")
            )
        )


def test_a_running_job_is_not_claimed_twice(ingestion, db):
    upload(ingestion)
    with db.session() as session:
        assert jobs.claim(session, lease_seconds=300) is not None
    with db.session() as session:
        assert jobs.claim(session, lease_seconds=300) is None


def test_a_crashed_workers_job_is_reclaimed_after_the_lease_expires(ingestion, db):
    upload(ingestion)
    with db.session() as session:
        first = jobs.claim(session, lease_seconds=300)
    age_lock(db)
    with db.session() as session:
        second = jobs.claim(session, lease_seconds=300)

    assert second is not None and second.id == first.id
    assert second.attempts == 2 and second.token != first.token


def test_a_worker_that_lost_its_lease_cannot_finish_the_job(ingestion, db):
    upload(ingestion)
    with db.session() as session:
        stale = jobs.claim(session, lease_seconds=300)
    age_lock(db)
    with db.session() as session:
        current = jobs.claim(session, lease_seconds=300)

    assert ingestion.run_job(stale) == "lost"
    with db.session() as session:
        assert jobs.succeed(session, stale) is False
        assert (
            jobs.fail(session, stale, error="x", retry=True, backoff_seconds=0)
            == "lost"
        )
    assert ingestion.run_job(current) == "indexed"  # the rightful owner still can


def test_a_job_that_keeps_killing_its_worker_is_eventually_given_up_on(ingestion, db):
    upload(ingestion)
    with db.session() as session:
        session.execute(
            update(IngestionJob).values(state="running", attempts=5, max_attempts=5)
        )
    age_lock(db)
    with db.session() as session:
        assert jobs.claim(session, lease_seconds=300) is None
    job = rows(db, IngestionJob)[0]
    assert job.state == "failed" and "gave up" in job.last_error


def test_concurrent_workers_process_every_job_exactly_once(db, blobs, search, add_doc):
    for i in range(12):
        add_doc(
            f"Doc {i}",
            f"# Doc {i}\n\ncontent number {i} sharedterm",
            ["*"],
            index=False,
        )
    settings = make_settings()
    service = IngestionService(db, blobs, FakeEmbedder(), settings)

    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                run_worker(service, db, settings, stop=threading.Event(), once=True)
            )
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sum(results) == 12
    all_jobs = rows(db, IngestionJob)
    assert {j.state for j in all_jobs} == {"succeeded"}
    assert {j.attempts for j in all_jobs} == {1}  # nobody did anyone else's work
    assert len(rows(db, Chunk)) == 12  # no duplicate chunks
    assert len(found(search, "sharedterm")) == 12


# --- ACL ----------------------------------------------------------------------


def test_replacing_an_acl_is_audited_with_before_and_after(ingestion, db):
    document_id = upload(ingestion, groups=["staff"]).document_id
    result = ingestion.set_acl(
        actor_id="admin-9", document_id=document_id, groups=["hr", "hr", " "]
    )

    assert result == {"hr"}
    acl = {
        a.group_id
        for a in rows(db, DocumentAcl, DocumentAcl.document_id == document_id)
    }
    assert acl == {"hr"}
    event = next(e for e in rows(db, AuditEvent) if e.action == "document.acl_changed")
    assert event.actor_id == "admin-9"
    assert event.details == {"before": ["staff"], "after": ["hr"]}


def test_an_invalid_acl_changes_nothing(ingestion, db):
    document_id = upload(ingestion, groups=["staff"]).document_id
    with pytest.raises(InvalidDocumentError):
        ingestion.set_acl(
            actor_id="a", document_id=document_id, groups=["ok", "bad;one"]
        )
    acl = {a.group_id for a in rows(db, DocumentAcl)}
    assert acl == {"staff"}


def test_acl_changes_on_unknown_documents_fail(ingestion):
    with pytest.raises(DocumentNotFoundError):
        ingestion.set_acl(actor_id="a", document_id=uuid.uuid4(), groups=["x"])


# --- delete ---------------------------------------------------------------------


def test_a_deleted_document_is_unsearchable_immediately_and_files_are_purged_later(
    ingestion, db, drain, blobs, search
):
    document_id = upload(ingestion).document_id
    drain()
    assert found(search, "zebracrossing") == {"VPN guide"}

    ingestion.delete_document(actor_id="admin-1", document_id=document_id)

    # no worker has run yet
    assert found(search, "zebracrossing") == set()
    assert rows(db, Chunk) == []
    assert ingestion.list_documents() == []
    assert blobs.keys() != set()  # files still there until the purge job runs

    drain()

    assert blobs.keys() == set()
    assert version_of(db, document_id).blob_deleted_at is not None
    with db.session() as session:
        document = session.get(Document, document_id)
        assert document.deleted_at is not None and document.current_version_id is None
    assert [e.action for e in rows(db, AuditEvent)][-1] == "document.delete"


def test_deleting_twice_or_deleting_unknown_documents_fails(ingestion):
    document_id = upload(ingestion).document_id
    ingestion.delete_document(actor_id="a", document_id=document_id)
    with pytest.raises(DocumentNotFoundError):
        ingestion.delete_document(actor_id="a", document_id=document_id)
    with pytest.raises(DocumentNotFoundError):
        ingestion.delete_document(actor_id="a", document_id=uuid.uuid4())


def test_deleting_cancels_queued_indexing(ingestion, db, drain, search):
    document_id = upload(ingestion).document_id
    ingestion.delete_document(actor_id="a", document_id=document_id)
    ingestion_jobs = {j.kind: j for j in rows(db, IngestionJob)}
    assert ingestion_jobs["ingest"].state == "failed"
    assert "deleted" in ingestion_jobs["ingest"].last_error

    drain()  # only the purge runs
    assert found(search, "zebracrossing") == set()


def test_a_job_running_while_its_document_is_deleted_cannot_bring_it_back(
    ingestion, db, search
):
    document_id = upload(ingestion).document_id
    with db.session() as session:
        claimed = jobs.claim(session, lease_seconds=300)  # worker has it...

    ingestion.delete_document(actor_id="a", document_id=document_id)  # ...admin deletes
    outcome = ingestion.run_job(claimed)  # ...worker finishes

    assert outcome == "skipped"
    assert rows(db, Chunk) == []
    assert found(search, "zebracrossing") == set()


def test_a_deleted_documents_external_id_can_be_reused(ingestion):
    first = upload(ingestion, source_ref="GEN-001")
    ingestion.delete_document(actor_id="a", document_id=first.document_id)
    again = upload(ingestion, source_ref="GEN-001")  # no unique-constraint clash
    assert again.document_id != first.document_id


# --- re-index and embedding model changes ---------------------------------------


def test_a_changed_embedding_model_is_detected_and_reindex_replaces_the_vectors(
    ingestion, db, drain, blobs, search
):
    document_id = upload(ingestion).document_id
    drain()
    old_chunk_ids = {c.id for c in rows(db, Chunk)}

    upgraded = make_service(db, blobs, FakeEmbedder("fake-embed-2"))
    [info] = upgraded.list_documents()
    assert info.needs_reindex is True  # indexed with fake-embed-1, now fake-embed-2
    assert ingestion.list_documents()[0].needs_reindex is False

    assert upgraded.reindex(actor_id="admin", document_id=document_id) is True
    assert found(search, "zebracrossing") == {"VPN guide"}  # old chunks still serve
    drain_with(upgraded, db)

    assert upgraded.list_documents()[0].needs_reindex is False
    assert version_of(db, document_id).embedding_model == "fake-embed-2"
    new_chunk_ids = {c.id for c in rows(db, Chunk)}
    assert new_chunk_ids and not (new_chunk_ids & old_chunk_ids)
    assert found(search, "zebracrossing") == {"VPN guide"}


def test_reindex_is_not_queued_twice(ingestion, drain):
    document_id = upload(ingestion).document_id
    drain()
    assert ingestion.reindex(actor_id="a", document_id=document_id) is True
    assert ingestion.reindex(actor_id="a", document_id=document_id) is False


def test_reindex_retries_a_failed_upload(db, blobs, search):
    service = make_service(db, blobs, FakeEmbedder(fail_times=1), ingest_max_attempts=1)
    document_id = upload(service).document_id
    drain_with(service, db, make_settings(ingest_max_attempts=1))
    assert version_of(db, document_id).status == "failed"

    assert service.reindex(actor_id="a", document_id=document_id) is True
    assert version_of(db, document_id).status == "pending"
    drain_with(service, db)

    assert version_of(db, document_id).status == "indexed"
    assert found(search, "zebracrossing") == {"VPN guide"}


def test_reindexing_with_nothing_uploaded_or_for_unknown_documents_fails(ingestion, db):
    with pytest.raises(DocumentNotFoundError):
        ingestion.reindex(actor_id="a", document_id=uuid.uuid4())
    document_id = upload(ingestion).document_id
    with db.session() as session:
        session.execute(text("DELETE FROM ingestion_jobs"))
        session.execute(text("DELETE FROM document_versions"))
    with pytest.raises(DocumentStateError):
        ingestion.reindex(actor_id="a", document_id=document_id)


# --- other formats flow through the same pipeline ---------------------------------


def test_a_pdf_is_extracted_chunked_and_searchable(ingestion, drain, search):
    upload(
        ingestion,
        make_pdf(["The quarterly narwhalbudget was approved."]),
        filename="budget.pdf",
        title="Budget",
    )
    drain()
    hits = search.search(READER, "narwhalbudget", limit=5)
    assert [h.title for h in hits] == ["Budget"]
    assert hits[0].heading == "Page 1"


# --- admin read models -------------------------------------------------------------


def test_the_admin_listing_reports_status_groups_versions_and_job_state(
    ingestion, db, drain
):
    document_id = upload(ingestion, groups=["staff", "hr"]).document_id
    [pending] = ingestion.list_documents()
    assert pending.latest_version.status == "pending"
    assert pending.latest_version.job.state == "queued"
    assert pending.current_version is None

    drain()
    info = ingestion.get_document(document_id)
    assert info.groups == ["hr", "staff"]
    assert info.current_version.status == "indexed"
    assert info.current_version.chunk_count == 2
    assert info.current_version.job.state == "succeeded"
    assert info.versions[0].uploaded_by == "admin-1"


def test_the_listing_never_exposes_storage_keys_or_content(ingestion, drain):
    upload(ingestion)
    drain()
    dump = repr(ingestion.list_documents())
    assert "documents/" not in dump  # blob keys
    assert "zebracrossing" not in dump  # document text
