"""Every guarded model call produces dual-copy evidence when a recorder is bound."""

from __future__ import annotations

import json

import pytest

from redibis.config import RAIConfig
from redibis.evidence.persist import is_raw_artifact
from redibis.telemetry.llm_evidence import llm_evidence_recorder
from redibis.telemetry.model_gateway import guarded_model_call


def test_guarded_model_call_writes_one_dual_copy_record(tmp_path):
    spool = tmp_path / "spool"
    with llm_evidence_recorder(
        run_dir=tmp_path, run_id="r1", table="db.t", execution_mode="single_llm",
        spool_dir=spool,
    ):
        result, _ = guarded_model_call(
            lambda: "hello ada@example.com",
            model_id="demo",
            system_prompt="You are a test.",
            user_prompt="Say hello to ada@example.com",
            rai_config=RAIConfig(enabled=False),
            model_role="contract.enrichment",
        )
    assert result == "hello ada@example.com"
    assert list((tmp_path / "llm_calls").glob("*.raw.json")) == []
    share_files = [
        p for p in (tmp_path / "llm_calls").glob("*.json")
        if not p.name.endswith(".raw.json") and p.name != "index.json"
    ]
    raw_files = list((spool / "r1" / "llm_calls").glob("*.raw.json"))
    assert len(raw_files) == 1
    assert len(share_files) == 1
    raw = json.loads(raw_files[0].read_text(encoding="utf-8"))
    share = json.loads(share_files[0].read_text(encoding="utf-8"))
    assert raw["system_prompt"] == "You are a test."
    assert "ada@example.com" in raw["user_prompt"]
    assert "ada@example.com" not in share["user_prompt"]
    assert "api_key" not in json.dumps(raw)
    assert raw["model_role"] == "contract.enrichment"
    mode = raw_files[0].stat().st_mode & 0o777
    assert mode == 0o600
    index = json.loads((tmp_path / "llm_calls" / "index.json").read_text(encoding="utf-8"))
    assert len(index["calls"]) == 1
    assert "raw" not in index["calls"][0]
    assert "spool" not in index["calls"][0]
    assert index["calls"][0]["execution_mode"] == "single_llm"
    assert not is_raw_artifact(index["calls"][0]["shareable"])
    full = json.loads((spool / "r1" / "llm_calls" / "index.json").read_text(encoding="utf-8"))
    assert is_raw_artifact(full["calls"][0]["raw"])


def test_nested_recorder_reuses_parent_and_keeps_lineage(tmp_path):
    spool = tmp_path / "spool"
    with llm_evidence_recorder(
        run_dir=tmp_path, run_id="r1", execution_mode="agentic", spool_dir=spool,
    ) as outer:
        guarded_model_call(
            lambda: "one",
            model_id="m",
            system_prompt="s1",
            user_prompt="u1",
            rai_config=RAIConfig(enabled=False),
            step_id="stage_a",
            attempt=1,
        )
        with llm_evidence_recorder(run_dir=tmp_path / "nested", execution_mode="single_llm"):
            guarded_model_call(
                lambda: "two",
                model_id="m",
                system_prompt="s2",
                user_prompt="u2",
                rai_config=RAIConfig(enabled=False),
                step_id="stage_b",
                attempt=2,
                parent_call_id="parent",
            )
        assert outer.seq == 2
    files = list((spool / "r1" / "llm_calls").glob("*.raw.json"))
    assert len(files) == 2
    assert not (tmp_path / "nested" / "llm_calls").exists()
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(files)]
    assert {p["step_id"] for p in payloads} == {"stage_a", "stage_b"}
    assert any(p["attempt"] == 2 and p["parent_call_id"] == "parent" for p in payloads)
    assert all(p["execution_mode"] == "agentic" for p in payloads)


def test_blocked_and_error_calls_are_still_recorded(tmp_path):
    spool = tmp_path / "spool"
    with llm_evidence_recorder(run_dir=tmp_path, run_id="r1", spool_dir=spool):
        with pytest.raises(PermissionError):
            guarded_model_call(
                lambda: (_ for _ in ()).throw(PermissionError("blocked by RAI")),
                model_id="m",
                user_prompt="secret",
                rai_config=RAIConfig(enabled=False),
            )
        with pytest.raises(RuntimeError):
            guarded_model_call(
                lambda: (_ for _ in ()).throw(RuntimeError("provider down")),
                model_id="m",
                user_prompt="retry",
                rai_config=RAIConfig(enabled=False),
            )
    raw_files = sorted((spool / "r1" / "llm_calls").glob("*.raw.json"))
    assert len(raw_files) == 2
    statuses = {json.loads(p.read_text(encoding="utf-8"))["status"] for p in raw_files}
    assert statuses == {"blocked", "error"}
    blocked = next(
        json.loads(p.read_text(encoding="utf-8")) for p in raw_files
        if json.loads(p.read_text(encoding="utf-8"))["status"] == "blocked"
    )
    assert blocked["blocked"] is True


