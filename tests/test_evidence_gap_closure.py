"""Regression tests for remaining steward-evidence gaps."""

from __future__ import annotations

import json
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from redibis.config import EvidenceAccessPolicy, RAIConfig
from redibis.evidence.manifest import build_manifest
from redibis.evidence.paths import prefixes_for_run_id, storage_prefix_for_key
from redibis.evidence.redact import sanitize_shareable_payload
from redibis.evidence.spool import DEFAULT_OUTPUT_DIR, DEFAULT_SPOOL_DIR
from redibis.models import PIIDetection
from redibis.scan.config import ScanConfig
from redibis.scan.context_build import ContextBuildError, build_context
from redibis.scan.evidence_ops import detection_from_evidence, replay, strip_bundle, strip_raw_values
from redibis.scan.report_bundle import ReportBundle, _detection_to_dict
from redibis.scan.types import ScanRunResult
from redibis.telemetry.llm_evidence import llm_evidence_recorder
from redibis.telemetry.model_gateway import guarded_model_call


def test_exact_records_are_spool_only_with_restricted_modes(tmp_path):
    spool = tmp_path / "spool"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with llm_evidence_recorder(
        run_dir=run_dir, run_id="r1", spool_dir=spool, execution_mode="single_llm",
    ):
        guarded_model_call(
            lambda: "ok",
            model_id="m",
            user_prompt="ada@example.com",
            rai_config=RAIConfig(enabled=False),
        )
    assert list((run_dir / "llm_calls").glob("*.raw.json")) == []
    raw = list((spool / "r1" / "llm_calls").glob("*.raw.json"))
    assert len(raw) == 1
    assert (raw[0].stat().st_mode & 0o777) == 0o600
    assert ((spool / "r1").stat().st_mode & 0o777) == 0o700
    assert ((spool / "r1" / "llm_calls").stat().st_mode & 0o777) == 0o700


