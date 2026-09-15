"""CLI tests for ``redibis pii text`` LLM flags and loud failure."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.cli import main as cli_main
from redibis.services.text_pii_service import TextPIIServiceError


def _args(**overrides):
    base = dict(
        pii_action="text",
        text="hello alice@example.com",
        file=None,
        language="en",
        engines="regex",
        min_score=0.2,
        resolve="priority",
        entities=None,
        use_llm=False,
        llm_provider="",
        llm_model="",
        llm_api_key="",
        llm_endpoint="",
        require_llm=False,
        require_ner=False,
        equation="independent",
        json=True,
        format=None,
        redact=False,
        no_text=False,
        config=None,
        explain=None,
        provenance_out=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_llm_provider_flag_reaches_the_service(monkeypatch):
    seen = {}

    def fake_scan(self, text, **kwargs):
        seen.update(kwargs)
        from redibis.pii.scan.result import DetectionResult
        return DetectionResult(
            kind="span",
            detections=(),
            entity_counts={},
            ruleset_id="t",
            ruleset_version="1",
            language="en",
            engines_ran=("regex", "llm"),
        )

    monkeypatch.setattr(
        "redibis.services.text_pii_service.TextPIIService.scan", fake_scan,
    )
    rc = cli_main._run_pii_text(_args(
        use_llm=True, llm_provider="sglang", llm_model="qwen2.5-7b", json=True,
    ))
    assert rc == 0
    assert seen["use_llm"] is True
    assert seen["llm_provider"] == "sglang"
    assert seen["llm_model"] == "qwen2.5-7b"


def test_llm_endpoint_reaches_get_provider(monkeypatch):
    captured = {}

    def fake_get_provider(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return SimpleNamespace(name=name, model=kwargs.get("model"), complete=lambda s, p: '{"spans":[]}')

    monkeypatch.setattr("redibis.enrich.providers.get_provider", fake_get_provider)
    from redibis.services.text_pii_service import TextPIIService

    svc = TextPIIService()
    svc._build_llm_override(
        "sglang", "qwen2.5-7b", endpoint_url="http://127.0.0.1:8000/v1",
    )
    assert captured["name"] == "sglang"
    assert captured["endpoint_url"] == "http://127.0.0.1:8000/v1"


def test_provider_flag_implies_use_llm(monkeypatch):
    seen = {}

    def fake_scan(self, text, **kwargs):
        seen.update(kwargs)
        from redibis.pii.scan.result import DetectionResult
        return DetectionResult(
            kind="span", detections=(), entity_counts={},
            ruleset_id="t", ruleset_version="1", language="en",
            engines_ran=("regex",),
            engines_unavailable={"llm": "no LLM refiner attached"},
        )

    monkeypatch.setattr(
        "redibis.services.text_pii_service.TextPIIService.scan", fake_scan,
    )
    rc = cli_main._run_pii_text(_args(use_llm=False, llm_provider="ollama", json=True))
    assert seen["use_llm"] is True
    assert rc == 0  # require_llm not set


def test_cloud_provider_is_refused_on_the_free_text_path(capsys):
    rc = cli_main._run_pii_text(_args(
        use_llm=True, llm_provider="openai", llm_model="gpt-4o-mini", json=True,
    ))
    err = capsys.readouterr().err
    assert rc != 0
    assert "not allowed" in err.lower() or "cloud" in err.lower()


def test_require_llm_exits_3_when_no_refiner_is_attached(capsys, monkeypatch):
    def fake_scan(self, text, **kwargs):
        from redibis.pii.scan.result import DetectionResult
        return DetectionResult(
            kind="span", detections=(), entity_counts={},
            ruleset_id="t", ruleset_version="1", language="en",
            engines_ran=("regex",),
            engines_unavailable={"llm": "no LLM refiner attached"},
        )

    monkeypatch.setattr(
        "redibis.services.text_pii_service.TextPIIService.scan", fake_scan,
    )
    rc = cli_main._run_pii_text(_args(
        use_llm=True, require_llm=True, json=True, engines="regex",
    ))
    captured = capsys.readouterr()
    assert rc == 3
    assert "did not run" in captured.err.lower() or "llm" in captured.err.lower()


def test_default_invocation_is_unchanged(tmp_path, capsys):
    note = tmp_path / "note.txt"
    note.write_text("مرحبا alice@example.com", encoding="utf-8")
    from redibis.services.text_pii_service import TextPIIService

    # Pin regex so a local GLiNER/LLM install cannot make CLI and library
    # construction diverge (CLI auto-selects NER; a bare TextPIIService does not).
    expected = TextPIIService().scan(
        note.read_text(encoding="utf-8"),
        language="ar",
        engines="regex",
        return_text=True,
    ).to_dict()
    rc = cli_main._run_pii_text(_args(
        text=None,
        file=str(note),
        language="ar",
        engines="regex",
        min_score=0.35,
        json=True,
    ))
    assert rc == 0
    got = json.loads(capsys.readouterr().out)
    for span in got.get("spans") or []:
        assert "agreement" not in span
        assert "llm_verdict" not in span
    assert "arbitration" not in got
    assert got["spans"] == expected["spans"]
    assert got["engines_ran"] == expected["engines_ran"]
    assert got["entity_counts"] == expected["entity_counts"]


def test_api_key_never_appears_in_stdout_stderr_or_logs(capsys, caplog, monkeypatch):
    secret = "sk-test-super-secret-key-9f3a"
    caplog.set_level(logging.DEBUG)

    def fake_scan(self, text, **kwargs):
        from redibis.pii.scan.result import DetectionResult
        assert kwargs.get("llm_api_key") == secret
        return DetectionResult(
            kind="span", detections=(), entity_counts={},
            ruleset_id="t", ruleset_version="1", language="en",
            engines_ran=("regex", "llm"),
        )

    monkeypatch.setattr(
        "redibis.services.text_pii_service.TextPIIService.scan", fake_scan,
    )
    rc = cli_main._run_pii_text(_args(
        use_llm=True,
        llm_provider="ollama",
        llm_api_key=secret,
        json=True,
        engines="regex",
        require_llm=False,
    ))
    captured = capsys.readouterr()
    blob = captured.out + captured.err + caplog.text
    assert secret not in blob
    assert rc == 0


def test_directory_file_hint(tmp_path, capsys):
    d = tmp_path / "notes"
    d.mkdir()
    with pytest.raises(SystemExit) as ei:
        cli_main._pii_text_input(SimpleNamespace(file=str(d), text=None))
    assert ei.value.code == 2
    assert "text-batch" in capsys.readouterr().err


def test_text_llm_check_help():
    with pytest.raises(SystemExit) as ei:
        cli_main.main(["pii", "text-llm-check", "--help"])
    assert ei.value.code == 0
