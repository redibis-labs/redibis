"""Unified evidence foundation: models, engines, compare, persist, flush."""

from __future__ import annotations

import ast
import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from redibis.evidence.compare import build_result_bundle, compare_column_maps, pii_columns_from_detections
from redibis.evidence.engines import project_engine_evidence
from redibis.evidence.manifest import build_manifest
from redibis.evidence.models import LLMCallEvidence, SCHEMA_VERSION
from redibis.evidence.persist import is_raw_artifact, write_llm_call_files
from redibis.evidence.sanitize import config_sha256, sanitize_mapping
from redibis.models import PIIDetection
from redibis.scan.config import ScanConfig
from redibis.scan.facades import ProfileScan
from redibis.scan.report_bundle import ReportBundle, _upload_artifacts
from redibis.scan.types import ScanRunResult
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.storage_backend import LocalBackend


REPO = Path(__file__).resolve().parents[1]
EVIDENCE_PKG = REPO / "redibis" / "evidence"
_LEAF_BANNED = (
    "redibis.scan",
    "redibis.pii",
    "redibis.profiling",
    "redibis.agents",
    "redibis.cli",
    "redibis.store",
    "redibis.webapp",
)


def _import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_evidence_package_is_a_leaf():
    for path in sorted(EVIDENCE_PKG.rglob("*.py")):
        names = _import_names(path)
        for banned in _LEAF_BANNED:
            assert not any(n == banned or n.startswith(banned + ".") for n in names), (
                f"{path.relative_to(REPO)} must not import {banned!r} (found {names})"
            )


def test_sanitize_mapping_strips_secrets():
    payload = {
        "api_key": "sk-secret",
        "nested": {"password": "hunter2", "table": "db.t"},
        "token_value": "sk-ant-abcdefghij",
        "endpoint": "http://localhost:11434",
    }
    out = sanitize_mapping(payload)
    assert "api_key" not in out
    assert "password" not in out["nested"]
    assert out["nested"]["table"] == "db.t"
    assert out["token_value"] == "[REDACTED]"
    assert config_sha256(payload) == config_sha256(payload)


def test_project_engine_evidence_includes_skips_and_future_plugins():
    det = PIIDetection(
        column="msisdn",
        detected=True,
        entity_type="PHONE_NUMBER",
        presidio_score=0.91,
        regex_hits=[{"entity_type": "PHONE_NUMBER", "score": 0.91}],
        gliner_score=0.31,
        ner_hits=[{"label": "phone", "score": 0.31}],
        phone_score=0.88,
        phone_valid_rate=0.97,
        nid_valid_rate=None,
        engine_evidence={
            "future_plugin": {
                "engine_id": "future_plugin",
                "name": "Tomorrow NER",
                "kind": "ner",
                "ran": True,
                "score": 0.77,
            }
        },
    )
    mapped = project_engine_evidence(det)
    assert mapped["presidio"]["ran"] is True
    assert mapped["presidio"]["score"] == pytest.approx(0.91)
    assert mapped["gliner"]["ran"] is True
    assert mapped["gliner"]["hits"]
    assert mapped["phone"]["ran"] is True
    assert mapped["nid"]["ran"] is False
    assert mapped["nid"]["reason"]
    assert mapped["future_plugin"]["ran"] is True
    assert mapped["future_plugin"]["score"] == pytest.approx(0.77)
    contrib = det.contributing_engines()
    assert "future_plugin" in contrib
    assert "future_plugin" not in det.deciding_engines()


def test_compare_available_modes_only_never_invokes_models(monkeypatch):
    from redibis.telemetry import model_gateway

    def _boom(*_a, **_k):
        raise AssertionError("compare must not call guarded_model_call")

    monkeypatch.setattr(model_gateway, "guarded_model_call", _boom)
    det = PIIDetection(column="email", detected=True, entity_type="EMAIL", confidence=0.9)
    cols = pii_columns_from_detections([det])
    bundle = build_result_bundle(
        table="db.t",
        run_id="r1",
        deterministic=cols,
        single_llm=None,
        agentic=None,
    )
    variants = {v["mode"]: v for v in bundle["variants"]["variants"]}
    assert variants["deterministic"]["status"] == "evaluated"
    assert variants["single_llm"]["status"] == "not_run"
    assert variants["agentic"]["status"] == "not_run"
    assert bundle["comparison"]["modes_compared"] == ["deterministic"]
    assert bundle["comparison"]["divergent_count"] == 0

    cmp = compare_column_maps({
        "deterministic": {"email": {"is_pii": True, "entity_type": "EMAIL"}},
        "single_llm": {"email": {"is_pii": False, "entity_type": ""}},
    })
    assert cmp["divergent_count"] == 1


