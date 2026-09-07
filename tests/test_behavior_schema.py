"""Tests for redibis.behavior.schema — structural validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from redibis.behavior.schema import (
    BehaviorPolicyDocument,
    canonical_sha256,
    validate_document,
    validate_document_size,
    MAX_REASON_LEN,
    MAX_RULES_PER_POLICY,
)


# ---------------------------------------------------------------------------
# Valid minimal document
# ---------------------------------------------------------------------------

MINIMAL_VALID = {
    "apiVersion": "redibis.io/behavior-policy/v1",
    "kind": "BehaviorPolicy",
    "metadata": {
        "id": "test-policy",
        "version": "1.0.0",
        "description": "A test policy",
    },
    "appliesTo": {
        "engine": "pii",
        "stage": "post_verdict",
    },
    "rules": [
        {
            "id": "rule-1",
            "when": {"fact": "column.name", "op": "matches", "value": "(?i)lac"},
            "effects": [
                {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}
            ],
            "reason": "LAC is a network identifier, not a phone number.",
        }
    ],
}


def test_minimal_valid_document():
    doc = validate_document(MINIMAL_VALID)
    assert doc.metadata.id == "test-policy"
    assert len(doc.rules) == 1
    assert doc.rules[0].reason == "LAC is a network identifier, not a phone number."


def test_all_document_group():
    doc_raw = {
        "apiVersion": "redibis.io/behavior-policy/v1",
        "kind": "BehaviorPolicy",
        "metadata": {"id": "p", "version": "1.0.0"},
        "appliesTo": {"engine": "pii", "stage": "post_verdict"},
        "rules": [
            {
                "id": "r1",
                "when": {
                    "all": [
                        {"fact": "column.name", "op": "matches", "value": "lac"},
                        {"fact": "verdict.entity", "op": "eq", "value": "PHONE_NUMBER"},
                    ]
                },
                "effects": [
                    {"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}
                ],
                "reason": "Network identifier correction.",
            }
        ],
    }
    doc = validate_document(doc_raw)
    assert doc.rules[0].id == "r1"


def test_any_group():
    doc_raw = {
        "apiVersion": "redibis.io/behavior-policy/v1",
        "kind": "BehaviorPolicy",
        "metadata": {"id": "p", "version": "1.0.0"},
        "appliesTo": {"engine": "pii", "stage": "post_verdict"},
        "rules": [
            {
                "id": "r1",
                "when": {
                    "any": [
                        {"fact": "column.name", "op": "eq", "value": "lac"},
                        {"fact": "column.name", "op": "eq", "value": "tac"},
                    ]
                },
                "effects": [{"effect": "core.verdict.set_entity", "params": {"entity": "NETWORK_ID"}}],
                "reason": "LAC or TAC is a network identifier.",
            }
        ],
    }
    doc = validate_document(doc_raw)
    assert doc.rules[0].id == "r1"


# ---------------------------------------------------------------------------
# Rejection tests
# ---------------------------------------------------------------------------

def test_unknown_field_in_document_rejected():
    bad = dict(MINIMAL_VALID)
    bad["unknown_field"] = "surprise"
    with pytest.raises(ValidationError, match="unknown_field"):
        validate_document(bad)


def test_unknown_field_in_rule_rejected():
    bad = dict(MINIMAL_VALID)
    bad["rules"] = [dict(MINIMAL_VALID["rules"][0], extra_key="bad")]
    with pytest.raises(ValidationError, match="extra_key"):
        validate_document(bad)


def test_missing_reason_rejected():
    bad = dict(MINIMAL_VALID)
    rule = dict(MINIMAL_VALID["rules"][0])
    del rule["reason"]
    bad["rules"] = [rule]
    with pytest.raises(ValidationError):
        validate_document(bad)


def test_empty_reason_rejected():
    bad = dict(MINIMAL_VALID)
    rule = dict(MINIMAL_VALID["rules"][0], reason="")
    bad["rules"] = [rule]
    with pytest.raises(ValidationError, match="reason"):
        validate_document(bad)


def test_whitespace_only_reason_rejected():
    bad = dict(MINIMAL_VALID)
    rule = dict(MINIMAL_VALID["rules"][0], reason="   ")
    bad["rules"] = [rule]
    with pytest.raises(ValidationError, match="reason"):
        validate_document(bad)


def test_oversized_reason_rejected():
    bad = dict(MINIMAL_VALID)
    rule = dict(MINIMAL_VALID["rules"][0], reason="x" * (MAX_REASON_LEN + 1))
    bad["rules"] = [rule]
    with pytest.raises(ValidationError):
        validate_document(bad)


def test_too_many_rules_rejected():
    rule_template = MINIMAL_VALID["rules"][0]
    rules = [dict(rule_template, id=f"rule-{i}") for i in range(MAX_RULES_PER_POLICY + 1)]
    bad = dict(MINIMAL_VALID, rules=rules)
    with pytest.raises(ValidationError):
        validate_document(bad)


def test_duplicate_rule_ids_rejected():
    bad = dict(MINIMAL_VALID)
    bad["rules"] = [
        MINIMAL_VALID["rules"][0],
        MINIMAL_VALID["rules"][0],   # same id
    ]
    with pytest.raises(ValidationError, match="duplicate"):
        validate_document(bad)


def test_wrong_api_version_rejected():
    bad = dict(MINIMAL_VALID, apiVersion="redibis.io/behavior-policy/v2")
    with pytest.raises(ValidationError):
        validate_document(bad)


def test_wrong_kind_rejected():
    bad = dict(MINIMAL_VALID, kind="NotAPolicy")
    with pytest.raises(ValidationError):
        validate_document(bad)


def test_empty_effects_rejected():
    bad = dict(MINIMAL_VALID)
    rule = dict(MINIMAL_VALID["rules"][0], effects=[])
    bad["rules"] = [rule]
    with pytest.raises(ValidationError):
        validate_document(bad)


# ---------------------------------------------------------------------------
# Document size validation
# ---------------------------------------------------------------------------

def test_document_size_ok():
    validate_document_size(b"x" * 100)   # should not raise


def test_document_size_exceeded():
    with pytest.raises(ValueError, match="maximum size"):
        validate_document_size(b"x" * (256 * 1024 + 1))


# ---------------------------------------------------------------------------
# Canonical SHA-256 — formatting-independent
# ---------------------------------------------------------------------------

def test_sha256_stable():
    doc1 = validate_document(MINIMAL_VALID)
    sha1 = canonical_sha256(doc1)
    assert len(sha1) == 64   # hex SHA-256
    # Same content → same SHA
    doc2 = validate_document(MINIMAL_VALID)
    assert canonical_sha256(doc2) == sha1


def test_sha256_changes_on_semantic_change():
    doc1 = validate_document(MINIMAL_VALID)
    sha1 = canonical_sha256(doc1)

    changed = dict(MINIMAL_VALID)
    rule = dict(MINIMAL_VALID["rules"][0], reason="Different reason changes SHA.")
    changed["rules"] = [rule]
    doc2 = validate_document(changed)
    sha2 = canonical_sha256(doc2)
    assert sha1 != sha2


def test_scope_fields_in_applies_to():
    doc_raw = {
        "apiVersion": "redibis.io/behavior-policy/v1",
        "kind": "BehaviorPolicy",
        "metadata": {"id": "scoped", "version": "1.0.0"},
        "appliesTo": {
            "engine": "pii",
            "stage": "post_verdict",
            "environments": ["production"],
            "tables": ["cdr.*"],
            "jurisdictions": ["EG"],
        },
        "rules": [MINIMAL_VALID["rules"][0]],
    }
    doc = validate_document(doc_raw)
    assert doc.appliesTo.environments == ["production"]
    assert doc.appliesTo.tables == ["cdr.*"]
