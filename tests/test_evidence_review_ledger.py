"""Tests for Evidence Review Ledger — drift, authority, export, and CLI replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.review.drift import LIFECYCLE_ACTIVE, LIFECYCLE_STALE, evaluate_column_drift
from redibis.review.fingerprint import ColumnFingerprintSnapshot, fingerprint_from_evidence_column
from redibis.review.verdict_package import VerdictEntry, VerdictPackage, load_verdict_package, write_verdict_package
from redibis.review.verdict_resolver import resolve_effective_verdict
from redibis.scan.evidence_ops import apply_supplied_verdicts
from redibis.services.evidence_review_service import EvidenceReviewService
from redibis.store.contract_store import ContractStore
from redibis.store.pii_decisions import PiiDecision, reconcile_pii_columns
from redibis.store.review_store import ReviewStore
from redibis.store.storage_backend import LocalBackend


TABLE = "telecom.customers"


def _fp(column: str, logical: str = "string", fmt: str = "DDDD") -> ColumnFingerprintSnapshot:
    from redibis.memory.fingerprint import normalize_column_name
    from redibis.memory.store import canonical_fingerprint_key

    name_norm = normalize_column_name(column)
    return ColumnFingerprintSnapshot(
        column=column,
        name_normalized=name_norm,
        logical_type=logical,
        physical_type=logical,
        format_signature=fmt,
        fingerprint_key=canonical_fingerprint_key(name_norm, logical, fmt),
    )


def _minimal_bundle(table: str = TABLE) -> dict:
    return {
        "schema_version": "2.0",
        "table": {"name": table, "run_id": "run-1"},
        "columns": {
            "msisdn": {
                "logical_type": "string",
                "physical_type": "string",
                "profile": {"format_masks": {"top_masks": [{"mask": "DDDD"}]}},
                "pii_verdict": {
                    "detected": True,
                    "entity_type": "PHONE_NUMBER",
                    "confidence": 0.92,
                },
            },
            "city": {
                "logical_type": "string",
                "pii_verdict": {"detected": False, "confidence": 0.1},
            },
        },
    }


def test_fingerprint_match_is_active():
    base = _fp("msisdn", fmt="DDDD")
    cur = _fp("msisdn", fmt="DDDD")
    drift = evaluate_column_drift(base, cur)
    assert drift.state == LIFECYCLE_ACTIVE
    assert drift.fingerprint_match is True


def test_logical_type_change_is_stale():
    base = _fp("msisdn", logical="string", fmt="DDDD")
    cur = _fp("msisdn", logical="integer", fmt="DDDD")
    drift = evaluate_column_drift(base, cur)
    assert drift.state == LIFECYCLE_STALE
    assert "logical_type_changed" in drift.reasons


def test_stale_decision_skipped_in_reconcile():
    contract = {
        "schema": [{
            "properties": [
                {"name": "email", "tags": ["pii"], "privacy": {"classification": "Restricted"}},
            ],
        }],
    }
    decisions = {
        "email": {
            "status": "not_pii",
            "lifecycle_state": LIFECYCLE_STALE,
        },
    }
    changed = reconcile_pii_columns(contract, decisions)
    assert changed == []
    assert contract["schema"][0]["properties"][0].get("tags")


def test_steward_wins_over_engine():
    bundle = _minimal_bundle()
    fp = fingerprint_from_evidence_column("msisdn", bundle["columns"]["msisdn"])
    steward = {
        "column": "msisdn",
        "status": "not_pii",
        "fingerprint_key": fp.fingerprint_key,
        "logical_type": fp.logical_type,
        "physical_type": fp.physical_type,
        "format_signature": fp.format_signature,
        "name_normalized": fp.name_normalized,
        "lifecycle_state": LIFECYCLE_ACTIVE,
    }
    effective = resolve_effective_verdict(
        column="msisdn",
        col_block=bundle["columns"]["msisdn"],
        current_fingerprint=fp,
        steward_decision=steward,
    )
    assert effective.source == "steward"
    assert effective.detected is False
    assert effective.engine_proposal["detected"] is True


def test_supplied_verdict_run_scoped_overlay(tmp_path: Path):
    bundle = _minimal_bundle()
    fp = fingerprint_from_evidence_column("msisdn", bundle["columns"]["msisdn"])
    package = VerdictPackage(
        entries=[
            VerdictEntry(
                table=TABLE,
                column="msisdn",
                status="not_pii",
                fingerprint_key=fp.fingerprint_key,
                logical_type=fp.logical_type,
                physical_type=fp.physical_type,
                format_signature=fp.format_signature,
                name_normalized=fp.name_normalized,
            ),
        ],
    )
    path = write_verdict_package(package, tmp_path / "verdicts.json")
    review, warnings = apply_supplied_verdicts(bundle, path)
    msisdn = next(c for c in review["columns"] if c["column"] == "msisdn")
    assert msisdn["effective_verdict"]["source"] == "supplied"
    assert msisdn["effective_verdict"]["detected"] is False
    assert warnings["stale"] == []


def test_stale_supplied_verdict_warns(tmp_path: Path):
    bundle = _minimal_bundle()
    package = VerdictPackage(
        entries=[
            VerdictEntry(
                table=TABLE,
                column="msisdn",
                status="not_pii",
                fingerprint_key="old|key|mismatch",
                logical_type="integer",
                format_signature="mixed",
                name_normalized="msisdn",
            ),
        ],
    )
    path = write_verdict_package(package, tmp_path / "verdicts.json")
    review, warnings = apply_supplied_verdicts(bundle, path)
    msisdn = next(c for c in review["columns"] if c["column"] == "msisdn")
    assert msisdn["effective_verdict"]["source"] == "engine"
    assert "msisdn" in warnings["missing"] or "msisdn" in warnings["stale"]


def test_export_verdicts_privacy_safe(tmp_path: Path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [{"name": "email", "tags": ["pii"]}],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="test", run_id="r1")
    fp = _fp("email")
    store.pii_decisions.set(
        TABLE,
        PiiDecision(
            column="email",
            status="pii",
            fingerprint_key=fp.fingerprint_key,
            logical_type=fp.logical_type,
            format_signature=fp.format_signature,
            name_normalized=fp.name_normalized,
            decided_by="alice",
            reason="confirmed",
        ),
    )
    svc = EvidenceReviewService(
        store,
        review_store=ReviewStore(backend, "active-contracts"),
        pii_store=store.pii_decisions,
    )
    exported = svc.export_verdicts(tables=[TABLE])
    assert exported["kind"] == "redibis.verdict_package"
    entry = exported["entries"][0]
    assert entry["column"] == "email"
    assert "payload" not in entry
    assert "sample" not in json.dumps(exported)


def test_decision_history_recorded_on_overwrite(tmp_path: Path):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [{"name": "email", "logicalType": "string"}],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="test", run_id="r1")

    store.set_pii_decision(TABLE, "email", "pii", decided_by="alice", reason="confirmed")
    store.set_pii_decision(TABLE, "email", "not_pii", decided_by="bob", reason="false positive")

    history = store.pii_decisions.history(TABLE, "email")
    assert len(history) == 2
    assert history[0]["decided_by"] == "bob"
    assert history[0]["status"] == "not_pii"
    assert history[1]["decided_by"] == "alice"
    assert history[1]["status"] == "pii"
    # the current decision's own dict never nests its own history
    assert "history" not in history[1]


def test_set_pii_decision_auto_derives_fingerprint(tmp_path: Path):
    """Every decision is fingerprint-bound by default — no new legacy authority."""
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [{"name": "phone", "logicalType": "string", "physicalType": "string"}],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="test", run_id="r1")

    store.set_pii_decision(TABLE, "phone", "pii", decided_by="carol")
    decisions = store.get_pii_decisions(TABLE)
    assert decisions["phone"]["fingerprint_key"]
    assert decisions["phone"]["logical_type"] == "string"


def test_drift_evaluated_and_persisted_on_contract_write(tmp_path: Path):
    """Drift is marked at the ContractStore.upsert() choke point, not on GET."""
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [{"name": "msisdn", "logicalType": "string", "physicalType": "string"}],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="test", run_id="r1")
    store.set_pii_decision(TABLE, "msisdn", "not_pii", decided_by="dave")
    assert store.get_pii_decisions(TABLE)["msisdn"]["lifecycle_state"] == "active"

    # Re-upsert with a changed logical type for the same column → drift.
    drifted_contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [{"name": "msisdn", "logicalType": "integer", "physicalType": "integer"}],
        }],
    }
    store.upsert(drifted_contract, table=TABLE, workflow="test", run_id="r2")
    assert store.get_pii_decisions(TABLE)["msisdn"]["lifecycle_state"] == "stale"


def test_evidence_review_get_is_side_effect_free(review_run_client):
    """Opening the reviewer (GET) must never mutate the steward ledger."""
    client, table = review_run_client
    from redibis.webapp import backend as web

    store = web.get_contract_store()
    fp = fingerprint_from_evidence_column("msisdn", _minimal_bundle()["columns"]["msisdn"])
    # Write directly to the sidecar (bypassing set_pii_decision's implicit
    # contract re-upsert) to simulate a decision whose fingerprint has already
    # drifted from the evidence bundle but hasn't been reconciled by a write yet.
    store.pii_decisions.set(table, PiiDecision(
        column="msisdn", status="not_pii", decided_by="erin",
        fingerprint_key="stale|mismatch|key",
        logical_type="integer", physical_type="integer", format_signature="mismatch",
        name_normalized=fp.name_normalized,
        lifecycle_state="active",
    ))
    before = store.get_pii_decisions(table)["msisdn"]["lifecycle_state"]
    r = client.get(f"/api/evidence/{table}/runs/run-1")
    assert r.status_code == 200
    after = store.get_pii_decisions(table)["msisdn"]["lifecycle_state"]
    assert before == after == "active"

    r2 = client.post(f"/api/evidence/{table}/review/recompute-drift?run_id=run-1")
    assert r2.status_code == 200
    assert "msisdn" in r2.json()["stale_columns_marked"]
    assert store.get_pii_decisions(table)["msisdn"]["lifecycle_state"] == "stale"


def test_column_decision_history_api(review_api_client):
    client, store = review_api_client
    client.put(f"/api/contracts/{TABLE}/review/columns/email",
               json={"reviewer": "alice", "pii_status": "not_pii"})
    client.put(f"/api/contracts/{TABLE}/review/columns/email",
               json={"reviewer": "bob", "pii_status": "pii", "reason": "reversed"})
    r = client.get(f"/api/contracts/{TABLE}/review/columns/email/history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["history"]) == 2
    assert body["history"][0]["decided_by"] == "bob"


def test_load_verdict_package_json_and_jsonl(tmp_path: Path):
    entry = VerdictEntry(table=TABLE, column="x", status="not_pii", fingerprint_key="a|b|c")
    package = VerdictPackage(entries=[entry])
    json_path = write_verdict_package(package, tmp_path / "v.json")
    loaded = load_verdict_package(json_path)
    assert loaded.entries[0].column == "x"
    jsonl_path = write_verdict_package(package, tmp_path / "v.jsonl", as_jsonl=True)
    loaded2 = load_verdict_package(jsonl_path)
    assert loaded2.entries[0].status == "not_pii"


@pytest.fixture
def review_api_client(tmp_path, monkeypatch):
    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{
            "physicalName": TABLE,
            "properties": [
                {"name": "email", "tags": ["pii"], "privacy": {"classification": "Restricted"}},
            ],
        }],
    }
    store.upsert(contract, table=TABLE, workflow="manual", run_id="r1")

    from redibis.webapp import backend as web
    from fastapi.testclient import TestClient

    monkeypatch.setattr(web, "get_contract_store", lambda: store)
    return TestClient(web.app), store


def test_verdict_export_api(review_api_client):
    client, store = review_api_client
    fp = _fp("email")
    store.set_pii_decision(
        TABLE, "email", "pii",
        decided_by="bob",
        fingerprint_key=fp.fingerprint_key,
        logical_type=fp.logical_type,
        format_signature=fp.format_signature,
        name_normalized=fp.name_normalized,
    )
    r = client.get(f"/api/contracts/{TABLE}/verdicts/export")
    assert r.status_code == 200
    body = r.json()
    assert body["entries"][0]["decided_by"] == "bob"


def test_evidence_review_from_bundle_api(review_api_client):
    client, _store = review_api_client
    r = client.post(
        "/api/evidence/review",
        json={"bundle": _minimal_bundle(), "mode": "test"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == TABLE
    assert len(body["columns"]) == 2
    msisdn = next(c for c in body["columns"] if c["column"] == "msisdn")
    assert msisdn["effective_verdict"]["source"] == "engine"


def test_review_edit_captures_fingerprint(review_api_client):
    client, store = review_api_client
    r = client.put(
        f"/api/contracts/{TABLE}/review/columns/email",
        json={"reviewer": "alice", "pii_status": "not_pii"},
    )
    assert r.status_code == 200, r.text
    decisions = store.get_pii_decisions(TABLE)
    assert decisions["email"]["fingerprint_key"]
    assert decisions["email"]["lifecycle_state"] == "active"
    assert int(decisions["email"]["decision_version"]) >= 1


def test_review_page_route(review_api_client):
    client, _store = review_api_client
    r = client.get(f"/review?table={TABLE}")
    assert r.status_code == 200


def test_session_evidence_paths_finds_scan_output_layout(tmp_path: Path):
    from redibis.scan.evidence_ops import _session_evidence_paths, load_evidence_bundle

    table = "data.golden_tutorial_customers"
    session_dir = tmp_path / "scan_output" / "sess-1"
    run_dir = session_dir / "runs" / "scan_pii_test"
    run_dir.mkdir(parents=True)
    bundle_path = run_dir / "evidence_bundle.json"
    bundle_path.write_text(json.dumps(_minimal_bundle(table=table)), encoding="utf-8")
    (session_dir / "session.json").write_text(
        json.dumps({"table_name": table}),
        encoding="utf-8",
    )

    paths = _session_evidence_paths(tmp_path / "scan_output", table)
    assert paths == [bundle_path]

    loaded, hint = load_evidence_bundle(
        table=table,
        output_dirs=[tmp_path / "scan_output"],
        backend=None,
    )
    assert hint == str(bundle_path)
    assert len(loaded["columns"]) == 2


def _write_session_run(scan_root: Path, table: str, run_id: str, *, extra_columns: bool = False) -> Path:
    session_dir = scan_root / f"sess-{run_id}"
    run_dir = session_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    bundle = _minimal_bundle(table=table)
    bundle["table"]["run_id"] = run_id
    if extra_columns:
        bundle["columns"]["email"] = {
            "logical_type": "string",
            "pii_verdict": {"detected": True, "entity_type": "EMAIL", "confidence": 0.9},
        }
    (run_dir / "evidence_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    (session_dir / "session.json").write_text(json.dumps({"table_name": table}), encoding="utf-8")
    return run_dir


def _write_manifest(run_dir: Path, table: str, run_id: str) -> None:
    from redibis.evidence.manifest import artifact_ref, build_manifest

    bundle_path = run_dir / "evidence_bundle.json"
    (run_dir / "notes.txt").write_text("hello reviewer", encoding="utf-8")
    artifacts = {
        "evidence_bundle": artifact_ref(
            name="evidence_bundle.json", path="evidence_bundle.json",
            kind="json", sensitivity="shareable", local_path=bundle_path,
        ),
        "notes": artifact_ref(
            name="notes.txt", path="notes.txt",
            kind="text", sensitivity="shareable", local_path=run_dir / "notes.txt",
        ),
        "llm_raw": artifact_ref(
            name="llm_raw", path="llm_calls/raw.json",
            kind="json", sensitivity="restricted",
        ),
    }
    manifest = build_manifest(
        table=table, run_id=run_id, artifacts=artifacts,
        run_profile=True, run_quality=False, run_pii=True, run_status="success",
    )
    (run_dir / "evidence_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def review_run_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend as web

    table = "data.golden_tutorial_customers"
    scan_root = tmp_path / "scan_output"
    run1 = _write_session_run(scan_root, table, "run-1")
    _write_manifest(run1, table, "run-1")
    run2 = _write_session_run(scan_root, table, "run-2", extra_columns=True)
    _write_manifest(run2, table, "run-2")

    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    return TestClient(web.app), table


def test_evidence_list_runs_api(review_run_client):
    client, table = review_run_client
    r = client.get(f"/api/evidence/{table}/runs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["runs"]) == {"run-1", "run-2"}


def test_evidence_run_overview_api(review_run_client):
    client, table = review_run_client
    r = client.get(f"/api/evidence/{table}/runs/run-2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "run-2"
    assert len(body["columns"]) == 3
    assert body["artifacts"]


def test_evidence_run_column_drilldown_api(review_run_client):
    client, table = review_run_client
    r = client.get(f"/api/evidence/{table}/runs/run-1/columns/msisdn")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["column"] == "msisdn"
    assert body["run_id"] == "run-1"

    r404 = client.get(f"/api/evidence/{table}/runs/run-1/columns/does_not_exist")
    assert r404.status_code == 404


def test_evidence_run_artifacts_list_and_content(review_run_client):
    client, table = review_run_client
    r = client.get(f"/api/evidence/{table}/runs/run-1/artifacts")
    assert r.status_code == 200, r.text
    names = set(r.json()["artifacts"].keys())
    assert {"evidence_bundle", "notes", "llm_raw"} <= names

    r_content = client.get(f"/api/evidence/{table}/runs/run-1/artifacts/notes")
    assert r_content.status_code == 200
    assert "hello reviewer" in r_content.text

    r_restricted = client.get(f"/api/evidence/{table}/runs/run-1/artifacts/llm_raw")
    assert r_restricted.status_code == 403


def test_evidence_compare_runs_api(review_run_client):
    client, table = review_run_client
    r = client.get(f"/api/evidence/{table}/runs/compare?a=run-1&b=run-2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_a"] == "run-1"
    assert body["run_b"] == "run-2"
    email_row = next(c for c in body["columns"] if c["column"] == "email")
    assert email_row["present_in_a"] is False
    assert email_row["present_in_b"] is True
    assert email_row["changed"] is True


def test_evidence_restricted_llm_requires_actor_reason(review_run_client):
    client, table = review_run_client
    r = client.post(
        f"/api/evidence/{table}/restricted/llm",
        json={"run_id": "run-1", "call_id": "call-1"},
    )
    assert r.status_code == 403


def test_evidence_review_table_api_from_session(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend as web

    table = "data.golden_tutorial_customers"
    scan_root = tmp_path / "scan_output"
    session_dir = scan_root / "sess-1"
    run_dir = session_dir / "runs" / "scan_pii_test"
    run_dir.mkdir(parents=True)
    (run_dir / "evidence_bundle.json").write_text(
        json.dumps(_minimal_bundle(table=table)),
        encoding="utf-8",
    )
    (session_dir / "session.json").write_text(
        json.dumps({"table_name": table}),
        encoding="utf-8",
    )

    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    client = TestClient(web.app)
    r = client.get(f"/api/evidence/{table}/review")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == table
    assert len(body["columns"]) == 2
    assert body["progress"]["total_columns"] == 2


# ── gap-closure regressions (audit fixes) ─────────────────────────────────


def test_review_never_exposes_raw_sample_values():
    """The review DTO is a general-audience surface — literal sample values

    (which ``evidence_bundle.json`` may embed under ``sample_mode: raw``, the
    scan default) must never leave ``from_bundle`` un-redacted. Only sampling
    shape/count is safe to expose; exact values stay in the governed spool.
    """
    bundle = _minimal_bundle()
    bundle["columns"]["msisdn"]["samples"] = {
        "mode": "raw", "n": 3, "seed": 7,
        "values": ["+201001234567", "+201009876543", "+201005555555"],
    }
    svc = EvidenceReviewService()
    out = svc.from_bundle(bundle, mode="test")
    msisdn = next(c for c in out["columns"] if c["column"] == "msisdn")
    assert msisdn["samples"]["mode"] == "raw"
    assert msisdn["samples"]["count"] == 3
    assert "values" not in msisdn["samples"]
    dumped = json.dumps(out)
    assert "+201001234567" not in dumped


def test_engine_matrix_sources_canonical_engine_evidence_not_compat_blocks():
    """``engine_evidence`` must be the plugin-neutral canonical map, not the

    legacy ``pii_evidence`` "compat" blocks (which use ad-hoc per-engine field
    names like ``rate``/``entities`` and would otherwise also leak the nested
    ``engine_evidence`` map itself as a bogus extra "engine").
    """
    bundle = _minimal_bundle()
    bundle["columns"]["msisdn"]["pii_evidence"] = {
        # Legacy compat blocks: ad-hoc field names, must NOT surface as-is.
        "phone": {"ran": True, "rate": 0.95, "valid_rate": 0.95},
        "presidio": {"ran": True, "entities": {"PHONE_NUMBER": 0.9}},
        # Canonical plugin-neutral shape: this is what must surface.
        "engine_evidence": {
            "phone": {
                "engine_id": "phone", "ran": True, "score": 0.95,
                "match_rate": 0.95, "rates": {"valid_rate": 0.95},
            },
            "presidio": {"engine_id": "presidio", "ran": False, "reason": "excluded"},
        },
    }
    svc = EvidenceReviewService()
    out = svc.from_bundle(bundle, mode="test")
    msisdn = next(c for c in out["columns"] if c["column"] == "msisdn")
    matrix = msisdn["engine_evidence"]
    assert set(matrix.keys()) == {"phone", "presidio"}
    assert "rate" not in matrix["phone"]  # legacy field name absent
    assert matrix["phone"]["match_rate"] == pytest.approx(0.95)
    assert matrix["phone"]["rates"]["valid_rate"] == pytest.approx(0.95)
    assert matrix["presidio"]["ran"] is False
    assert matrix["presidio"]["reason"] == "excluded"


def test_edit_column_with_run_id_fingerprints_from_evidence_not_unknown(tmp_path: Path, monkeypatch):
    """A steward decision made against a specific run must fingerprint from

    that run's real evidence profile (plan §5), not the contract property
    (whose profile is always empty, yielding ``format_signature="unknown"``
    and later mismatching evidence-derived drift checks).
    """
    from fastapi.testclient import TestClient

    from redibis.webapp import backend as web

    table = TABLE
    scan_root = tmp_path / "scan_output"
    run_dir = _write_session_run(scan_root, table, "run-1")
    bundle = json.loads((run_dir / "evidence_bundle.json").read_text(encoding="utf-8"))
    expected_fp = fingerprint_from_evidence_column("msisdn", bundle["columns"]["msisdn"])
    assert expected_fp.format_signature != "unknown"

    backend = LocalBackend(tmp_path / "storage")
    store = ContractStore(backend, bucket="active-contracts")
    contract = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "schema": [{"physicalName": table, "properties": [{"name": "msisdn"}]}],
    }
    store.upsert(contract, table=table, workflow="manual", run_id="r0")

    monkeypatch.setenv("SCAN_OUTPUT_DIR", str(scan_root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web, "get_contract_store", lambda: store)

    client = TestClient(web.app)
    r = client.put(
        f"/api/contracts/{table}/review/columns/msisdn",
        json={"reviewer": "alice", "pii_status": "pii",
              "entity_type": "PHONE_NUMBER", "run_id": "run-1"},
    )
    assert r.status_code == 200, r.text
    decisions = store.get_pii_decisions(table)
    assert decisions["msisdn"]["format_signature"] == expected_fp.format_signature
    assert decisions["msisdn"]["format_signature"] != "unknown"
    assert decisions["msisdn"]["evidence_digest"]
    assert decisions["msisdn"]["run_id"] == "run-1"