def test_write_llm_call_files_dual_copy(tmp_path):
    rec = LLMCallEvidence(
        call_id="abc123",
        system_prompt="sys",
        user_prompt="contact me at ada@example.com",
        response_text="ok",
        config_sanitized={"api_key": "should-already-be-gone", "table": "db.t"},
    )
    spool = tmp_path / "spool"
    paths = write_llm_call_files(tmp_path, rec, seq=1, spool_dir=spool, run_id="r1")
    raw_path = Path(paths["raw"])
    share_path = Path(paths["shareable"])
    assert raw_path.name.endswith(".raw.json")
    assert share_path.name.endswith(".json") and not share_path.name.endswith(".raw.json")
    assert str(spool) in str(raw_path)
    assert list((tmp_path / "llm_calls").glob("*.raw.json")) == []
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    share = json.loads(share_path.read_text(encoding="utf-8"))
    assert raw["user_prompt"] == "contact me at ada@example.com"
    assert "ada@example.com" not in share["user_prompt"]
    assert "api_key" not in raw["config_sanitized"]
    assert raw["sensitivity"]["egress"] == "deny"
    assert share["sensitivity"]["egress"] == "allow"
    mode = raw_path.stat().st_mode & 0o777
    assert mode == 0o600
    assert is_raw_artifact(paths["rel_raw"])
    assert not is_raw_artifact(paths["rel_shareable"])


def test_build_manifest_coverage_phases():
    man = build_manifest(
        table="db.t",
        run_id="r1",
        artifacts={"evidence_bundle": "/tmp/eb.json", "pii_detections": "/tmp/pii.json"},
        run_profile=True,
        run_quality=False,
        run_pii=True,
        errors=[{"phase": "quality", "error": "skipped"}],
    )
    assert man["kind"] == "redibis.evidence_manifest"
    assert man["schema_version"] == SCHEMA_VERSION
    assert man["coverage"]["profile"]["status"] == "evaluated"
    assert man["coverage"]["quality"]["status"] == "skipped"
    assert man["coverage"]["pii"]["status"] == "evaluated"
    assert man["coverage"]["llm"]["status"] == "not_run"


def test_report_bundle_surfaces_evidence_write_failures(tmp_path, monkeypatch):
    def _fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("redibis.scan.report_bundle._write_evidence_bundle", _fail)
    result = ScanRunResult(run_id="r1", table="db.t", status="success")
    artifacts = ReportBundle(result, ScanConfig(table="db.t", run_pii=False, run_quality=False)).flush(tmp_path)
    assert "evidence_errors" in artifacts
    assert "disk full" in artifacts["evidence_errors"]
    assert (tmp_path / "evidence_manifest.json").is_file()


def test_upload_skips_raw_llm_and_local_evidence_bundle(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "evidence_bundle.json").write_text("{}", encoding="utf-8")
    (run_dir / "evidence_bundle.shareable.json").write_text("{}", encoding="utf-8")
    calls = run_dir / "llm_calls"
    calls.mkdir()
    (calls / "0001-x.raw.json").write_text("{}", encoding="utf-8")
    (calls / "0001-x.json").write_text("{}", encoding="utf-8")
    backend = LocalBackend(str(tmp_path / "store"))
    writer = RunOutputWriter(
        backend=backend, bucket="runs", workflow="scan", table="db.t", run_id="r1",
    )
    links, _errors = _upload_artifacts(writer, run_dir)
    keys = " ".join(links.values())
    assert "evidence_bundle.shareable.json" in keys
    assert "0001-x.json" in keys
    assert "0001-x.raw.json" not in keys
    assert not any(k.endswith("/evidence_bundle.json") for k in links.values())


