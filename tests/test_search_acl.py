"""Permission-aware search against a real Postgres.

This is the core security property of the product: a principal only ever
sees chunks of documents whose ACL names one of their groups (or the
"everyone" entry), and changes to ACLs and deletions apply immediately.
"""

import itertools
import uuid

import pytest
from sqlalchemy import update

from leyline.db.models import Document
from leyline.ports.search import SearchHit
from leyline.services.authorization import authorize_hits, can_read
from tests.support import person

TERM = "alphaword"

# title -> ACL
CORPUS = {
    "Public": ["*"],
    "Staff only": ["staff"],
    "Security only": ["security"],
    "HR only": ["hr"],
    "Finance only": ["finance"],
    "HR or Finance": ["hr", "finance"],
    "Nobody (empty ACL)": [],
}

PEOPLE = {
    "employee": person("p1", "staff"),
    "security": person("p2", "staff", "security"),
    "hr": person("p3", "staff", "hr"),
    "finance": person("p4", "finance"),
    "no groups": person("p5"),
    "everything": person("p6", "staff", "security", "hr", "finance"),
}

# What each person must see, written out by hand (not derived from the code).
EXPECTED = {
    "employee": {"Public", "Staff only"},
    "security": {"Public", "Staff only", "Security only"},
    "hr": {"Public", "Staff only", "HR only", "HR or Finance"},
    "finance": {"Public", "Finance only", "HR or Finance"},
    "no groups": {"Public"},
    "everything": set(CORPUS) - {"Nobody (empty ACL)"},
}


@pytest.fixture
def corpus(add_doc):
    ids = {}
    for title, groups in CORPUS.items():
        ids[title] = add_doc(title, f"# {title}\n\n{TERM} content of {title}.", groups)
    return ids


def titles(search, principal, query=TERM, limit=50):
    return {hit.title for hit in search.search(principal, query, limit=limit)}


@pytest.mark.parametrize("who", PEOPLE)
def test_each_person_sees_exactly_the_documents_their_groups_allow(corpus, search, who):
    assert titles(search, PEOPLE[who]) == EXPECTED[who]


def test_a_document_with_an_empty_acl_is_invisible_even_to_the_most_privileged(
    corpus, search
):
    everything = person("root", "staff", "security", "hr", "finance", "*")
    assert "Nobody (empty ACL)" not in titles(search, everything)


def test_the_database_filter_agrees_with_the_pure_rule_for_every_combination(
    db, add_doc, search
):
    """readable_by (SQL) and can_read (Python) must never disagree."""
    atoms = ["a", "b", "*"]
    acls = [
        list(combo)
        for size in range(len(atoms) + 1)
        for combo in itertools.combinations(atoms, size)
    ]
    for index, acl in enumerate(acls):
        add_doc(f"doc{index}", f"# D\n\n{TERM} number {index}", acl)

    people = [
        person(f"u{index}", *combo)
        for index, combo in enumerate(
            c for n in range(4) for c in itertools.combinations(["a", "b", "c"], n)
        )
    ]
    for principal in people:
        expected = {f"doc{i}" for i, acl in enumerate(acls) if can_read(principal, acl)}
        assert titles(search, principal) == expected, (principal.groups, acls)


# --- changes take effect on the very next request ---------------------------------


def test_revoking_a_group_hides_the_document_immediately(ingestion, corpus, search):
    employee = PEOPLE["employee"]
    assert "Staff only" in titles(search, employee)

    ingestion.set_acl(
        actor_id="a", document_id=corpus["Staff only"], groups=["security"]
    )

    assert "Staff only" not in titles(search, employee)
    assert "Staff only" in titles(search, PEOPLE["security"])


def test_granting_a_group_reveals_the_document_immediately(ingestion, corpus, search):
    assert "Security only" not in titles(search, PEOPLE["employee"])
    ingestion.set_acl(
        actor_id="a", document_id=corpus["Security only"], groups=["staff"]
    )
    assert "Security only" in titles(search, PEOPLE["employee"])


def test_clearing_an_acl_hides_the_document_from_everyone(ingestion, corpus, search):
    ingestion.set_acl(actor_id="a", document_id=corpus["Public"], groups=[])
    for principal in PEOPLE.values():
        assert "Public" not in titles(search, principal)


def test_a_deleted_document_disappears_for_everyone_immediately(
    ingestion, corpus, search
):
    ingestion.delete_document(actor_id="a", document_id=corpus["Public"])
    for principal in PEOPLE.values():
        assert "Public" not in titles(search, principal)


def test_only_the_current_version_is_searchable(ingestion, add_doc, drain, search):
    document_id = add_doc("Policy", "# Policy\n\nolderwordx rule", ["*"])
    ingestion.submit_upload(
        actor_id="a",
        filename="p.md",
        data=b"# Policy\n\nnewerwordx rule",
        document_id=document_id,
    )
    drain()
    reader = person("r", "staff")
    assert titles(search, reader, "newerwordx") == {"Policy"}
    assert titles(search, reader, "olderwordx") == set()


