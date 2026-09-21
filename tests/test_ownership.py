"""ODCS ownership migration — legacy owner → top-level team."""

from __future__ import annotations

from redibis.contracts.ownership import (
    ensure_team_member,
    normalize_odcs_ownership,
    owner_as_team_member,
)


def test_owner_as_team_member_from_string():
    assert owner_as_team_member("mdaoor") == {
        "name": "mdaoor",
        "username": "mdaoor",
        "role": "owner",
    }
    assert owner_as_team_member("  ") is None
    assert owner_as_team_member(None) is None


def test_normalize_promotes_owner_and_strips_invalid_fields():
    contract = {
        "owner": "mdaoor",
        "team": [{"name": "mdaoor", "username": "mdaoor", "role": "owner"}],
        "schema": [{
            "name": "t",
            "owner": "mdaoor",
            "team": [{"name": "ops", "username": "ops", "role": "steward"}],
            "properties": [],
        }],
    }
    out = normalize_odcs_ownership(contract)
    assert "owner" not in out
    assert "owner" not in out["schema"][0]
    assert "team" not in out["schema"][0]
    usernames = {m["username"] for m in out["team"]}
    assert usernames == {"mdaoor", "ops"}


def test_ensure_team_member_dedupes():
    contract = {"team": [{"name": "ada", "username": "ada", "role": "owner"}]}
    assert ensure_team_member(contract, owner_as_team_member("ada")) is False
    assert len(contract["team"]) == 1
    assert ensure_team_member(contract, owner_as_team_member("bob")) is True
    assert len(contract["team"]) == 2
