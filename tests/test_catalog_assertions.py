"""Assertion builder + hash/serde round-trips — no network."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from redibis.services.catalog.assertions import (
    AssertionKey,
    CoverageRecord,
    CoverageStatus,
    Facet,
    LedgerEntry,
    Suppression,
    assertion_key_from_dict,
    assertion_key_to_dict,
    build_assertions_from_contract,
    ledger_entry_from_dict,
    ledger_entry_to_dict,
    suppression_from_dict,
    suppression_to_dict,
    value_hash,
)
from redibis.services.catalog.mapping import load_contract_file

FIXTURE = Path(__file__).parent / "fixtures" / "catalog_telecom_customers.yaml"
ASSET = "hive_prod.telecom.public.customers"
TABLE = "telecom.customers"


def test_build_assertions_from_fixture_contract():
    contract = load_contract_file(FIXTURE)
    assertions, coverage = build_assertions_from_contract(
        contract,
        TABLE,
        asset_fqn=ASSET,
        scan_id="scan-fixture",
        include_tags=True,
        include_glossary=True,
    )

    assert assertions, "expected assertions from fixture"
    # Sample values never land in evidence_ref; defaulted tag confidence uses
    # the sentinel "contract-boolean".
    assert all(
        a.evidence_ref in ("", "contract-boolean") for a in assertions
    ), "never embed samples"
    tag_assertions = [
        a for a in assertions
        if a.key.facet in (Facet.PII_TAG, Facet.ENTITY_TAG, Facet.POLICY_TAG)
    ]
    assert tag_assertions
    assert all(a.evidence_ref == "contract-boolean" for a in tag_assertions)

    # PII columns msisdn / national_id should produce PII + entity tags
    pii_keys = {
        (a.key.column_path, a.key.facet, a.key.value_key)
        for a in assertions
        if a.key.facet in (Facet.PII_TAG, Facet.ENTITY_TAG)
    }
    assert ("msisdn", Facet.PII_TAG, "PII.NonSensitive") in pii_keys or (
        "msisdn",
        Facet.PII_TAG,
        "PII.Sensitive",
    ) in pii_keys
    assert any(
        col == "msisdn" and facet is Facet.ENTITY_TAG and vk.startswith("Redibis.")
        for col, facet, vk in pii_keys
    )
    assert any(
        col == "national_id" and facet is Facet.PII_TAG
        for col, facet, vk in pii_keys
    )

    # Descriptions + display names
    facets_present = {a.key.facet for a in assertions}
    assert Facet.TABLE_DESCRIPTION in facets_present
    assert Facet.COLUMN_DESCRIPTION in facets_present
    assert Facet.COLUMN_DISPLAY_NAME in facets_present

    # Glossary for columns with businessName
    gloss = [a for a in assertions if a.key.facet is Facet.GLOSSARY_LINK]
    assert gloss
    assert any(a.key.value_key == "Redibis_telecom.msisdn" for a in gloss)

    # Coverage marks EVALUATED for tag facets on every column (incl. non-PII status)
    columns = {"msisdn", "national_id", "status"}
    for col in columns:
        for facet in (Facet.PII_TAG, Facet.ENTITY_TAG, Facet.POLICY_TAG):
            recs = [
                c
                for c in coverage
                if c.column_path == col
                and c.facet is facet
                and c.status is CoverageStatus.EVALUATED
            ]
            assert recs, f"missing EVALUATED coverage for {col}/{facet.value}"


def test_value_hash_stable_and_round_trip_helpers():
    assert value_hash({"b": 1, "a": 2}) == value_hash({"a": 2, "b": 1})
    assert value_hash("PII.Sensitive") != value_hash("PII.NonSensitive")

    key = AssertionKey(
        asset_fqn=ASSET,
        column_path="msisdn",
        facet=Facet.PII_TAG,
        value_key="PII.NonSensitive",
    )
    assert assertion_key_from_dict(assertion_key_to_dict(key)) == key

    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    entry = LedgerEntry(
        backend="openmetadata",
        key=key,
        value_hash=value_hash("PII.NonSensitive"),
        scan_id="s1",
        published_at=now,
        state="confirmed",
        negative_streak=0,
    )
    round_entry = ledger_entry_from_dict(ledger_entry_to_dict(entry))
    assert round_entry.backend == entry.backend
    assert round_entry.key == entry.key
    assert round_entry.value_hash == entry.value_hash
    assert round_entry.state == entry.state

    sup = Suppression(
        key=key,
        actor="alice",
        reason="rejected",
        created_at=now,
        expires_at=None,
        source="cli",
    )
    round_sup = suppression_from_dict(suppression_to_dict(sup))
    assert round_sup.key == key
    assert round_sup.actor == "alice"
    assert round_sup.source == "cli"


def test_coverage_evaluated_for_all_columns_even_without_tags():
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    _assertions, coverage = build_assertions_from_contract(
        contract,
        TABLE,
        asset_fqn=ASSET,
        scan_id="scan-2",
        include_tags=True,
    )
    # status has lifecycle tag → Redibis.lifecycle entity tag, but PII_TAG still
    # covered as EVALUATED even if no PII.* assertion exists.
    status_pii_cov = [
        c
        for c in coverage
        if c.column_path == "status" and c.facet is Facet.PII_TAG
    ]
    assert status_pii_cov
    assert all(c.status is CoverageStatus.EVALUATED for c in status_pii_cov)

    status_pii_assertions = [
        a
        for a in _assertions
        if a.key.column_path == "status" and a.key.facet is Facet.PII_TAG
    ]
    assert status_pii_assertions == []


def test_scan_coverage_used_verbatim():
    """When scan_coverage is provided, use it verbatim (no contract-derived coverage)."""
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    scan_cov = [
        CoverageRecord(
            scan_id="scan-3",
            asset_fqn=ASSET,
            column_path="msisdn",
            facet=Facet.PII_TAG,
            status=CoverageStatus.SKIPPED,
            reason="column not in sample",
        )
    ]
    _assertions, coverage = build_assertions_from_contract(
        contract,
        TABLE,
        asset_fqn=ASSET,
        scan_id="scan-3",
        scan_coverage=scan_cov,
    )
    assert coverage is not scan_cov  # copied list
    assert len(coverage) == 1
    assert coverage[0].status is CoverageStatus.SKIPPED
    assert coverage[0].reason == "column not in sample"
    assert coverage[0].column_path == "msisdn"


def test_column_telemetry_confidence_feeds_suggested_tier():
    """Telemetry confidence < confirm_threshold → mid-confidence tag assertions."""
    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assertions, _coverage = build_assertions_from_contract(
        contract,
        TABLE,
        asset_fqn=ASSET,
        scan_id="scan-tel",
        column_telemetry={"msisdn": {"confidence": 0.72}},
        publish_threshold=0.60,
    )
    msisdn_tags = [
        a for a in assertions
        if a.key.column_path == "msisdn"
        and a.key.facet in (Facet.PII_TAG, Facet.ENTITY_TAG)
    ]
    assert msisdn_tags
    assert all(abs(a.confidence - 0.72) < 1e-9 for a in msisdn_tags)
    assert all(a.evidence_ref == "" for a in msisdn_tags)


def test_metadata_store_confidence_feeds_suggested_tier():
    """metadata_store column telemetry → Suggested-tier confidence (0.72)."""
    class _FakeMeta:
        def get_column_telemetry(self, table):
            assert table == TABLE
            return {"msisdn": {"confidence": 0.72}}

    contract = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    assertions, _coverage = build_assertions_from_contract(
        contract,
        TABLE,
        asset_fqn=ASSET,
        scan_id="scan-meta",
        metadata_store=_FakeMeta(),
        publish_threshold=0.60,
    )
    msisdn_tags = [
        a for a in assertions
        if a.key.column_path == "msisdn"
        and a.key.facet in (Facet.PII_TAG, Facet.ENTITY_TAG)
    ]
    assert msisdn_tags
    assert all(abs(a.confidence - 0.72) < 1e-9 for a in msisdn_tags)
    # confirm_threshold default in ReconcilePolicy is 0.85 → Suggested path
    assert all(a.confidence < 0.85 for a in msisdn_tags)
