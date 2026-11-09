"""Hybrid (keyword + semantic) retrieval against a real Postgres."""

import threading

import pytest

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.ingestion.service import IngestionService
from leyline.worker import run_worker
from tests.support import (
    DEFAULT_CONCEPTS,
    ConceptEmbedder,
    FakeEmbedder,
    make_settings,
    person,
)

READER = person("reader", "staff")


@pytest.fixture
def concept():
    return ConceptEmbedder(DEFAULT_CONCEPTS)


@pytest.fixture
def world(db, blobs, concept):
    """Ingestion + a hybrid index sharing one concept embedder."""
    settings = make_settings()
    service = IngestionService(db, blobs, concept, settings)

    class World:
        def add(self, title, text, groups=("*",)):
            service.submit_upload(
                actor_id="t",
                filename="d.md",
                data=text.encode(),
                title=title,
                groups=list(groups),
            )
            run_worker(service, db, settings, stop=threading.Event(), once=True)

        def index(self, **options):
            return PostgresSearchIndex(db, concept, SearchOptions(**options))

    return World()


def titles(hits):
    return [h.title for h in hits]


def search(index, query, principal=READER, limit=10):
    return index.search(principal, query, limit=limit)


# --- semantic matches ---------------------------------------------------------------


def test_semantic_search_finds_a_document_that_shares_no_words_with_the_question(world):
    world.add(
        "Lost devices", "# Devices\n\nReport a lost or stolen laptop immediately."
    )
    world.add("Printing", "# Printing\n\nThe printer jam light means open the tray.")

    keyword_only = PostgresSearchIndex(world.index().__dict__["_database"])
    question = "somebody took my backpack"

    assert search(keyword_only, question) == []  # no overlapping words at all
    hybrid = search(world.index(), question)
    assert titles(hybrid)[0] == "Lost devices"
    assert hybrid[0].similarity == pytest.approx(1.0, abs=0.01)
    assert hybrid[0].lexical_score is None  # found by meaning, not by keywords


def test_keyword_matches_still_work_when_the_question_has_no_semantic_signal(world):
    world.add("Error codes", "# Codes\n\nThe fault zx81x means the fan is blocked.")
    hits = search(world.index(), "what is zx81x")
    assert titles(hits) == ["Error codes"]
    assert hits[0].lexical_score is not None


def test_a_chunk_found_by_both_legs_outranks_chunks_found_by_one(world):
    world.add("Both", "# One\n\nA lost laptop must be reported to the helpdesk.")
    world.add("Meaning only", "# Two\n\nStolen devices go through the helpdesk too.")
    world.add("Words only", "# Three\n\nThe helpdesk desk laptop queue opens at nine.")

    # a real similarity floor, so unrelated text is not a "neighbour" by default
    hits = search(world.index(min_similarity=0.5), "laptop was lost")

    by_title = {h.title: h for h in hits}
    assert titles(hits)[0] == "Both"
    assert by_title["Both"].similarity is not None
    assert by_title["Both"].lexical_score is not None
    assert by_title["Meaning only"].lexical_score is None
    assert by_title["Words only"].similarity is None
    assert by_title["Both"].score > by_title["Meaning only"].score
    assert by_title["Both"].score > by_title["Words only"].score


def test_results_are_ordered_by_descending_score(world):
    for i in range(4):
        world.add(f"Doc {i}", f"# D{i}\n\nlaptop lost number{i} " + "filler " * i)
    hits = search(world.index(), "lost laptop")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True) and len(scores) >= 2


# --- relevance floors ------------------------------------------------------------------


def test_a_similarity_floor_drops_chunks_that_are_not_about_the_question(world):
    world.add("Holiday", "# Leave\n\nAnnual holiday and vacation rules.")
    world.add("Printing", "# Printing\n\nThe printer jam light means open the tray.")

    loose = search(world.index(min_similarity=-1.0), "my printer has a jam")
    strict = search(world.index(min_similarity=0.5), "my printer has a jam")

    assert "Holiday" in titles(loose)  # with no floor, anything is a "neighbour"
    assert titles(strict) == ["Printing"]


def test_both_floors_together_let_the_search_refuse_unrelated_questions(world):
    world.add("Holiday", "# Leave\n\nAnnual holiday and vacation rules.")
    world.add("Printing", "# Printing\n\nThe printer jam light means open the tray.")

    index = world.index(min_similarity=0.5, min_lexical=0.1)

    assert search(index, "what is the capital of australia") == []
    assert search(index, "write a poem about the sea") == []


