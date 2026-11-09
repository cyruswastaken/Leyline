import pytest

from leyline.auth.principal import (
    AppRole,
    InvalidClaimsError,
    Principal,
    principal_from_claims,
)
from leyline.services.authorization import can_access, filter_authorized_documents

RESTRICTED = frozenset({"security"})


# --- claims -> principal ----------------------------------------------------


def test_principal_from_claims_maps_identity_groups_and_roles():
    principal = principal_from_claims(
        {
            "sub": "abc-123",
            "name": "Ada",
            "email": "ada@example.test",
            "groups": ["staff", "security"],
            "roles": ["security_auditor"],
        }
    )
    assert principal.user_id == "abc-123"
    assert principal.groups == {"staff", "security"}
    assert principal.roles == {AppRole.USER, AppRole.SECURITY_AUDITOR}


def test_everyone_is_a_plain_user_and_unknown_roles_grant_nothing():
    principal = principal_from_claims(
        {"sub": "x", "roles": ["root", "kb_admin_extra", "KB_ADMIN"]}
    )
    assert principal.roles == {AppRole.USER}


def test_custom_claim_names_are_honoured():
    principal = principal_from_claims(
        {"sub": "x", "grp": ["hr"], "app_roles": ["kb_admin"]},
        groups_claim="grp",
        roles_claim="app_roles",
    )
    assert principal.groups == {"hr"}
    assert AppRole.KB_ADMIN in principal.roles


@pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": "  "}, {"sub": 42}])
def test_claims_without_a_usable_subject_are_rejected(claims):
    with pytest.raises(InvalidClaimsError):
        principal_from_claims(claims)


@pytest.mark.parametrize("groups", [None, 5, {"a": 1}, [1, None, {}]])
def test_malformed_group_claims_become_no_groups(groups):
    assert principal_from_claims({"sub": "x", "groups": groups}).groups == frozenset()


def test_group_overage_is_flagged():
    principal = principal_from_claims({"sub": "x", "_claim_names": {"groups": "src1"}})
    assert principal.groups_incomplete is True
    assert principal.groups == frozenset()


def test_principal_survives_a_session_round_trip():
    original = principal_from_claims(
        {"sub": "x", "groups": ["g1"], "roles": ["support_agent"], "name": "N"}
    )
    assert Principal.from_session(original.to_session()) == original


@pytest.mark.parametrize(
    "stored", [None, "str", [], {}, {"user_id": ""}, {"user_id": 5}]
)
def test_corrupt_session_data_is_not_an_identity(stored):
    assert Principal.from_session(stored) is None


def test_tampered_session_roles_cannot_invent_roles():
    stored = {"user_id": "x", "roles": ["superuser", "kb_admin"], "groups": []}
    principal = Principal.from_session(stored)
    assert principal is not None
    assert principal.roles == {AppRole.USER, AppRole.KB_ADMIN}


# --- document authorization -------------------------------------------------


def _doc(level):
    return {"id": f"d-{level}", "access_level": level}


def test_general_documents_need_only_a_signed_in_user():
    user = Principal(user_id="u")
    assert can_access("general", user, restricted_groups=RESTRICTED)


def test_restricted_documents_need_a_restricted_group():
    outsider = Principal(user_id="u", groups=frozenset({"staff"}))
    insider = Principal(user_id="v", groups=frozenset({"staff", "security"}))
    assert not can_access("restricted", outsider, restricted_groups=RESTRICTED)
    assert can_access("restricted", insider, restricted_groups=RESTRICTED)


def test_app_roles_do_not_grant_document_access():
    auditor = Principal(
        user_id="u", roles=frozenset({AppRole.USER, AppRole.SECURITY_AUDITOR})
    )
    assert not can_access("restricted", auditor, restricted_groups=RESTRICTED)


def test_empty_restricted_group_config_grants_nobody():
    insider = Principal(user_id="v", groups=frozenset({"security"}))
    assert not can_access("restricted", insider, restricted_groups=frozenset())


@pytest.mark.parametrize("level", [None, "", "secret", "GENERAL", "Restricted", 7])
def test_missing_or_unknown_access_level_is_denied_to_everyone(level):
    everything = Principal(
        user_id="u",
        groups=frozenset({"security", "staff"}),
        roles=frozenset(AppRole),
    )
    assert not can_access(level, everything, restricted_groups=RESTRICTED)


def test_overage_principal_cannot_read_restricted():
    principal = principal_from_claims({"sub": "x", "_claim_names": {"groups": "src1"}})
    docs = [_doc("general"), _doc("restricted")]
    allowed = filter_authorized_documents(docs, principal, restricted_groups=RESTRICTED)
    assert [d["access_level"] for d in allowed] == ["general"]
