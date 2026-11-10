"""The contract every ``SearchIndex`` implementation must satisfy.

The fixture below is the only place that knows which implementations exist.
Add a new adapter (Azure AI Search in Phase 8) by adding one entry to
``INDEXES``; it then has to pass the same permission, freshness and ordering
guarantees as the Postgres implementations.

The properties are deliberately independent of *how* a result was found: a
semantic implementation may return documents that share no words with the
question, so these tests check what must never be returned (unreadable,
deleted, superseded content) rather than demanding a specific result set.
"""

import threading

import pytest

from leyline.adapters.search_postgres import PostgresSearchIndex, SearchOptions
from leyline.ingestion.service import IngestionService
from leyline.ports.search import SearchHit
from leyline.services.authorization import can_read
from leyline.worker import run_worker
from tests.support import DEFAULT_CONCEPTS, ConceptEmbedder, make_settings, person

# title -> ACL
CORPUS = {
    "Public": ["*"],
    "Staff only": ["staff"],
    "Security only": ["security"],
    "HR only": ["hr"],
    "Finance only": ["finance"],
    "HR or Finance": ["hr", "finance"],
    "Nobody": [],
}
PEOPLE = {
    "employee": person("p1", "staff"),
    "security": person("p2", "staff", "security"),
    "hr": person("p3", "staff", "hr"),
    "finance": person("p4", "finance"),
    "no groups": person("p5"),
    "everyone-and-more": person("p6", "staff", "security", "hr", "finance", "*"),
}
QUERIES = [
    "lost stolen laptop policy",
    "somebody took my backpack",
    "alphaword",
    "holiday and vacation",
    "' OR 1=1 --",
    "!!!",
    "",
]


@pytest.fixture
def concept():
    return ConceptEmbedder(DEFAULT_CONCEPTS)


INDEXES = {
    "keyword": lambda db, embedder: PostgresSearchIndex(db),
    "hybrid": lambda db, embedder: PostgresSearchIndex(
        db,
        embedder,
        SearchOptions(min_similarity=-1.0),  # most permissive floor
    ),
    "hybrid-strict": lambda db, embedder: PostgresSearchIndex(
        db, embedder, SearchOptions(min_similarity=0.5, min_lexical=0.1)
    ),
}


@pytest.fixture(params=list(INDEXES))
def index(request, db, concept):
    return INDEXES[request.param](db, concept)


@pytest.fixture
def service(db, blobs, concept):
    return IngestionService(db, blobs, concept, make_settings())


@pytest.fixture
def load(db, service):
    def _load(title, text, groups, *, index=True):
        result = service.submit_upload(
            actor_id="t",
            filename="d.md",
            data=text.encode(),
            title=title,
            groups=groups,
        )
        if index:
            run_worker(service, db, make_settings(), stop=threading.Event(), once=True)
        return result.document_id

    return _load


@pytest.fixture
def corpus(load):
    ids = {}
    for title, groups in CORPUS.items():
        ids[title] = load(
            title,
            f"# {title}\n\nalphaword lost stolen laptop policy for {title}. "
            "Holiday and vacation notes.",
            groups,
        )
    return ids


def search(index, principal, query, limit=50):
    return index.search(principal, query, limit=limit)


# --- shape ----------------------------------------------------------------------------


def test_an_empty_index_returns_nothing(index):
    for query in QUERIES:
        assert search(index, PEOPLE["employee"], query) == []


def test_hits_carry_what_the_pipeline_needs(index, corpus):
    hits = search(index, PEOPLE["employee"], "lost stolen laptop policy")
    assert hits
    for hit in hits:
        assert isinstance(hit, SearchHit)
        assert hit.title and hit.content and hit.score > 0
        assert hit.document_id and hit.version_id and hit.chunk_id


def test_scores_never_increase_down_the_list(index, corpus):
    for query in QUERIES:
        scores = [h.score for h in search(index, PEOPLE["hr"], query)]
        assert scores == sorted(scores, reverse=True)


def test_the_limit_is_respected(index, corpus):
    for limit in (1, 2, 3):
        assert (
            len(search(index, PEOPLE["everyone-and-more"], "alphaword", limit)) <= limit
        )


def test_results_are_reproducible(index, corpus):
    first = [
        h.chunk_id for h in search(index, PEOPLE["hr"], "lost stolen laptop policy")
    ]
    again = [
        h.chunk_id for h in search(index, PEOPLE["hr"], "lost stolen laptop policy")
    ]
    assert first == again


# --- permissions: the guarantee that must hold for every implementation -------------------


@pytest.mark.parametrize("who", PEOPLE)
@pytest.mark.parametrize("query", QUERIES)
def test_nothing_unreadable_is_ever_returned(index, corpus, who, query):
    principal = PEOPLE[who]
    allowed = {title for title, acl in CORPUS.items() if can_read(principal, acl)}
    returned = {h.title for h in search(index, principal, query)}
    assert returned <= allowed, f"{who} was given {returned - allowed}"


def test_an_empty_acl_document_is_never_returned(index, corpus):
    for principal in PEOPLE.values():
        assert "Nobody" not in {h.title for h in search(index, principal, "alphaword")}


def test_revoking_access_applies_to_the_very_next_search(index, corpus, service):
    employee = PEOPLE["employee"]
    service.set_acl(actor_id="a", document_id=corpus["Staff only"], groups=["security"])
    assert "Staff only" not in {h.title for h in search(index, employee, "alphaword")}


def test_granting_access_applies_to_the_very_next_search(index, corpus, service):
    service.set_acl(actor_id="a", document_id=corpus["Security only"], groups=["*"])
    returned = {h.title for h in search(index, PEOPLE["no groups"], "alphaword")}
    # "alphaword" is in the text, so every implementation can find it by keyword
    assert "Security only" in returned
    # and nothing else may appear: only documents readable under the *new* ACLs
    assert returned <= {"Public", "Security only"}


def test_deleted_documents_are_never_returned(index, corpus, service):
    service.delete_document(actor_id="a", document_id=corpus["Public"])
    for principal in PEOPLE.values():
        for query in QUERIES:
            assert "Public" not in {h.title for h in search(index, principal, query)}


# --- freshness --------------------------------------------------------------------------------


def test_only_the_current_version_is_searchable(index, load, service, db):
    document_id = load("Policy", "# P\n\nolderwordx lost stolen laptop", ["*"])
    service.submit_upload(
        actor_id="a",
        filename="p.md",
        data=b"# P\n\nnewerwordx lost stolen laptop",
        document_id=document_id,
    )
    run_worker(service, db, make_settings(), stop=threading.Event(), once=True)

    for query in ("olderwordx", "lost stolen laptop", "newerwordx"):
        hits = search(index, PEOPLE["employee"], query)
        assert all("olderwordx" not in h.content for h in hits)


def test_documents_that_are_not_indexed_yet_are_not_returned(index, load):
    load("Draft", "# D\n\nalphaword lost stolen laptop", ["*"], index=False)
    assert search(index, PEOPLE["employee"], "alphaword lost stolen laptop") == []