def test_a_lexical_floor_silences_weak_keyword_matches(world):
    world.add("Rare", "# R\n\nlaptop " + "padding " * 40 + "zorbulate")
    weak = search(world.index(min_similarity=0.99, min_lexical=0.0), "the zorbulate")
    assert titles(weak) == ["Rare"]
    assert (
        search(world.index(min_similarity=0.99, min_lexical=50.0), "the zorbulate")
        == []
    )


# --- caps and limits ----------------------------------------------------------------------


def test_at_most_n_chunks_per_document_are_returned(world):
    body = "\n\n".join(
        f"## Part {i}\nlaptop policy details {i} " + "word " * 120 for i in range(6)
    )
    world.add("Long", "# Long\n\n" + body)
    world.add("Short", "# Short\n\nlaptop policy summary")

    capped = search(world.index(max_chunks_per_document=2), "laptop policy")
    uncapped = search(world.index(max_chunks_per_document=50), "laptop policy")

    assert sum(1 for h in capped if h.title == "Long") == 2
    assert sum(1 for h in uncapped if h.title == "Long") > 2
    assert "Short" in titles(capped)  # the cap leaves room for other documents


def test_the_limit_bounds_the_number_of_hits(world):
    for i in range(6):
        world.add(f"Doc {i}", f"# D\n\nlost laptop case {i}")
    assert len(search(world.index(), "lost laptop", limit=3)) == 3


# --- permissions (the part that must never break) ------------------------------------------


def test_the_semantic_leg_never_returns_a_document_the_asker_cannot_read(world):
    world.add(
        "Secret theft policy",
        "# Secret\n\nStolen devices: classified steps.",
        ["security"],
    )
    world.add(
        "Public printing", "# Printing\n\nThe printer jam light means open the tray."
    )

    index = world.index(min_similarity=-1.0)  # most permissive: anything is a neighbour

    outsider = titles(search(index, "somebody took my backpack"))
    insider = titles(
        search(index, "somebody took my backpack", person("s", "staff", "security"))
    )

    assert "Secret theft policy" not in outsider  # nearest by meaning, still invisible
    assert insider[0] == "Secret theft policy"


def test_a_selective_permission_filter_still_leaves_readable_results(world):
    """Many unreadable documents are nearer than the only readable one."""
    for i in range(45):
        world.add(
            f"Hidden {i}", f"# H\n\nlost stolen laptop incident {i}", ["security"]
        )
    world.add("Visible", "# V\n\nprinter jam in the tray", ["*"])

    hits = search(world.index(min_similarity=-1.0, candidates=40), "stolen laptop")

    assert titles(hits) == ["Visible"]  # found despite 45 closer-but-hidden rivals


def test_acl_changes_and_deletions_apply_immediately_in_hybrid_mode(world, ingestion):
    world.add("Doc", "# D\n\nA lost or stolen laptop must be reported.", ["staff"])
    index = world.index()
    assert titles(search(index, "somebody took my backpack")) == ["Doc"]

    [info] = ingestion.list_documents()
    ingestion.set_acl(actor_id="a", document_id=info.id, groups=["security"])
    assert search(index, "somebody took my backpack") == []

    ingestion.set_acl(actor_id="a", document_id=info.id, groups=["staff"])
    assert titles(search(index, "somebody took my backpack")) == ["Doc"]
    ingestion.delete_document(actor_id="a", document_id=info.id)
    assert search(index, "somebody took my backpack") == []


def test_old_versions_are_not_returned_by_either_leg(world, ingestion, drain):
    world.add("Policy", "# P\n\nOlder wording: lost devices go to oldmarker.")
    [info] = ingestion.list_documents()
    ingestion.submit_upload(
        actor_id="a",
        filename="p.md",
        data=b"# P\n\nNewer wording about a stolen device.",
        document_id=info.id,
    )
    drain()  # (the shared `ingestion` uses FakeEmbedder; keyword leg is enough here)

    hits = search(world.index(min_similarity=-1.0), "oldmarker lost devices")

    assert hits and all("oldmarker" not in h.content for h in hits)


# --- robustness ---------------------------------------------------------------------------------