def test_a_document_that_is_not_yet_indexed_is_not_searchable(add_doc, search):
    add_doc("Draft", f"# Draft\n\n{TERM} pending", ["*"], index=False)
    assert titles(search, person("r", "staff")) == set()


# --- the principal is the only thing that decides ----------------------------------


def test_a_group_name_that_looks_like_sql_matches_nothing(corpus, search):
    sneaky = person("x", "staff' OR '1'='1", "security' --", "%", "_", "*x")
    assert titles(search, sneaky) == {"Public"}


def test_a_group_literally_named_star_is_just_the_everyone_entry(corpus, search):
    assert titles(search, person("x", "*")) == {"Public"}


def test_group_matching_is_exact_not_prefix_or_case_insensitive(corpus, search):
    near_misses = person("x", "Security", "security-team", "sec", "SECURITY")
    assert "Security only" not in titles(search, near_misses)


@pytest.mark.parametrize(
    "query",
    ["' OR 1=1 --", "a:* & !b", "(((", "\\", "%", "; DROP TABLE chunks; --", "!!!"],
)
def test_hostile_queries_are_harmless(corpus, search, db, query):
    assert titles(search, PEOPLE["employee"], query) <= EXPECTED["employee"]
    assert titles(search, PEOPLE["employee"]) == EXPECTED["employee"]  # table intact


def test_results_respect_the_limit_and_rank_better_matches_first(add_doc, search):
    add_doc("Weak", "# W\n\n" + "filler " * 40 + "rankterm", ["*"])
    add_doc("Strong", "# Strong\n\nrankterm rankterm rankterm", ["*"])
    add_doc("Other", "# Other\n\nnothing relevant here", ["*"])
    reader = person("r", "staff")

    hits = search.search(reader, "rankterm", limit=1)

    assert [h.title for h in hits] == ["Strong"]
    assert len(search.search(reader, "rankterm", limit=10)) == 2


def test_search_hits_expose_chunk_text_title_and_heading(add_doc, search):
    add_doc("Guide", "# Guide\n\n## Setup\nfindthisphrase here", ["*"])
    [hit] = search.search(person("r"), "findthisphrase", limit=5)
    assert hit.title == "Guide" and hit.heading == "Guide > Setup"
    assert "findthisphrase" in hit.content and hit.score > 0


# --- re-authorization of hits ------------------------------------------------------


def hits_for(search, principal, query=TERM):
    return search.search(principal, query, limit=50)


def test_authorize_hits_keeps_what_is_still_readable(db, corpus, search):
    employee = PEOPLE["employee"]
    hits = hits_for(search, employee)
    assert {h.title for h in authorize_hits(db, employee, hits)} == EXPECTED["employee"]


def test_authorize_hits_drops_hits_whose_acl_was_revoked_after_the_search(
    db, ingestion, corpus, search
):
    employee = PEOPLE["employee"]
    stale = hits_for(search, employee)  # an index that is slightly behind
    ingestion.set_acl(
        actor_id="a", document_id=corpus["Staff only"], groups=["security"]
    )

    kept = {h.title for h in authorize_hits(db, employee, stale)}

    assert "Staff only" not in kept and "Public" in kept


def test_authorize_hits_drops_hits_of_deleted_documents(db, ingestion, corpus, search):
    employee = PEOPLE["employee"]
    stale = hits_for(search, employee)
    ingestion.delete_document(actor_id="a", document_id=corpus["Public"])
    assert "Public" not in {h.title for h in authorize_hits(db, employee, stale)}


def test_authorize_hits_drops_hits_of_superseded_versions(
    db, ingestion, add_doc, drain, search
):
    reader = person("r", "staff")
    document_id = add_doc("Policy", f"# P\n\n{TERM} oldtext", ["*"])
    stale = hits_for(search, reader)
    ingestion.submit_upload(
        actor_id="a",
        filename="p.md",
        data=f"# P\n\n{TERM} newtext".encode(),
        document_id=document_id,
    )
    drain()
    assert authorize_hits(db, reader, stale) == []


def test_authorize_hits_cannot_be_talked_into_returning_unreadable_documents(
    db, corpus, search
):
    """A forged hit for a document the principal cannot read is dropped."""
    security_hits = hits_for(search, PEOPLE["security"])
    secret = next(h for h in security_hits if h.title == "Security only")
    forged = SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=secret.document_id,
        version_id=secret.version_id,
        title="Totally public",
        heading=None,
        content="smuggled",
        score=99.0,
    )
    assert authorize_hits(db, PEOPLE["employee"], [forged]) == []


def test_authorize_hits_with_no_hits_does_not_touch_the_database():
    class Exploding:
        def session(self):
            raise AssertionError("should not be called")

    assert authorize_hits(Exploding(), person("x"), []) == []


def test_a_document_whose_current_version_is_cleared_has_no_readable_hits(
    db, corpus, search
):
    stale = hits_for(search, PEOPLE["employee"])
    with db.session() as session:
        session.execute(update(Document).values(current_version_id=None))
    assert authorize_hits(db, PEOPLE["employee"], stale) == []
