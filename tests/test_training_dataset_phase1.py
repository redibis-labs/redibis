"""Phase 1 — TrainingDataset export, portable assert, ArtifactResidencyGate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.pack.errors import PackValidationError
from redibis.pack.models import PackManifest
from redibis.pack.writer import build_pack_files, write_pack
from redibis.store.contract_store import ContractStore
from redibis.store.pii_decisions import PiiDecision
from redibis.store.storage_backend import LocalBackend
from redibis.training.dataset import TrainingDataset, TrainingExample
from redibis.training.exporter import ExportOptions, TrainingDatasetExporter
from redibis.training.portable_assert import PortableLeakError, assert_portable_row
from redibis.training.residency import (
    ArtifactResidencyError,
    ArtifactResidencyGate,
    assert_artifact_portable,
)


@pytest.fixture
def store(tmp_path):
    return ContractStore(LocalBackend(tmp_path / "storage"), bucket="contracts")


def _seed(store: ContractStore) -> None:
    store.upsert(
        partial={
            "schema": [{
                "name": "telecom_customers",
                "physicalName": "telecom.customers",
                "properties": [
                    {
                        "name": "msisdn",
                        "logicalType": "string",
                        "physicalType": "varchar",
                        "classification": "pii_personal",
                        "tags": ["pii", "gdpr_personal_data"],
                        "entity_type": "PHONE_NUMBER",
                        "description": "Customer MSISDN",
                    },
                    {"name": "city", "logicalType": "string"},
                ],
            }],
        },
        table="telecom.customers",
        workflow="pii",
        run_id="seed",
    )
    store.metadata.merge_column_telemetry(
        "telecom.customers",
        {
            "msisdn": {
                "entity_type": "PHONE_NUMBER",
                "confidence": 0.91,
                "discovery_engines": ["presidio", "gliner"],
                "decision_rule": "balanced",
                "gliner_score": 0.71,
                "gliner_label": "phone number",
                "regex_score": 0.9,
                "regex_hits": ["e164_phone"],
            },
            "city": {
                "entity_type": "",
                "confidence": 0.05,
                "discovery_engines": [],
                "decision_rule": "not_pii",
            },
        },
    )
    store.set_pii_decision(
        "telecom.customers",
        "msisdn",
        "pii",
        entity_type="PHONE_NUMBER",
        payload={
            "classification": "pii_personal",
            "tags": ["pii"],
            "entity_type": "PHONE_NUMBER",
        },
        decided_by="steward",
    )
    # Human corrects city that engine correctly said not_pii — still a decision.
    store.set_pii_decision(
        "telecom.customers",
        "city",
        "not_pii",
        decided_by="steward",
    )


def test_assert_portable_row_rejects_samples():
    row = {
        "features": {"column_name": "msisdn"},
        "label": {"is_pii": True},
        "residency": "portable",
        "samples": ["+201001234567"],
    }
    with pytest.raises(PortableLeakError):
        assert_portable_row(row)


def test_assert_portable_row_rejects_phone_looking_string():
    row = {
        "features": {"column_name": "msisdn", "note": "call +201001234567 now"},
        "label": {"is_pii": True},
        "residency": "portable",
    }
    with pytest.raises(PortableLeakError):
        assert_portable_row(row)


def test_assert_portable_row_accepts_clean_features():
    row = {
        "features": {
            "column_name": "msisdn",
            "name_tokens": ["msisdn"],
            "format_signatures": [r"^\+?\d{11,12}$"],
            "regex_hits": ["e164_phone"],
        },
        "label": {"is_pii": True, "entity_type": "PHONE_NUMBER"},
        "provenance": {"context_hash": "sha256:abc"},
        "residency": "portable",
    }
    assert_portable_row(row)  # no raise


def test_exporter_portable_join(store, tmp_path):
    _seed(store)
    exporter = TrainingDatasetExporter(store)
    ds = exporter.export(ExportOptions())
    assert ds.residency == "portable"
    assert len(ds.examples) >= 2
    names = {e.features["column_name"] for e in ds.examples}
    assert "msisdn" in names
    assert "city" in names
    msisdn = next(e for e in ds.examples if e.features["column_name"] == "msisdn")
    assert msisdn.label["is_pii"] is True
    assert msisdn.label["source"] == "human_decision"
    assert msisdn.samples is None
    # Phase 1b: baseline captured at decision time → not join-derived.
    assert msisdn.label.get("corrected_engine_derived") is False
    for e in ds.examples:
        assert_portable_row(e.to_dict())

    out = tmp_path / "train.jsonl"
    ds.write_jsonl(out)
    loaded = TrainingDataset.from_jsonl(out)
    assert loaded.checksum == ds.checksum
    assert loaded.stats()["row_count"] == len(ds.examples)


def test_exporter_include_samples_forces_local(store):
    _seed(store)
    exporter = TrainingDatasetExporter(store)
    ds = exporter.export(
        ExportOptions(
            include_samples=True,
            samples_by_column={
                "msisdn": ["+201001234567", "+201112223344"],
                "telecom.customers.city": ["Cairo", "Giza"],
            },
        )
    )
    assert ds.residency == "local"
    msisdn = next(e for e in ds.examples if e.features["column_name"] == "msisdn")
    assert msisdn.samples
    assert msisdn.residency == "local"
    assert msisdn.contains_raw_values is True
    with pytest.raises(PortableLeakError):
        assert_portable_row(msisdn.to_dict())


def test_exporter_dedupe_by_context_hash(store):
    _seed(store)
    exporter = TrainingDatasetExporter(store)
    ds1 = exporter.export()
    ds2 = exporter.export()
    assert len(ds1.examples) == len(ds2.examples)
    hashes = [e.provenance["context_hash"] for e in ds1.examples]
    assert len(hashes) == len(set(hashes))


def test_artifact_residency_gate_refuses_local_jsonl(tmp_path):
    path = tmp_path / "local.jsonl"
    TrainingDataset(
        examples=[
            TrainingExample(
                features={"column_name": "msisdn"},
                label={"is_pii": True},
                residency="local",
                samples=["+201001234567"],
                contains_raw_values=True,
            )
        ],
        residency="local",
    ).write_jsonl(path)
    with pytest.raises(ArtifactResidencyError):
        ArtifactResidencyGate.check_path(path)
    with pytest.raises(ArtifactResidencyError):
        assert_artifact_portable(path=path)


def test_pack_build_refuses_local_corpus_section():
    """build_pack_files must refuse a local training corpus member."""
    manifest = PackManifest.model_validate({
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {"id": "test-pack", "version": "0.0.1"},
        "mode": "overlay",
        "contents": {"config": True},
        "requires": {"redibis": ">=0.0.0"},
    })
    sections = {
        "config/redibis.yaml": {"scan_types": ["pii"]},
        "assets/training/local.json": {
            "features": {"column_name": "x"},
            "label": {"is_pii": True},
            "residency": "local",
            "samples": ["secret-value-here"],
            "contains_raw_values": True,
        },
    }
    with pytest.raises((ArtifactResidencyError, PackValidationError)):
        build_pack_files(manifest, sections, check_registries=False)


def test_pack_build_allows_portable_corpus_section(tmp_path):
    manifest = PackManifest.model_validate({
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {"id": "test-pack", "version": "0.0.1"},
        "mode": "overlay",
        "contents": {"config": True},
        "requires": {"redibis": ">=0.0.0"},
    })
    row = {
        "features": {"column_name": "msisdn", "name_tokens": ["msisdn"]},
        "label": {"is_pii": True, "entity_type": "PHONE_NUMBER"},
        "residency": "portable",
        "provenance": {"context_hash": "sha256:abc"},
    }
    sections = {
        "config/redibis.yaml": {"scan_types": ["pii"]},
        "assets/training/portable.json": row,
    }
    files, sha = build_pack_files(manifest, sections, check_registries=False)
    assert sha
    assert "assets/training/portable.json" in files
    out = tmp_path / "ok.rdbpack"
    write_pack(out, manifest, sections, check_registries=False)
    assert out.is_file()


def test_gate_refuses_raw_trained_model_card():
    card = {
        "model_id": "learned-cls-v1",
        "residency": "local",
        "raw_trained": True,
        "metrics": {"f1": 0.9},
    }
    with pytest.raises(ArtifactResidencyError):
        ArtifactResidencyGate.check_payload(card, label="model-card")


def test_cli_dataset_help():
    from redibis.cli.main import main
    import sys

    old = sys.argv
    try:
        sys.argv = ["redibis", "dataset", "--help"]
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
    finally:
        sys.argv = old