def test_chunks_without_embeddings_are_still_found_by_keyword(db, blobs, world):
    keyword_only = IngestionService(db, blobs, None, make_settings())
    keyword_only.submit_upload(
        actor_id="t",
        filename="d.md",
        data=b"# K\n\nzx81x fan fault",
        title="No vectors",
        groups=["*"],
    )
    run_worker(keyword_only, db, make_settings(), stop=threading.Event(), once=True)
    world.add("With vectors", "# V\n\nunrelated holiday text")

    hits = search(world.index(), "zx81x")

    assert titles(hits)[0] == "No vectors"


def test_a_failing_embedding_service_degrades_to_keyword_search(world, db):
    world.add("Doc", "# D\n\nreset your password with the helpdesk")
    broken = PostgresSearchIndex(db, FakeEmbedder(fail_times=99), SearchOptions())

    assert titles(search(broken, "password reset")) == ["Doc"]  # no exception


def test_a_model_with_the_wrong_dimension_degrades_instead_of_breaking(world, db):
    class WrongSize:
        model = "wrong"

        def embed(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    world.add("Doc", "# D\n\nreset your password with the helpdesk")
    index = PostgresSearchIndex(db, WrongSize(), SearchOptions())
    assert titles(search(index, "password reset")) == ["Doc"]


def test_the_query_prefix_is_applied_to_the_text_that_is_embedded(world, concept):
    world.add("Doc", "# D\n\nlost laptop")
    concept.calls.clear()
    search(world.index(query_prefix="search_query: "), "lost laptop")
    assert concept.calls == ["search_query: lost laptop"]


@pytest.mark.parametrize("query", ["", "   ", "!!!", "' OR 1=1 --", "a:* & !b", "\\"])
def test_hostile_or_empty_questions_are_harmless(world, query):
    world.add("Doc", "# D\n\nlost laptop")
    hits = search(world.index(min_similarity=-1.0), query)
    assert all(h.title == "Doc" for h in hits)


def test_ties_break_the_same_way_every_time(world):
    for name in ("Bravo", "Alpha", "Charlie"):
        world.add(name, "# T\n\nidentical text about a printer jam")
    first = titles(search(world.index(), "printer jam"))
    for _ in range(3):
        assert titles(search(world.index(), "printer jam")) == first
    assert first == sorted(first)  # ties resolve alphabetically by title


def test_without_an_embedder_the_index_is_plain_keyword_search(db, ingestion, add_doc):
    add_doc("Doc", "# D\n\nlost laptop report", ["*"])
    hits = PostgresSearchIndex(db).search(READER, "lost laptop", limit=5)
    assert titles(hits) == ["Doc"]
    assert hits[0].similarity is None and hits[0].lexical_score is not None


# --- the lexical floor only applies while the vector leg runs ----------------------------


def test_the_lexical_floor_does_not_apply_to_keyword_only_search(
    db, ingestion, add_doc
):
    add_doc("Doc", "# D\n\nthe zorbulate procedure", ["*"])
    strict = SearchOptions(min_lexical=50.0)  # would drop every keyword match

    hits = PostgresSearchIndex(db, None, strict).search(READER, "zorbulate", limit=5)

    assert titles(hits) == ["Doc"]


def test_a_failing_embedder_does_not_leave_keyword_search_under_the_hybrid_floor(
    db, ingestion, add_doc
):
    add_doc("Doc", "# D\n\nthe zorbulate procedure", ["*"])
    index = PostgresSearchIndex(
        db, FakeEmbedder(fail_times=99), SearchOptions(min_lexical=50.0)
    )
    assert titles(search(index, "zorbulate")) == ["Doc"]


def test_with_a_working_embedder_the_lexical_floor_does_apply(world):
    world.add("Doc", "# D\n\nthe zorbulate procedure")
    index = world.index(min_similarity=0.99, min_lexical=50.0)
    assert search(index, "zorbulate") == []


def test_strict_mode_surfaces_embedding_failures_instead_of_degrading(db, world):
    world.add("Doc", "# D\n\npassword reset")
    strict = PostgresSearchIndex(
        db, FakeEmbedder(fail_times=99), SearchOptions(strict_embedding=True)
    )
    with pytest.raises(RuntimeError, match="embedding provider unavailable"):
        search(strict, "password reset")


def test_strict_mode_rejects_vectors_of_the_wrong_size(db, world):
    class WrongSize:
        model = "wrong"

        def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

    world.add("Doc", "# D\n\npassword reset")
    strict = PostgresSearchIndex(db, WrongSize(), SearchOptions(strict_embedding=True))
    with pytest.raises(ValueError, match="2 dimensions"):
        search(strict, "password reset")