@patch("redibis.scan.facades.ReportBundle.flush")
@patch("redibis.scan.facades.Scan.run")
def test_profile_scan_default_flush_uses_output_dir_run_id(mock_run, mock_flush, tmp_path):
    mock_run.return_value = ScanRunResult(run_id="r99", table="db.t", status="success")
    mock_flush.return_value = {"evidence_bundle": "x"}
    cfg = ScanConfig(table="db.t", output_dir=tmp_path)
    ProfileScan(cfg).run(pd.DataFrame({"a": [1]}))
    mock_flush.assert_called_once()
    dest = Path(mock_flush.call_args[0][0])
    assert dest == tmp_path / "r99"


def test_cli_evidence_coverage_and_llm(tmp_path, capsys):
    from redibis.cli.evidence_cmd import _run_evidence_coverage, _run_evidence_llm

    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    bundle = {
        "kind": "redibis.evidence_bundle",
        "schema_version": "2.0",
        "table": {"name": "db.t", "run_id": "r1"},
        "columns": {"email": {}},
        "table_summary": {"pii": {"detected_columns": 1}},
    }
    (run_dir / "evidence_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    (run_dir / "evidence_manifest.json").write_text(
        json.dumps({"table": "db.t", "run_id": "r1", "coverage": {"profile": {"status": "evaluated"}}}),
        encoding="utf-8",
    )
    calls = run_dir / "llm_calls"
    calls.mkdir()
    share = {"call_id": "c1", "user_prompt": "[REDACTED_EMAIL]"}
    raw = {"call_id": "c1", "user_prompt": "ada@example.com"}
    (calls / "0001-c1.json").write_text(json.dumps(share), encoding="utf-8")
    (calls / "0001-c1.raw.json").write_text(json.dumps(raw), encoding="utf-8")
    (calls / "index.json").write_text(
        json.dumps({
            "calls": [{
                "call_id": "c1",
                "shareable": "llm_calls/0001-c1.json",
                "raw": "llm_calls/0001-c1.raw.json",
            }]
        }),
        encoding="utf-8",
    )

    args = Namespace(table="db.t", run_id="r1", latest=False, out="-")
    assert _run_evidence_coverage(args, tmp_path) == 0
    cov = json.loads(capsys.readouterr().out)
    assert cov["coverage"]["profile"]["status"] == "evaluated"

    args_list = Namespace(table="db.t", run_id="r1", latest=False, out="-", call_id=None, raw=False, reason=None)
    assert _run_evidence_llm(args_list, tmp_path) == 0
    index = json.loads(capsys.readouterr().out)
    assert index["calls"][0]["call_id"] == "c1"

    args_one = Namespace(table="db.t", run_id="r1", latest=False, out="-", call_id="c1", raw=False, reason=None)
    assert _run_evidence_llm(args_one, tmp_path) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["user_prompt"] == "[REDACTED_EMAIL]"

    args_raw_bad = Namespace(table="db.t", run_id="r1", latest=False, out="-", call_id="c1", raw=True, reason="", actor="")
    assert _run_evidence_llm(args_raw_bad, tmp_path) == 2

    args_raw_no_actor = Namespace(table="db.t", run_id="r1", latest=False, out="-", call_id="c1", raw=True, reason="debug", actor="")
    assert _run_evidence_llm(args_raw_no_actor, tmp_path) == 2

    args_raw = Namespace(table="db.t", run_id="r1", latest=False, out="-", call_id="c1", raw=True, reason="debug", actor="steward")
    assert _run_evidence_llm(args_raw, tmp_path) == 0
    shown_raw = json.loads(capsys.readouterr().out)
    assert shown_raw["user_prompt"] == "ada@example.com"
    audit = tmp_path / "_restricted_evidence" / "_audit" / "restricted_reads.jsonl"
    assert audit.is_file()
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert row["actor"] == "steward"
    assert row["reason"] == "debug"
    assert row["call_id"] == "c1"
    assert row["outcome"] == "ok"


def test_shareable_omits_names_and_addresses():
    from redibis.evidence.redact import shareable_llm_call

    rec = {
        "system_prompt": "You are a steward.",
        "user_prompt": "John Smith lives at 12 Oak Street, Cairo. NID 28401011234567",
        "response_text": "classified",
        "parsed_result": {"name": "John Smith", "nid": "28401011234567"},
        "context": {"sample": "Jane Doe, 10 Downing Street"},
        "hashes": {},
    }
    share = shareable_llm_call(rec)
    blob = json.dumps(share)
    assert "John Smith" not in blob
    assert "28401011234567" not in blob
    assert "12 Oak Street" not in blob or share["sensitivity"]["sample_mode"] in {"redacted", "omitted"}
    assert share["sensitivity"]["contains_raw_pii"] is False
    assert share["hashes"]["user_prompt"]