def test_recorder_never_uploads_raw_copy(tmp_path):
    class _Writer:
        def __init__(self):
            self.names: list[str] = []

        def write(self, name, content):
            self.names.append(name)
            return name

    writer = _Writer()
    with llm_evidence_recorder(run_dir=tmp_path, run_writer=writer, run_id="r1", spool_dir=tmp_path / "spool"):
        guarded_model_call(
            lambda: "ok",
            model_id="m",
            system_prompt="s",
            user_prompt="u",
            rai_config=RAIConfig(enabled=False),
        )
    assert writer.names
    assert all(not n.endswith(".raw.json") for n in writer.names)
    assert all(n.endswith(".json") for n in writer.names)


def test_resume_continues_seq_and_appends_index(tmp_path):
    calls = tmp_path / "llm_calls"
    calls.mkdir()
    (calls / "index.json").write_text(
        json.dumps({
            "calls": [{"seq": 2, "call_id": "old", "shareable": "llm_calls/0002-old.json", "raw": "llm_calls/0002-old.raw.json"}],
            "warnings": [],
        }),
        encoding="utf-8",
    )
    with llm_evidence_recorder(run_dir=tmp_path, run_id="r1", spool_dir=tmp_path / "spool"):
        guarded_model_call(
            lambda: "ok",
            model_id="m",
            user_prompt="next",
            rai_config=RAIConfig(enabled=False),
        )
    index = json.loads((calls / "index.json").read_text(encoding="utf-8"))
    assert len(index["calls"]) == 2
    assert index["calls"][0]["call_id"] == "old"
    assert index["calls"][-1]["seq"] == 3


def test_persist_failure_warns_and_keeps_model_result(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("redibis.telemetry.llm_evidence.write_llm_call_files", _boom)
    with llm_evidence_recorder(run_dir=tmp_path, run_id="r1", spool_dir=tmp_path / "spool") as rec:
        result, _ = guarded_model_call(
            lambda: "ok",
            model_id="m",
            user_prompt="u",
            rai_config=RAIConfig(enabled=False),
        )
    assert result == "ok"
    assert rec.warnings
    assert any("disk full" in str(w.get("error")) for w in rec.warnings)


def test_unbound_call_persists_exact_to_spool(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    monkeypatch.setattr("redibis.telemetry.llm_evidence.resolve_spool_dir", lambda config=None: spool)
    guarded_model_call(
        lambda: "ok",
        model_id="m",
        user_prompt="exact-secret-value",
        rai_config=RAIConfig(enabled=False),
        run_id="rid-1",
        model_role="pii.refiner",
    )
    raw_files = list((spool / "rid-1" / "llm_calls").glob("*.raw.json"))
    assert raw_files
    body = raw_files[0].read_text(encoding="utf-8")
    assert "exact-secret-value" in body


def test_prompt_context_shareable_omits_exact_contract(tmp_path):
    from redibis.enrich.service import _shareable_prompt_context, _write_prompt_context_artifact

    class _Writer:
        def __init__(self):
            self.payload = None

        def write(self, name, content):
            self.payload = content
            return name

    writer = _Writer()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "redibis.telemetry.llm_evidence.persist_restricted_artifact",
        lambda *a, **k: tmp_path / "raw.json",
    )
    try:
        _write_prompt_context_artifact(
            writer,
            table="db.t",
            run_id="r1",
            provider_name="demo",
            model="m",
            system_prompt="System with ada@example.com",
            user_prompt="John Smith NID 28401011234567",
            prompt_redacted=False,
            contract_for_prompt={"schema": [{"properties": [{"name": "email", "samples": ["ada@example.com"]}]}]},
            context_docs=[],
            example_docs=[],
            sample_data=["secret.csv"],
            example_contracts=[],
            memory_context=False,
        )
    finally:
        monkeypatch.undo()
    assert writer.payload is not None
    blob = json.dumps(writer.payload)
    assert "28401011234567" not in blob
    assert "samples" not in json.dumps(writer.payload.get("contract_for_prompt") or {})
    share = _shareable_prompt_context({
        "system_prompt": "s",
        "user_prompt": "John Smith",
        "docs": {},
        "prompt_chars": 1,
    })
    assert "John Smith" not in json.dumps(share)
