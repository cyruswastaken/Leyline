"""What the embedding model reads for a chunk: the text, led by its context."""

from leyline.ingestion.chunker import ChunkDraft
from leyline.ingestion.service import IngestionService
from tests.support import make_settings


def service(db, blobs, embedder, **settings):
    return IngestionService(db, blobs, embedder, make_settings(**settings))


def draft(heading, content="Submit within thirty days."):
    return ChunkDraft(index=0, heading=heading, content=content)


def test_the_default_text_is_the_heading_then_the_chunk(db, blobs, embedder):
    s = service(db, blobs, embedder)
    assert (
        s._embedding_text(draft("Submitting claims"), "Expense Policy")
        == "Submitting claims\nSubmit within thirty days."
    )
    assert s._embedding_text(draft(None), "Expense Policy") == (
        "Submit within thirty days."
    )


def test_with_the_title_setting_the_title_leads_the_text(db, blobs, embedder):
    s = service(db, blobs, embedder, embedding_include_title=True)
    assert (
        s._embedding_text(draft("Submitting claims"), "Expense Policy")
        == "Expense Policy\nSubmitting claims\nSubmit within thirty days."
    )
    assert s._embedding_text(draft(None), "Expense Policy") == (
        "Expense Policy\nSubmit within thirty days."
    )


def test_a_heading_that_only_repeats_the_title_is_not_said_twice(db, blobs, embedder):
    s = service(db, blobs, embedder, embedding_include_title=True)
    assert s._embedding_text(draft("Expense Policy"), "Expense Policy") == (
        "Expense Policy\nSubmit within thirty days."
    )
    # without the title setting nothing is dropped (the recorded vectors depend on it)
    plain = service(db, blobs, embedder)
    assert plain._embedding_text(draft("Expense Policy"), "Expense Policy") == (
        "Expense Policy\nSubmit within thirty days."
    )


def test_the_task_prefix_goes_first(db, blobs, embedder):
    s = service(
        db,
        blobs,
        embedder,
        embedding_include_title=True,
        embedding_document_prefix="search_document: ",
    )
    assert s._embedding_text(draft("H"), "T").startswith("search_document: T\nH\n")


def test_a_missing_title_adds_nothing(db, blobs, embedder):
    s = service(db, blobs, embedder, embedding_include_title=True)
    assert s._embedding_text(draft("H"), "") == "H\nSubmit within thirty days."


class SpyEmbedder:
    """Delegates to the test embedder and keeps every text it was asked to embed."""

    def __init__(self, inner):
        self._inner = inner
        self.model = inner.model
        self.texts: list[str] = []

    def embed(self, texts):
        self.texts.extend(texts)
        return self._inner.embed(texts)


def test_only_what_is_embedded_changes_not_what_is_stored(db, blobs, embedder):
    import threading

    from sqlalchemy import select

    from leyline.db.models import Chunk
    from leyline.worker import run_worker

    spy = SpyEmbedder(embedder)
    settings = make_settings(embedding_include_title=True)
    s = IngestionService(db, blobs, spy, settings)
    s.submit_upload(
        actor_id="t",
        filename="d.md",
        data=b"# Booking\n\nSubmit within thirty days.",
        title="Expense Policy",
        groups=["*"],
    )
    run_worker(s, db, settings, stop=threading.Event(), once=True)

    assert spy.texts and all(t.startswith("Expense Policy\n") for t in spy.texts)
    with db.session() as session:
        stored = [c.content for c in session.scalars(select(Chunk))]
    assert stored and not any("Expense Policy" in c for c in stored)