def test_manifest_has_relative_integrity_not_host_paths(tmp_path):
    result = ScanRunResult(
        run_id="r1", table="db.t", status="success",
        col_dtypes={"email": "string"},
        total_columns=1,
    )
    cfg = ScanConfig(table="db.t", run_pii=False, run_quality=False, run_profile=True)
    artifacts = ReportBundle(result, cfg).flush(tmp_path)
    man = json.loads(Path(artifacts["evidence_manifest"]).read_text(encoding="utf-8"))
    assert man["source_files"]
    assert all(not Path(p).is_absolute() for p in man["source_files"])
    bundle_ref = man["artifacts"].get("evidence_bundle") or {}
    assert isinstance(bundle_ref, dict)
    assert bundle_ref.get("sha256")
    assert bundle_ref.get("path") == "evidence_bundle.json"
    variants = json.loads(Path(artifacts["result_variants"]).read_text(encoding="utf-8"))
    modes = {v["mode"]: v for v in variants["variants"]}
    assert modes["deterministic"]["status"] == "evaluated"
    assert modes["deterministic"]["columns"]


def test_failed_scan_still_flushes(tmp_path):
    result = ScanRunResult(run_id="fail1", table="db.t", status="failed", error="boom")
    artifacts = ReportBundle(
        result, ScanConfig(table="db.t", run_pii=False, run_quality=False, run_profile=True),
    ).flush(tmp_path)
    assert (tmp_path / "evidence_manifest.json").is_file()
    assert artifacts.get("evidence_manifest")


def test_generic_engine_replay_prefers_engine_evidence_and_honors_ran_false():
    from redibis.scan.evidence_ops import detection_from_evidence
    from redibis.pii.equations import decide_pii
    from redibis.pii.thresholds import Thresholds

    block = {
        "pii_evidence": {
            "presidio": {"ran": True, "score": 0.2},
            "engine_evidence": {
                "presidio": {"engine_id": "presidio", "ran": True, "score": 0.91, "hits": [{"entity_type": "EMAIL"}]},
                "nid": {"engine_id": "nid", "ran": False, "score": 0.99, "reason": "skipped"},
                "imei": {"engine_id": "imei", "ran": True, "score": 0.88},
                "future_plugin": {"engine_id": "future_plugin", "ran": True, "score": 0.95, "kind": "ner"},
            },
        },
        "validator_results": {"v.eg_nid": {"rate": 0.99, "checked": 10}},
    }
    det = detection_from_evidence("col", block)
    assert det.presidio_score == pytest.approx(0.91)
    assert det.nid_valid_rate is None
    assert det.imei_valid_rate == pytest.approx(0.88)
    assert det.engine_evidence["future_plugin"]["score"] == pytest.approx(0.95)
    decided = decide_pii(det, "lenient", Thresholds())
    # plugin-only score must not be the reason a column is PII by itself
    assert decided.detected is True  # presidio 0.91 votes in lenient
    plugin_only = detection_from_evidence("other", {
        "pii_evidence": {
            "engine_evidence": {
                "future_plugin": {"engine_id": "future_plugin", "ran": True, "score": 0.99},
            }
        }
    })
    plugin_decided = decide_pii(plugin_only, "lenient", Thresholds())
    assert plugin_decided.detected is False


def test_latest_uses_completion_timestamp_not_lexical(tmp_path):
    from redibis.scan.evidence_ops import load_evidence_bundle

    older = tmp_path / "zzz_lexical_latest"
    newer = tmp_path / "aaa_lexical_older"
    older.mkdir()
    newer.mkdir()
    def _bundle(run_id, finished):
        return {
            "kind": "redibis.evidence_bundle",
            "table": {"name": "db.t", "run_id": run_id},
            "timestamps": {"scan_finished_at": finished, "bundle_created_at": finished},
            "columns": {},
        }
    (older / "evidence_bundle.json").write_text(json.dumps(_bundle("zzz_lexical_latest", "2020-01-01T00:00:00+00:00")))
    (newer / "evidence_bundle.json").write_text(json.dumps(_bundle("aaa_lexical_older", "2026-08-01T00:00:00+00:00")))
    bundle, hint = load_evidence_bundle(table="db.t", latest=True, output_dir=tmp_path)
    assert bundle["table"]["run_id"] == "aaa_lexical_older"
    assert "aaa_lexical_older" in hint


