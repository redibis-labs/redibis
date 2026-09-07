"""Tests for verdict-package preview/import/replay (plan §6, `verdict-replay`).

Covers ``EvidenceReviewService.preview_verdicts``/``import_verdicts``, the
REST preview/import/replay/import-log endpoints, side-effect-free replay,
and the ``redibis verdict preview|import`` CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.review.fingerprint import fingerprint_from_evidence_column
from redibis.review.verdict_package import VerdictEntry, VerdictPackage
from redibis.services.evidence_review_service import EvidenceReviewService
from redibis.store.contract_store import ContractStore
from redibis.store.pii_decisions import PiiDecision
from redibis.store.review_store import ReviewStore
from redibis.store.storage_backend import LocalBackend

TABLE = "telecom.customers"


def _bundle(run_id: str = "run-1") -> dict:
    return {
        "schema_version": "2.0",
        "table": {"name": TABLE, "run_id": run_id},
        "columns": {
            "msisdn": {
                "logical_type": "string",
                "physical_type": "string",
                "profile": {"format_masks": {"top_masks": [{"mask": "DDDD"}]}},
                "pii_verdict": {"detected": True, "entity_type": "PHONE_NUMBER", "confidence": 0.92},
            },
            "city": {
                "logical_type": "string",
                "pii_verdict": {"detected": False, "confidence": 0.1},
            },
        },
    }


def _fp_entry(bundle: dict, column: str, *, status: str, **overrides) -> VerdictEntry:
    fp = fingerprint_from_evidence_column(column, bundle["columns"][column])
    kwargs = dict(
        table=TABLE,
        column=column,
        status=status,
        fingerprint_key=fp.fingerprint_key,
        logical_type=fp.logical_type,
        physical_type=fp.physical_type,
        format_signature=fp.format_signature,
        name_normalized=fp.name_normalized,
    )
    kwargs.update(overrides)
    return VerdictEntry(**kwargs)


def _store_with_contract(tmp_path: Path) -> ContractStore:
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [
                {"name": "msisdn", "logicalType": "string", "physicalType": "string"},
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="test", run_id="r0")
    return store


def _svc(store: ContractStore) -> EvidenceReviewService:
    return EvidenceReviewService(
        store, review_store=ReviewStore(store.backend, store.bucket), pii_store=store.pii_decisions,
    )


# ── preview_verdicts classification ──────────────────────────────────────

def test_preview_classifies_matching(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    preview = _svc(store).preview_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]))
    assert [r["column"] for r in preview["matching"]] == ["msisdn"]
    assert preview["summary"]["matching"] == 1


def test_preview_classifies_stale_on_fingerprint_mismatch(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii", fingerprint_key="mismatched|key")
    preview = _svc(store).preview_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]))
    assert [r["column"] for r in preview["stale"]] == ["msisdn"]


def test_preview_classifies_missing_column(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = VerdictEntry(table=TABLE, column="does_not_exist", status="not_pii", fingerprint_key="a|b|c")
    preview = _svc(store).preview_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]))
    assert [r["column"] for r in preview["missing"]] == ["does_not_exist"]


def test_preview_classifies_invalid_without_fingerprint(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = VerdictEntry(table=TABLE, column="msisdn", status="not_pii", fingerprint_key="")
    preview = _svc(store).preview_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]))
    assert [r["column"] for r in preview["invalid"]] == ["msisdn"]


def test_preview_classifies_conflicting_with_active_local_decision(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    store.set_pii_decision(TABLE, "msisdn", "pii", entity_type="PHONE_NUMBER", decided_by="alice")
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    preview = _svc(store).preview_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]))
    assert [r["column"] for r in preview["conflicting"]] == ["msisdn"]


def test_preview_never_writes(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    _svc(store).preview_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]))
    assert store.get_pii_decisions(TABLE) == {}


# ── import_verdicts promotion + audit ────────────────────────────────────

def test_import_promotes_matching_entries(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii", reason="confirmed false positive")
    result = _svc(store).import_verdicts(
        TABLE, bundle, VerdictPackage(entries=[entry]), actor="alice", reason="batch import",
    )
    assert result["applied"] == ["msisdn"]
    decisions = store.get_pii_decisions(TABLE)
    assert decisions["msisdn"]["status"] == "not_pii"
    assert decisions["msisdn"]["decided_by"] == "import:alice"


def test_import_skips_stale_and_missing_regardless_of_policy(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    stale_entry = _fp_entry(bundle, "msisdn", status="not_pii", fingerprint_key="mismatched|key")
    missing_entry = VerdictEntry(table=TABLE, column="ghost", status="not_pii", fingerprint_key="a|b|c")
    pkg = VerdictPackage(entries=[stale_entry, missing_entry])
    result = _svc(store).import_verdicts(
        TABLE, bundle, pkg, actor="alice", reason="x", merge_policy="overwrite_conflicts",
    )
    assert result["applied"] == []
    assert result["stale_skipped"] == ["msisdn"]
    assert result["missing_skipped"] == ["ghost"]
    assert store.get_pii_decisions(TABLE) == {}


def test_import_matching_only_skips_conflicts(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    store.set_pii_decision(TABLE, "msisdn", "pii", entity_type="PHONE_NUMBER", decided_by="alice")
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    result = _svc(store).import_verdicts(
        TABLE, bundle, VerdictPackage(entries=[entry]), actor="bob", reason="reconciling",
        merge_policy="matching_only",
    )
    assert result["applied"] == []
    assert result["conflicting_skipped"] == ["msisdn"]
    assert store.get_pii_decisions(TABLE)["msisdn"]["status"] == "pii"


def test_import_overwrite_conflicts_promotes_disagreement(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    store.set_pii_decision(TABLE, "msisdn", "pii", entity_type="PHONE_NUMBER", decided_by="alice")
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    result = _svc(store).import_verdicts(
        TABLE, bundle, VerdictPackage(entries=[entry]), actor="bob", reason="steward override",
        merge_policy="overwrite_conflicts",
    )
    assert result["applied"] == ["msisdn"]
    assert store.get_pii_decisions(TABLE)["msisdn"]["status"] == "not_pii"
    assert store.get_pii_decisions(TABLE)["msisdn"]["decided_by"] == "import:bob"


def test_import_requires_actor_and_reason(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    with pytest.raises(ValueError):
        _svc(store).import_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]), actor="", reason="x")
    with pytest.raises(ValueError):
        _svc(store).import_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]), actor="alice", reason="")


def test_import_rejects_unknown_merge_policy(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    with pytest.raises(ValueError):
        _svc(store).import_verdicts(
            TABLE, bundle, VerdictPackage(entries=[entry]), actor="a", reason="r", merge_policy="bogus",
        )


def test_import_records_audit_event_in_verdict_import_log(tmp_path: Path):
    store = _store_with_contract(tmp_path)
    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    _svc(store).import_verdicts(TABLE, bundle, VerdictPackage(entries=[entry]), actor="alice", reason="ok")
    log = store.verdict_import_log.list(TABLE)
    assert len(log) == 1
    assert log[0]["actor"] == "alice"
    assert log[0]["applied"] == ["msisdn"]


# ── REST: preview / import / replay / import-log ─────────────────────────

def _write_session_run(scan_root: Path, run_id: str) -> Path:
    session_dir = scan_root / f"sess-{run_id}"
    run_dir = session_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    bundle = _bundle(run_id=run_id)
    (run_dir / "evidence_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    (session_dir / "session.json").write_text(json.dumps({"table_name": TABLE}), encoding="utf-8")
    return run_dir


@pytest.fixture
def verdict_api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend as web

    scan_root = tmp_path / "scan_output"
    _write_session_run(scan_root, "run-1")
    store = _store_with_contract(tmp_path)
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, "get_contract_store", lambda: store)
    return TestClient(web.app), store


def _package_payload(bundle: dict, column: str, status: str) -> dict:
    entry = _fp_entry(bundle, column, status=status)
    return {"entries": [entry.to_dict()]}


def test_verdicts_preview_api(verdict_api_client):
    client, _store = verdict_api_client
    payload = _package_payload(_bundle(), "msisdn", "not_pii")
    r = client.post(f"/api/evidence/{TABLE}/verdicts/preview", json={"package": payload, "run_id": "run-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["matching"] == 1


def test_verdicts_import_api_and_log(verdict_api_client):
    client, store = verdict_api_client
    payload = _package_payload(_bundle(), "msisdn", "not_pii")
    r = client.post(
        f"/api/evidence/{TABLE}/verdicts/import",
        json={"package": payload, "run_id": "run-1", "actor": "alice", "reason": "confirmed"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == ["msisdn"]
    assert store.get_pii_decisions(TABLE)["msisdn"]["status"] == "not_pii"

    r2 = client.get(f"/api/evidence/{TABLE}/verdicts/imports")
    assert r2.status_code == 200
    assert len(r2.json()["imports"]) == 1


def test_verdicts_import_api_requires_actor_reason(verdict_api_client):
    client, _store = verdict_api_client
    payload = _package_payload(_bundle(), "msisdn", "not_pii")
    r = client.post(
        f"/api/evidence/{TABLE}/verdicts/import",
        json={"package": payload, "run_id": "run-1"},
    )
    assert r.status_code >= 400


def test_verdicts_replay_is_non_mutating(verdict_api_client):
    client, store = verdict_api_client
    payload = _package_payload(_bundle(), "msisdn", "not_pii")
    r = client.post(
        f"/api/evidence/{TABLE}/runs/run-1/verdicts/replay",
        json={"package": payload},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == ["msisdn"]
    assert body["stale_skipped"] == []
    # Replay is run-scoped and non-mutating: no durable decision is written.
    assert store.get_pii_decisions(TABLE) == {}


def test_verdicts_preview_api_invalid_package_rejected(verdict_api_client):
    client, _store = verdict_api_client
    r = client.post(
        f"/api/evidence/{TABLE}/verdicts/preview",
        json={"package": {"entries": [{"table": TABLE, "column": "msisdn", "status": "not_a_status"}]}},
    )
    assert r.status_code == 400


# ── CLI: redibis verdict preview|import ──────────────────────────────────

def test_cli_verdict_preview_and_import(tmp_path: Path, capsys):
    from redibis.cli.main import main
    from redibis.review.verdict_package import write_verdict_package

    scan_root = tmp_path / "scan_output"
    _write_session_run(scan_root, "run-1")
    # `main()` builds its ContractStore from `{output_dir}/_dev_storage` (see
    # `_build_backend`) — seed the contract there so the CLI process sees it.
    backend = LocalBackend(scan_root / "_dev_storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [
                {"name": "msisdn", "logicalType": "string", "physicalType": "string"},
                {"name": "city", "logicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="test", run_id="r0")

    bundle = _bundle()
    entry = _fp_entry(bundle, "msisdn", status="not_pii")
    pkg_path = write_verdict_package(VerdictPackage(entries=[entry]), tmp_path / "verdicts.json")

    common = [
        "--output-dir", str(scan_root),
        "--s3-runs-bucket", "pii-reports",
        "--s3-contracts-bucket", "active-contracts",
    ]

    rc = main([
        "verdict", "preview", TABLE, "--package", str(pkg_path), "--run-id", "run-1",
        *common,
    ])
    assert rc == 0
    preview_out = json.loads(capsys.readouterr().out)
    assert preview_out["summary"]["matching"] == 1

    rc2 = main([
        "verdict", "import", TABLE, "--package", str(pkg_path), "--run-id", "run-1",
        "--actor", "alice", "--reason", "cli import",
        *common,
    ])
    assert rc2 == 0
    import_out = json.loads(capsys.readouterr().out)
    assert import_out["applied"] == ["msisdn"]


# ── golden end-to-end flow (plan §7) ─────────────────────────────────────
# scan → open review → record a steward verdict → export → replay in a
# later run → confirm the effective result follows the steward decision.

GOLDEN_TABLE = "data.golden_tutorial_customers"


def _golden_bundle(run_id: str) -> dict:
    """Evidence for the `data.golden_tutorial_customers` fixture columns
    (name, email, mobile, Egyptian national ID) — synthetic scores standing
    in for a real Presidio/GLiNER/NID-validator scan run so this test stays
    fast while exercising the full verdict lifecycle end to end."""
    return {
        "schema_version": "2.0",
        "table": {"name": GOLDEN_TABLE, "run_id": run_id},
        "columns": {
            "mobile": {
                "logical_type": "string",
                "physical_type": "string",
                "profile": {"format_masks": {"top_masks": [{"mask": "DDDDDDDDDDD"}]}},
                "pii_verdict": {"detected": True, "entity_type": "PHONE_NUMBER", "confidence": 0.9},
                "pii_evidence": {"phone_engine": {"ran": True, "rate": 0.95}},
            },
            "email": {
                "logical_type": "string",
                "pii_verdict": {"detected": True, "entity_type": "EMAIL", "confidence": 0.97},
                "pii_evidence": {"regex": {"ran": True, "score": 0.97}},
            },
            "national_id": {
                "logical_type": "string",
                "physical_type": "string",
                "profile": {"format_masks": {"top_masks": [{"mask": "DDDDDDDDDDDDDD"}]}},
                "pii_verdict": {"detected": False, "confidence": 0.2},
                "pii_evidence": {"national_id_egypt": {"ran": True, "rate": 0.10}},
            },
        },
    }


def test_golden_tutorial_end_to_end_scan_review_verdict_replay(tmp_path: Path):
    """Full lifecycle: scan (run-1) → steward reviews and corrects a column
    → export a portable verdict → a later scan (run-2, same schema) → the
    exported verdict is previewed, imported, and its effective result is
    confirmed to win over the fresh engine proposal."""
    store = ContractStore(LocalBackend(tmp_path / "storage"), bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": GOLDEN_TABLE,
            "properties": [
                {"name": "mobile", "logicalType": "string", "physicalType": "string"},
                {"name": "email", "logicalType": "string"},
                {"name": "national_id", "logicalType": "string", "physicalType": "string"},
            ],
        }],
    }
    store.upsert(contract, table=GOLDEN_TABLE, workflow="scan", run_id="r0")

    # 1. Scan (run-1): the engine proposal for national_id is a low-confidence
    #    "not PII" — the steward disagrees after opening the run in the reviewer.
    run1 = _golden_bundle("run-1")
    svc = _svc(store)
    review1 = svc.from_bundle(run1, table=GOLDEN_TABLE, mode="run")
    nid_col = next(c for c in review1["columns"] if c["column"] == "national_id")
    assert nid_col["effective_verdict"]["source"] == "engine"
    assert nid_col["effective_verdict"]["detected"] is False

    # 2. Steward records the correction, fingerprint-bound to *this run's*
    #    evidence (not the contract's coarser type-only fingerprint) so the
    #    decision matches the column the steward actually reviewed.
    nid_fp = fingerprint_from_evidence_column("national_id", run1["columns"]["national_id"])
    store.set_pii_decision(
        GOLDEN_TABLE, "national_id", "pii", entity_type="EG_NATIONAL_ID",
        decided_by="steward.alice", reason="confirmed 14-digit Egyptian NID format",
        run_id="run-1",
        fingerprint_key=nid_fp.fingerprint_key,
        name_normalized=nid_fp.name_normalized,
        logical_type=nid_fp.logical_type,
        physical_type=nid_fp.physical_type,
        format_signature=nid_fp.format_signature,
    )
    review1b = svc.from_bundle(run1, table=GOLDEN_TABLE, mode="run")
    nid_col_b = next(c for c in review1b["columns"] if c["column"] == "national_id")
    assert nid_col_b["effective_verdict"]["source"] == "steward"
    assert nid_col_b["effective_verdict"]["detected"] is True

    # 3. Export a portable verdict package.
    exported = svc.export_verdicts(tables=[GOLDEN_TABLE], exporter="steward.alice")
    assert exported["kind"] == "redibis.verdict_package"
    assert "sample" not in json.dumps(exported)
    package = VerdictPackage(entries=[VerdictEntry.from_dict(e) for e in exported["entries"]])

    # 4. A later scan (run-2) with the *same* column schema/format.
    run2 = _golden_bundle("run-2")

    # 5. Preview against run-2: the exported decision still matches (schema unchanged).
    preview = svc.preview_verdicts(GOLDEN_TABLE, run2, package)
    assert "national_id" in [r["column"] for r in preview["matching"]]

    # 6. Run-scoped replay confirms what *would* apply without writing anything.
    from redibis.review.verdict_package import write_verdict_package
    from redibis.scan.evidence_ops import apply_supplied_verdicts

    pkg_path = write_verdict_package(package, tmp_path / "golden_verdicts.json")
    replayed_review, warnings = apply_supplied_verdicts(run2, pkg_path)
    nid_replayed = next(c for c in replayed_review["columns"] if c["column"] == "national_id")
    assert nid_replayed["effective_verdict"]["source"] == "supplied"
    assert nid_replayed["effective_verdict"]["detected"] is True
    assert warnings["stale"] == []

    # Confirm the durable decision is unaffected by the dry-run replay.
    assert store.get_pii_decisions(GOLDEN_TABLE)["national_id"]["status"] == "pii"
