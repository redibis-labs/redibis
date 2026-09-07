"""Tests for JSON-file auth + scoped sharing."""

import hashlib

import pytest

from redibis.store.storage_backend import LocalBackend
from redibis.store.auth_store import (
    AuthStore, hash_password, apply_scoped_edits, scope_allows,
    MIN_PASSWORD_CHARS,
)


@pytest.fixture
def backend(tmp_path):
    return LocalBackend(tmp_path / "s")


@pytest.fixture
def auth(backend):
    return AuthStore(backend, bucket="active-contracts")


def test_pbkdf2_hash_is_salted():
    a = hash_password("same-password-1")
    b = hash_password("same-password-1")
    assert a != b
    assert a.startswith("pbkdf2_sha256$")
    assert b.startswith("pbkdf2_sha256$")


def test_create_and_authenticate(auth):
    auth.create_user("alice", "pw1234567890", role="explorer", default_scopes=["business"])
    assert auth.authenticate("alice", "pw1234567890").role == "explorer"
    assert auth.authenticate("alice", "wrong-password") is None
    assert "password_hash" not in auth.list_users()[0]


def test_short_password_rejected(auth):
    with pytest.raises(ValueError, match="at least"):
        auth.create_user("bob", "short", role="explorer")
    assert MIN_PASSWORD_CHARS == 12


def test_default_admin_bootstrap(auth):
    created = auth.ensure_default_admin()
    assert created.role == "admin"
    assert auth.ensure_default_admin() is None


def test_share_lifecycle(auth):
    share = auth.create_share("telecom.customers", scope="pii", created_by="admin")
    assert share.share_token
    assert auth.get_share(share.share_token).scope == "pii"
    assert auth.list_shares("telecom.customers")
    assert auth.revoke_share(share.share_token) is True
    assert auth.get_share(share.share_token) is None


def test_scope_enforcement():
    assert scope_allows("business", "business")
    assert not scope_allows("business", "quality")
    assert scope_allows("all", "quality")


def test_apply_scoped_edits_respects_scope():
    active = {"schema": [{"name": "t", "properties": [
        {"name": "email", "classification": "internal", "business": {}},
    ]}]}
    edits = {"columns": {"email": {
        "business": {"definition": "Email"},     # business scope ok
        "quality": [{"rule": "missingCount"}],   # NOT in business scope
        "classification": "pii_personal",        # NOT in business scope
    }}}
    modified, applied = apply_scoped_edits(active, edits, "business")
    prop = modified["schema"][0]["properties"][0]
    assert prop["business"]["definition"] == "Email"   # applied
    assert "quality" not in prop                        # skipped
    assert prop["classification"] == "internal"         # unchanged
    assert applied == ["email.business"]


def test_apply_scoped_edits_pii_scope():
    active = {"schema": [{"name": "t", "properties": [{"name": "email"}]}]}
    edits = {"columns": {"email": {"classification": "pii_personal",
                                    "pii": {"entity_type": "EMAIL"}}}}
    modified, applied = apply_scoped_edits(active, edits, "pii")
    prop = modified["schema"][0]["properties"][0]
    assert prop["classification"] == "pii_personal"
    assert prop["pii"]["entity_type"] == "EMAIL"
    assert set(applied) == {"email.classification", "email.pii"}


def test_legacy_sha256_still_in_hashlib():
    digest = hashlib.sha256(b"x").hexdigest()
    assert len(digest) == 64
