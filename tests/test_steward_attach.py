"""Attach steward verdict files at scan / enrich time."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from redibis.contracts.lifecycle import apply_llm_classification_decisions
from redibis.review.artifacts import export_artifact_bundle
from redibis.review.steward_attach import (
    STEWARD_VERDICT_ACTOR_PREFIX,
    attach_steward_verdict_path,
    load_steward_verdict_path,
    locked_human_pii_columns,
)
from redibis.services.steward_review_service import StewardReviewService
from redibis.store.contract_store import ContractStore
from redibis.store.generation_ledger import Generation
from redibis.store.pii_decisions import PiiDecision
from redibis.store.storage_backend import LocalBackend


TABLE = "db.customers"


def _contract():
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "id": "customers-contract",
        "status": "active",
        "name": "customers_contract",
        "database_name": "db",
        "table_name": "customers",
        "version": "1.0.0",
        "schema": [{
            "name": "customers",
            "description": "customers",
            "properties": [
                {"name": "id", "logicalType": "integer"},
                {"name": "email", "logicalType": "string",
                 "classification": "pii_personal", "tags": ["pii"],
                 "description": "customer email"},
                {"name": "notes", "logicalType": "string", "description": "free text"},
            ],
        }],
    }


def _a1(table=TABLE):
    return {
        "kind": "redibis.steward_verdicts",
        "schema_version": "1.0",
        "table": table,
        "exported_at": "2026-01-01T00:00:00+00:00",
        "exporter": "ada",
        "entries": [
            {
                "table": table,
                "column": "email",
                "field": "pii",
                "decision": "accept",
                "status": "pii",
                "value": {"is_pii": True, "entity_type": "EMAIL_ADDRESS"},
                "fingerprint_key": "",
            },
            {
                "table": table,
                "column": "notes",
                "field": "pii",
                "decision": "needs_review",
                "status": "needs_review",
                "value": {"is_pii": False},
            },
            {
                "table": "other.table",
                "column": "ssn",
                "field": "pii",
                "decision": "accept",
                "status": "pii",
                "value": {"is_pii": True},
            },
            {
                "table": table,
                "column": "missing_col",
                "field": "pii",
                "decision": "accept",
                "status": "pii",
                "value": {"is_pii": True},
            },
        ],
    }


def test_load_file_and_directory(tmp_path: Path):
    one = tmp_path / "a1.json"
    one.write_text(json.dumps(_a1()), encoding="utf-8")
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "one.json").write_text(json.dumps(_a1()), encoding="utf-8")
    (batch / "two.json").write_text(json.dumps({
        "kind": "redibis.verdict_package",
        "schema_version": "1.0",
        "tables": [TABLE],
        "entries": [{
            "table": TABLE, "column": "id", "status": "not_pii",
            "fingerprint_key": "",
        }],
    }), encoding="utf-8")
    assert len(load_steward_verdict_path(one)) == 1
    loaded = load_steward_verdict_path(batch)
    assert len(loaded) == 2


def test_attach_locks_human_verified_skips_rest():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table=TABLE, workflow="manual", run_id="r1")
        path = Path(tmp) / "a1.json"
        path.write_text(json.dumps(_a1()), encoding="utf-8")
        report = attach_steward_verdict_path(store, TABLE, path, actor="ada")
        assert "email" in report.applied
        assert "notes" in report.skipped_needs_review
        assert "ssn" in report.skipped_other_table
        assert "missing_col" in report.skipped_schema
        decisions = store.get_pii_decisions(TABLE)
        assert decisions["email"]["status"] == "pii"
        assert decisions["email"]["decided_by"].startswith(STEWARD_VERDICT_ACTOR_PREFIX)
        assert "notes" not in decisions
        locked = locked_human_pii_columns(store, TABLE)
        assert "email" in locked
        assert "notes" not in locked


def test_apply_llm_skips_locked_steward_columns():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table=TABLE, workflow="manual", run_id="r1")
        store.pii_decisions.set(TABLE, PiiDecision(
            column="email", status="not_pii",
            decided_by=f"{STEWARD_VERDICT_ACTOR_PREFIX}cli",
        ))
        applied = apply_llm_classification_decisions(
            store,
            TABLE,
            [
                {"column": "email", "status": "pii", "entity_type": "EMAIL_ADDRESS"},
                {"column": "notes", "status": "not_pii"},
            ],
            enriched_by="llm",
            run_id="enrich_1",
        )
        assert "email" not in applied
        assert "notes" in applied
        assert store.get_pii_decisions(TABLE)["email"]["status"] == "not_pii"
        assert store.get_pii_decisions(TABLE)["notes"]["status"] == "not_pii"


def test_export_artifact_bundle_includes_current_a1():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table=TABLE, workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        for col in ("id", "email", "notes"):
            store.generation_ledger.append(TABLE, col, [
                Generation(field="pii", source="regex",
                           value={"is_pii": col == "email"},
                           confidence=0.8, run_id="r1", ts="t"),
            ])
            svc.decide(TABLE, col, "pii", {
                "decision": "accept" if col == "email" else "no_action",
                "chosen_source": "regex" if col == "email" else "human",
                "rationale_code": "engine_correct",
                "value": {"is_pii": col == "email"},
            }, actor="ada")
        current = svc.export_verdicts(TABLE, actor="ada")
        raw = export_artifact_bundle(store, TABLE, current_verdicts=current)
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as zf:
            names = zf.namelist()
        assert any(n.endswith("current/steward_verdicts.json") for n in names)
        assert any(n.endswith("current/contract.yaml") for n in names)


@pytest.mark.parametrize("argv", [
    ["scan", "--help"],
    ["enrich", "--help"],
    ["deep-scan", "--help"],
    ["profile", "--help"],
    ["quality", "--help"],
])
def test_cli_help_lists_steward_verdict_path(argv, capsys):
    from redibis.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--steward-verdict-path" in out
