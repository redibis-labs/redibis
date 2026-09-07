"""Tests for format DQ rules (match_regex → portable ODCS) and masking defaults."""

from redibis.contracts.mapper import ge_expectation_to_odcs
from redibis.contracts.rules import extract_rules, odcs_to_ge
from redibis.contracts.masking_policy import build_masking_policy, resolve_role_config
from redibis.models import suggest_masking_default


class _Exp:
    """Minimal stand-in for a GE ExpectationConfiguration."""
    def __init__(self, expectation_type, kwargs, meta=None):
        self.expectation_type = expectation_type
        self.kwargs = kwargs
        self.meta = meta or {}


# ── match_regex → portable {rule: regex, pattern} ──────────────────────────

def test_match_regex_maps_to_portable_regex_rule():
    exp = _Exp("expect_column_values_to_match_regex",
               {"column": "a_party_msisdn", "regex": r"^\+?[1-9]\d{1,14}$"})
    dq = ge_expectation_to_odcs(exp)
    assert dq["rule"] == "regex"
    assert dq["pattern"] == r"^\+?[1-9]\d{1,14}$"
    # not the GE-only escape hatch
    assert dq.get("engine") != "greatExpectations"


def test_match_regex_with_mostly_records_threshold():
    exp = _Exp("expect_column_values_to_match_regex",
               {"column": "imsi", "regex": r"^\d{15}$", "mostly": 0.99})
    dq = ge_expectation_to_odcs(exp)
    assert dq["rule"] == "regex"
    assert dq["mustBeGreaterOrEqualTo"] == 99.0 and dq["unit"] == "percent"


def test_regex_rule_round_trips_back_to_ge():
    contract = {
        "schema": [{"name": "t", "properties": [{
            "name": "a_party_msisdn",
            "quality": [{"rule": "regex", "pattern": r"^\+?[1-9]\d{1,14}$"}],
        }]}],
    }
    rules = extract_rules(contract)
    assert any(r.type == "regex" for r in rules)
    suite = odcs_to_ge(contract)
    types = [e["expectation_type"] for e in suite["expectations"]]
    assert "expect_column_values_to_match_regex" in types


# ── suggest_masking_default ────────────────────────────────────────────────

def test_ids_get_reversible_fpe():
    for ent in ("NATIONAL_ID", "PASSPORT", "CREDIT_CARD", "IBAN_CODE"):
        pol = suggest_masking_default(ent)
        assert pol["default"] == "fpe" and pol["reversible"] is True


def test_contactish_entities_get_fake():
    for ent in ("EMAIL", "PHONE_NUMBER", "PERSON", "MSISDN"):
        pol = suggest_masking_default(ent)
        assert pol["default"] == "fake" and pol["reversible"] is False


def test_detected_unknown_entity_pseudonymizes():
    pol = suggest_masking_default(None, detected=True)
    assert pol["default"] == "hash" and pol["reversible"] is False


def test_not_detected_is_passthrough():
    pol = suggest_masking_default("EMAIL", detected=False)
    assert pol["default"] == "passthrough"


def test_known_non_pii_entities_stay_passthrough_even_when_detected():
    """ORGANIZATION and its non-PII siblings must never be masked, regardless
    of the raw 'detected' evidence flag -- they classify as internal, not
    personal data (see redibis.models.NON_PII_ENTITIES)."""
    for ent in ("ORGANIZATION", "ORG", "SWIFT_BIC", "NETWORK_ID", "CELL_ID", "CONTENT_HASH", "URL"):
        pol = suggest_masking_default(ent, detected=True)
        assert pol["default"] == "passthrough", ent
        assert pol["reversible"] is False


def test_employer_gets_indirect_fpe_masking():
    """Unlike a bare ORGANIZATION reference, EMPLOYER is pii_indirect."""
    pol = suggest_masking_default("EMPLOYER", detected=True)
    assert pol["default"] == "fpe" and pol["reversible"] is True


# ── role-based masking policy ──────────────────────────────────────────────

def test_default_roles_admin_and_data_science():
    cfg = resolve_role_config()
    assert set(cfg.keys()) == {"admin", "data_science"}


def test_admin_sees_raw_data_science_masked_for_personal():
    pol = build_masking_policy("EMAIL", detected=True)  # pii_personal
    assert pol["default"] == "fake"
    assert pol["roles"]["admin"] == "none"            # full access
    assert pol["roles"]["data_science"] == "fake"     # "default" → column strategy


def test_sensitive_forces_hash_for_data_science():
    pol = build_masking_policy("NATIONAL_ID", detected=True)  # pii_sensitive
    assert pol["default"] == "fpe"
    assert pol["roles"]["admin"] == "none"
    assert pol["roles"]["data_science"] == "hash"     # tighter than the fpe default


def test_indirect_gets_fpe_default():
    pol = build_masking_policy("SESSION_ID", detected=True)
    assert pol["default"] == "fpe"
    assert pol["reversible"] is True
    assert pol["roles"]["data_science"] == "fpe"


def test_security_sensitive_gets_redact():
    pol = build_masking_policy("PASSWORD_HASH", detected=True)
    assert pol["default"] == "redact"
    assert pol["reversible"] is False
    assert pol["roles"]["data_science"] == "redact"


def test_non_pii_returns_no_policy():
    assert build_masking_policy("EMAIL", detected=False) is None


def test_settings_dict_overrides_on_the_fly():
    override = {"roles": {
        "admin": {"pii_personal": "hash", "pii_sensitive": "hash"},
        "data_science": {"pii_personal": "redact", "pii_sensitive": "redact"},
        "auditor": {"pii_personal": "none", "pii_sensitive": "none"},
    }}
    cfg = resolve_role_config(override)
    pol = build_masking_policy("EMAIL", detected=True, role_config=cfg)
    assert pol["roles"]["admin"] == "hash"
    assert pol["roles"]["data_science"] == "redact"
    assert pol["roles"]["auditor"] == "none"          # new role added on the fly