def test_custom_policy_audit_path_and_role(tmp_path, capsys, monkeypatch):
    from redibis.cli.evidence_cmd import _run_evidence_llm
    from redibis.config import EvidenceAccessPolicy

    spool = tmp_path / "custom_spool"
    policy = EvidenceAccessPolicy(
        steward_role="data_steward",
        require_actor=True,
        require_reason=True,
        audit_enabled=True,
    )
    monkeypatch.setattr(
        "redibis.cli.evidence_cmd._configured_spool_and_policy",
        lambda output_dir=None: (spool, policy),
    )

    run_dir = tmp_path / "r1"
    (run_dir / "llm_calls").mkdir(parents=True)
    bundle = {"table": {"name": "db.t", "run_id": "r1"}, "columns": {"a": {}}}
    (run_dir / "evidence_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    raw = {"call_id": "c1", "user_prompt": "secret"}
    (spool / "r1" / "llm_calls").mkdir(parents=True)
    (spool / "r1" / "llm_calls" / "0001-c1.raw.json").write_text(json.dumps(raw), encoding="utf-8")
    (run_dir / "llm_calls" / "index.json").write_text(
        json.dumps({"calls": [{"call_id": "c1", "shareable": "llm_calls/x.json"}]}),
        encoding="utf-8",
    )

    args_bad_role = Namespace(
        table="db.t", run_id="r1", latest=False, out="-", call_id="c1",
        raw=True, reason="debug", actor="steward", role="intern",
    )
    assert _run_evidence_llm(args_bad_role, tmp_path) == 2

    args_ok = Namespace(
        table="db.t", run_id="r1", latest=False, out="-", call_id="c1",
        raw=True, reason="debug", actor="steward", role="data_steward",
    )
    assert _run_evidence_llm(args_ok, tmp_path) == 0
    audit = spool / "_audit" / "restricted_reads.jsonl"
    assert audit.is_file()
    assert (audit.stat().st_mode & 0o777) == 0o600


def test_strip_raw_values_omits_llm_reasoning_and_strip_bundle_alias():
    bundle = {
        "columns": {
            "email": {
                "samples": {"values": ["ada@example.com"], "mode": "raw"},
                "pii_evidence": {
                    "llm_refiner": {"ran": True, "score": 0.9, "reasoning": "this is ada@example.com"},
                    "llm_reasoning": "free text with ada@example.com",
                },
            }
        },
        "sensitivity": {"contains_raw_pii": True},
    }
    stripped = strip_raw_values(bundle)
    blob = json.dumps(stripped)
    assert "ada@example.com" not in blob
    assert "llm_reasoning" not in blob
    assert "reasoning" not in blob
    with pytest.warns(DeprecationWarning):
        aliased = strip_bundle(bundle)
    assert aliased["sensitivity"]["contains_raw_pii"] is False


def test_detection_json_omits_llm_reasoning():
    det = PIIDetection(
        column="email",
        llm_reasoning="John Smith lives here",
        llm_score=0.8,
        detected=True,
        entity_type="EMAIL",
    )
    payload = _detection_to_dict(det)
    assert "llm_reasoning" not in payload
    assert "John Smith" not in json.dumps(payload)


def test_context_build_detects_literal_samples(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({
        "sensitivity": {"contains_raw_pii": False},
        "columns": {"email": {"samples": {"values": ["ada@example.com"]}}},
    }), encoding="utf-8")
    with pytest.raises(ContextBuildError):
        build_context([path], allow_raw_pii=False)


def test_artifact_path_blocks_raw_and_raw_bundle(tmp_path):
    from redibis.services.session.loader import artifact_path_allowed

    raw = tmp_path / "0001-c1.raw.json"
    raw.write_text("{}", encoding="utf-8")
    bundle = tmp_path / "evidence_bundle.json"
    bundle.write_text("{}", encoding="utf-8")
    share = tmp_path / "evidence_bundle.shareable.json"
    share.write_text("{}", encoding="utf-8")
    assert artifact_path_allowed(raw, run_dir=tmp_path) is False
    assert artifact_path_allowed(bundle, run_dir=tmp_path) is False
    assert artifact_path_allowed(share, run_dir=tmp_path) is True


def test_nested_prefix_discovery():
    keys = [
        "scan/db_t/sess-1/run-9/evidence_manifest.json",
        "scan/db_t/run-flat/pii_detections.json",
    ]
    nested = storage_prefix_for_key(keys[0], "run-9")
    assert nested == "scan/db_t/sess-1/run-9/"
    assert prefixes_for_run_id(keys, "run-9") == ["scan/db_t/sess-1/run-9/"]
    assert prefixes_for_run_id(keys, "run-flat") == ["scan/db_t/run-flat/"]


def test_generic_first_replay_ignores_legacy_when_ran_false():
    bundle = {
        "header": {"rules": {"equations": []}, "timestamps": {}},
        "columns": {
            "nid": {
                "pii_evidence": {
                    "engine_evidence": {
                        "nid": {"ran": False, "reason": "skipped", "entity": None},
                        "presidio": {"ran": True, "score": 0.2, "entity": "OTHER"},
                    },
                    "presidio": {"entity": "EG_NID", "score": 0.99},
                },
                "validator_results": {"v.eg_nid": {"rate": 0.99, "checked": 10}},
                "pii_verdict": {"detected": False, "entity_type": "", "confidence": 0.0},
            }
        },
        "table_summary": {"pii": {}},
    }
    before = json.dumps(bundle["columns"]["nid"]["pii_evidence"], sort_keys=True)
    from redibis.pii.thresholds import Thresholds

    out = replay(bundle, equation="independent", thresholds=Thresholds())
    after = json.dumps(out["columns"]["nid"]["pii_evidence"], sort_keys=True)
    assert before == after
    det = detection_from_evidence("nid", out["columns"]["nid"])
    assert det.nid_valid_rate is None
    assert det.entity_type == "OTHER"
    assert "future_plugin" not in (out["columns"]["nid"]["pii_verdict"].get("deciding_engines") or [])


def test_plugin_not_in_replay_deciding_engines():
    from redibis.pii.thresholds import Thresholds

    bundle = {
        "header": {"rules": {"equations": []}, "timestamps": {}},
        "columns": {
            "x": {
                "pii_evidence": {
                    "engine_evidence": {
                        "presidio": {"ran": True, "score": 0.2},
                        "future_plugin": {"ran": True, "score": 0.99, "entity": "SECRET"},
                    }
                },
                "pii_verdict": {"detected": False},
            }
        },
        "table_summary": {"pii": {}},
    }
    out = replay(bundle, equation="independent", thresholds=Thresholds())
    deciding = out["columns"]["x"]["pii_verdict"]["deciding_engines"]
    assert "future_plugin" not in deciding
    assert "presidio" in deciding


def test_empty_and_cancelled_manifest_status():
    empty = build_manifest(
        table="db.t", run_id="r1", artifacts={"evidence_bundle": "evidence_bundle.json"},
        run_profile=True, empty=True, run_status="success",
    )
    assert empty["run_status"] == "empty"
    assert empty["coverage"]["profile"]["reason"] == "empty table"
    cancelled = build_manifest(
        table="db.t", run_id="r1", artifacts={},
        run_profile=True, run_status="cancelled",
    )
    assert cancelled["run_status"] == "cancelled"
    assert cancelled["coverage"]["profile"]["reason"] == "scan cancelled"


def test_manifest_rewritten_on_upload_failure(tmp_path):
    result = ScanRunResult(run_id="r1", table="db.t", status="success", total_rows=3)
    cfg = ScanConfig(table="db.t", run_profile=True, run_quality=False, run_pii=False, output_dir=tmp_path)
    (tmp_path / "evidence_bundle.json").write_text("{}", encoding="utf-8")
    (tmp_path / "effective_config.yaml").write_text("table: db.t\n", encoding="utf-8")
    (tmp_path / "effective_config.sha256").write_text("abc\n", encoding="utf-8")

    class _Writer:
        def write_file(self, name, path):
            if name == "evidence_manifest.json":
                raise OSError("upload denied")
            return f"s3:{name}"

        def write(self, name, content):
            return name

        def write_evidence(self, payload):
            return "_meta/evidence/db.t/r1.json"

    artifacts = ReportBundle(result, cfg).flush(tmp_path, run_writer=_Writer())
    man = json.loads(Path(artifacts["evidence_manifest"]).read_text(encoding="utf-8"))
    assert any("upload" in str(e.get("error", "")) for e in man.get("errors") or [])
    assert "effective_config.sha256" in man["artifacts"]


def test_default_output_dir_is_reports():
    assert DEFAULT_OUTPUT_DIR == Path("./reports")
    assert DEFAULT_SPOOL_DIR == Path("./reports/_restricted_evidence")
    assert ScanConfig(table="db.t").output_dir == Path("./reports")
    from redibis.config import ReportConfig

    assert ReportConfig().output_dir == Path("./reports")


def test_sanitize_shareable_payload_redacts_uuid():
    out = sanitize_shareable_payload({
        "note": "id 550e8400-e29b-41d4-a716-446655440000",
        "llm_reasoning": "should vanish",
    })
    assert "llm_reasoning" not in out
    assert "550e8400-e29b-41d4-a716-446655440000" not in json.dumps(out)


def test_concurrent_resume_appends_without_losing_seq(tmp_path):
    spool = tmp_path / "spool"
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def _one(n):
        with llm_evidence_recorder(run_dir=run_dir, run_id="r1", spool_dir=spool):
            guarded_model_call(
                lambda: f"ok{n}",
                model_id="m",
                user_prompt=f"u{n}",
                rai_config=RAIConfig(enabled=False),
            )

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    index = json.loads((run_dir / "llm_calls" / "index.json").read_text(encoding="utf-8"))
    seqs = [c["seq"] for c in index["calls"]]
    assert len(seqs) == 4
    assert sorted(seqs) == seqs
    assert len(set(seqs)) == 4


def test_session_writer_uses_nested_prefix(tmp_path):
    from redibis.store.run_output_writer import RunOutputWriter, bare_evidence_run_id
    from redibis.store.storage_backend import LocalBackend

    backend = LocalBackend(str(tmp_path / "store"))
    writer = RunOutputWriter(
        backend=backend,
        bucket="pii-reports",
        workflow="scan",
        table="db.t",
        run_id="sess-abc/run-1",
    )
    writer.write("pii_detections.json", [{"column": "email"}])
    key = "scan/db_t/sess-abc/run-1/pii_detections.json"
    assert backend.exists("pii-reports", key)
    assert bare_evidence_run_id(writer.run_id) == "run-1"
    assert storage_prefix_for_key(key, "run-1") == "scan/db_t/sess-abc/run-1/"
