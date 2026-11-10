import pytest

from leyline.auth.principal import (
    AppRole,
    InvalidClaimsError,
    Principal,
    principal_from_claims,
)
from leyline.services.authorization import can_read

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


ENTRA_IDS = {
    "11111111-1111-1111-1111-111111111111": "staff",
    "22222222-2222-2222-2222-222222222222": "hr",
}


def test_entra_group_ids_become_the_group_names_documents_use():
    principal = principal_from_claims(
        {"sub": "u", "groups": list(ENTRA_IDS)}, group_names=ENTRA_IDS
    )
    assert principal.groups == {"staff", "hr"}


def test_a_group_nobody_mapped_grants_nothing_when_a_map_is_configured():
    principal = principal_from_claims(
        {"sub": "u", "groups": ["33333333-3333-3333-3333-333333333333"]},
        group_names=ENTRA_IDS,
    )
    assert principal.groups == frozenset()


def test_a_group_literally_named_like_an_acl_group_is_not_trusted_through_the_map():
    # With a map configured only identifiers in the map count, so a token entry that
    # merely spells "hr" (or "staff") cannot stand in for the mapped group.
    principal = principal_from_claims(
        {"sub": "u", "groups": ["hr", "staff"]}, group_names=ENTRA_IDS
    )
    assert principal.groups == frozenset()


def test_without_a_map_group_names_pass_through_as_before():
    principal = principal_from_claims({"sub": "u", "groups": ["hr"]})
    assert principal.groups == {"hr"}
    assert principal_from_claims(
        {"sub": "u", "groups": ["hr"]}, group_names={}
    ).groups == {"hr"}


def test_overage_is_still_flagged_and_fails_closed_with_a_map():
    principal = principal_from_claims(
        {"sub": "u", "_claim_names": {"groups": "src1"}}, group_names=ENTRA_IDS
    )
    assert principal.groups_incomplete is True and principal.groups == frozenset()


def test_the_group_map_is_read_from_a_json_environment_variable(monkeypatch):
    from leyline.config import Settings

    monkeypatch.setenv("OIDC_GROUP_NAMES", '{"abc": "hr"}')
    assert Settings(_env_file=None).oidc_group_names == {"abc": "hr"}  # type: ignore[call-arg]


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


# --- document authorization (pure rules) --------------------------------------


def person(*groups: str, roles: frozenset[AppRole] | None = None) -> Principal:
    return Principal(
        user_id="u",
        groups=frozenset(groups),
        roles=roles or frozenset({AppRole.USER}),
    )


def test_a_matching_group_grants_read():
    assert can_read(person("staff", "security"), ["security"])


def test_no_matching_group_denies_read():
    assert not can_read(person("staff"), ["security", "hr"])


def test_the_everyone_entry_grants_any_signed_in_user_even_without_groups():
    assert can_read(person(), ["*"])
    assert can_read(person("staff"), ["*", "security"])


def test_an_empty_acl_grants_nobody():
    everything = person("staff", "security", "*", roles=frozenset(AppRole))
    assert not can_read(everything, [])


def test_app_roles_never_grant_read():
    powerful = person(roles=frozenset(AppRole))
    assert not can_read(powerful, ["security"])


def test_group_matching_is_exact_and_case_sensitive():
    assert not can_read(person("Security"), ["security"])
    assert not can_read(person("security-team"), ["security"])
    assert not can_read(person("sec"), ["security"])
    assert not can_read(person("security "), ["security"])


def test_the_wildcard_is_not_a_pattern():
    assert not can_read(person("sec*"), ["security"])
    assert not can_read(person("security"), ["sec*"])


def test_overage_principal_only_reads_everyone_documents():
    principal = principal_from_claims({"sub": "x", "_claim_names": {"groups": "src1"}})
    assert can_read(principal, ["*"])
    assert not can_read(principal, ["security"])
