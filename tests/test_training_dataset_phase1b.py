"""Phase 1b — persist engine baseline on PiiDecision for corrected_engine."""

from __future__ import annotations

import pytest

from redibis.store.contract_store import ContractStore
from redibis.store.pii_decisions import (
    PiiDecision,
    infer_engine_baseline_from_telemetry,
)
from redibis.store.storage_backend import LocalBackend
from redibis.training.exporter import ExportOptions, TrainingDatasetExporter


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "storage"), bucket="contracts")


def _seed_contract(store: ContractStore) -> None:
    store.upsert(
        partial={
            "schema": [{
                "name": "telecom_customers",
                "physicalName": "telecom.customers",
                "properties": [
                    {
                        "name": "msisdn",
                        "logicalType": "string",
                        "classification": "pii_personal",
                        "tags": ["pii"],
                        "entity_type": "PHONE_NUMBER",
                    },
                    {"name": "city", "logicalType": "string"},
                    {"name": "notes", "logicalType": "string"},
                ],
            }],
        },
        table="telecom.customers",
        workflow="pii",
        run_id="seed",
    )


def test_infer_engine_baseline_from_telemetry():
    assert infer_engine_baseline_from_telemetry({
        "entity_type": "PHONE_NUMBER",
        "confidence": 0.91,
        "discovery_engines": ["presidio"],
        "decision_rule": "balanced",
    })["engine_is_pii"] is True

    assert infer_engine_baseline_from_telemetry({
        "entity_type": "",
        "confidence": 0.05,
        "decision_rule": "not_pii",
    })["engine_is_pii"] is False

    assert infer_engine_baseline_from_telemetry({})["engine_is_pii"] is None


def test_set_pii_decision_captures_engine_baseline(store):
    _seed_contract(store)
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {
            "msisdn": {
                "entity_type": "PHONE_NUMBER",
                "confidence": 0.88,
                "discovery_engines": ["gliner"],
                "decision_rule": "balanced",
            },
        },
    )
    # Human agrees with engine (still PII).
    store.set_pii_decision(
        "telecom.customers",
        "msisdn",
        "pii",
        entity_type="PHONE_NUMBER",
        decided_by="steward",
    )
    row = store.get_pii_decisions("telecom.customers")["msisdn"]
    assert row["engine_is_pii"] is True
    assert row["engine_entity_type"] == "PHONE_NUMBER"
    assert abs(float(row["engine_confidence"]) - 0.88) < 1e-9
    assert row["engine_decision_rule"] == "balanced"
    d = PiiDecision.from_dict(row)
    assert d.corrected_engine is False


def test_set_pii_decision_disagreement_is_stored(store):
    _seed_contract(store)
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {
            "city": {
                "entity_type": "LOCATION",
                "confidence": 0.77,
                "discovery_engines": ["presidio"],
                "decision_rule": "lenient",
            },
        },
    )
    # Engine said PII; steward demotes.
    store.set_pii_decision(
        "telecom.customers",
        "city",
        "not_pii",
        decided_by="steward",
    )
    row = store.get_pii_decisions("telecom.customers")["city"]
    assert row["engine_is_pii"] is True
    assert PiiDecision.from_dict(row).corrected_engine is True


def test_exporter_prefers_stored_baseline(store):
    _seed_contract(store)
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {
            "city": {
                "entity_type": "LOCATION",
                "confidence": 0.9,
                "discovery_engines": ["presidio"],
                "decision_rule": "lenient",
            },
        },
    )
    store.set_pii_decision("telecom.customers", "city", "not_pii", decided_by="s")

    # Poison join-time telemetry so a derived path would disagree differently.
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {
            "city": {
                "entity_type": "",
                "confidence": 0.01,
                "discovery_engines": [],
                "decision_rule": "not_pii",
            },
        },
    )

    ds = TrainingDatasetExporter(store).export(ExportOptions())
    city = next(e for e in ds.examples if e.features["column_name"] == "city")
    assert city.label["corrected_engine"] is True
    assert city.label["corrected_engine_derived"] is False
    assert city.provenance["engine_baseline"] is True
    assert (ds.meta.get("audit") or {}).get("stored_corrected_engine", 0) >= 1


def test_exporter_falls_back_when_baseline_absent(store):
    _seed_contract(store)
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {
            "notes": {
                "entity_type": "PERSON",
                "confidence": 0.8,
                "discovery_engines": ["gliner"],
                "decision_rule": "balanced",
            },
        },
    )
    # Legacy overlay without engine_* fields.
    store.pii_decisions.set(
        "telecom.customers",
        PiiDecision(column="notes", status="not_pii", decided_by="legacy"),
    )
    # Manually strip engine keys if asdict wrote them as None — write raw.
    decisions = store.pii_decisions.get("telecom.customers")
    decisions["notes"] = {
        "column": "notes",
        "status": "not_pii",
        "decided_by": "legacy",
        "payload": {},
        "run_id": "",
        "ts": "2026-01-01T00:00:00Z",
    }
    store.pii_decisions._save("telecom.customers", decisions)

    ds = TrainingDatasetExporter(store).export(ExportOptions())
    notes = next(e for e in ds.examples if e.features["column_name"] == "notes")
    assert notes.label["corrected_engine"] is True
    assert notes.label["corrected_engine_derived"] is True


def test_explicit_engine_kwargs_override_telemetry(store):
    _seed_contract(store)
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {"msisdn": {"entity_type": "PHONE_NUMBER", "confidence": 0.99}},
    )
    store.set_pii_decision(
        "telecom.customers",
        "msisdn",
        "pii",
        entity_type="PHONE_NUMBER",
        engine_is_pii=False,
        engine_confidence=0.1,
        decided_by="test",
    )
    row = store.get_pii_decisions("telecom.customers")["msisdn"]
    assert row["engine_is_pii"] is False
    assert abs(float(row["engine_confidence"]) - 0.1) < 1e-9
    assert PiiDecision.from_dict(row).corrected_engine is True