def test_bare_run_id_for_session_prefixed_evidence(tmp_path):
    from redibis.store.run_output_writer import evidence_storage_key

    key = evidence_storage_key("db.t", "session-abc/run-1")
    assert key == "_meta/evidence/db.t/run-1.json"
    assert "/" not in key.split("db.t/")[1].replace(".json", "") or key.endswith("run-1.json")


def test_upload_failure_is_in_manifest(tmp_path):
    class _Boom:
        def write_file(self, name, path):
            raise OSError("s3 down")

        def write(self, name, content):
            raise OSError("s3 down")

        def write_evidence(self, content):
            raise OSError("s3 down")

    result = ScanRunResult(run_id="r1", table="db.t", status="success")
    artifacts = ReportBundle(
        result, ScanConfig(table="db.t", run_pii=False, run_quality=False, run_profile=False),
    ).flush(tmp_path, run_writer=_Boom())
    man = json.loads(Path(artifacts["evidence_manifest"]).read_text(encoding="utf-8"))
    assert man.get("errors") or "evidence_errors" in artifacts


def test_get_cmd_non_zip_passes_path(tmp_path):
    from redibis.cli import get_cmd
    from argparse import Namespace

    class _Backend:
        def __init__(self):
            self.store = {
                "scan/db_t/r1/evidence_manifest.json": {"table": "db.t"},
                "scan/db_t/r1/llm_calls/0001-c1.json": {"call_id": "c1", "user_prompt": "[REDACTED]"},
            }

        def list_keys(self, bucket, prefix=""):
            return [k for k in self.store if k.startswith(prefix)]

        def exists(self, bucket, key):
            return key in self.store

        def get_json(self, bucket, key):
            return self.store[key]

        def get_yaml(self, bucket, key):
            return self.store[key]

        def get_text(self, bucket, key):
            return json.dumps(self.store[key])

    class _Store:
        backend = _Backend()
        config = type("C", (), {"runs_bucket": "runs"})()

    out = tmp_path / "got"
    args = Namespace(get_action="llm-call-logs", run_or_session_id="r1", zip=None, out=str(out))
    rc = get_cmd.run_get(args, _Store(), _Store().backend)
    assert rc == 0
    assert (out / "evidence_manifest.json").is_file()
    assert (out / "llm_calls" / "0001-c1.json").is_file()
    assert not list(out.rglob("*.raw.json"))


def test_config_hash_bundle_matches_manifest(tmp_path):
    from redibis.scan.evidence_bundle import _config_sha256

    cfg = ScanConfig(table="db.t", run_pii=True, run_quality=False)
    result = ScanRunResult(run_id="r1", table="db.t", status="success")
    artifacts = ReportBundle(result, cfg).flush(tmp_path)
    man = json.loads(Path(artifacts["evidence_manifest"]).read_text(encoding="utf-8"))
    assert man["config_hash"] == _config_sha256(cfg)


def test_agentic_lifecycle_comparison(tmp_path):
    from redibis.contracts.lifecycle import write_llm_lifecycle_artifacts

    class _Writer:
        def __init__(self):
            self.payloads = {}

        def write(self, name, content):
            self.payloads[name] = content
            return name

    writer = _Writer()
    det = {"name": "db.t", "schema": [{"properties": [{"name": "email", "classification": "pii.sensitive", "entity_type": "EMAIL"}]}]}
    llm = {"name": "db.t", "schema": [{"properties": [{"name": "email", "classification": "pii.sensitive", "entity_type": "EMAIL"}]}]}
    write_llm_lifecycle_artifacts(
        writer, det, llm, run_id="e1",
        agentic=True,
        agentic_steps=[{"step_id": "s1", "kind": "column_definitions", "attempt": 1}],
    )
    variants = {v["mode"]: v for v in writer.payloads["result_variants.json"]["variants"]}
    assert variants["agentic"]["status"] == "evaluated"
    assert variants["single_llm"]["status"] == "not_run"
    assert variants["agentic"]["steps"]

