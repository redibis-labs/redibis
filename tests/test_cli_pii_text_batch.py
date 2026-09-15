"""CLI tests for ``redibis pii text-batch``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.cli import main as cli_main
from redibis.pii.scan.result import DetectionResult
from redibis.pii.text_batch import (
    BatchDocument,
    BatchRunConfig,
    build_scan_report,
    run_text_batch,
    write_findings_csv,
)
from redibis.services.text_pii_service import TextPIIService


def test_directory_input_scans_every_matching_file(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("mail a@x.com", encoding="utf-8")
    (notes / "b.txt").write_text("mail b@x.com", encoding="utf-8")
    (notes / "skip.md").write_text("mail c@x.com", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch",
        "--input", str(notes),
        "--glob", "*.txt",
        "--engines", "regex",
        "--out-dir", str(out),
        "--quiet",
    ])
    assert rc == 0
    docs = list((out / "documents").glob("*.json"))
    assert len(docs) == 2
    report = json.loads((out / "scan-report.json").read_text(encoding="utf-8"))
    assert report["aggregates"]["documents"]["scanned"] == 2


def test_jsonl_and_csv_inputs_use_the_id_field(tmp_path):
    jsonl = tmp_path / "notes.jsonl"
    jsonl.write_text(
        '{"id":"t1","body":"a@x.com"}\n{"id":"t2","body":"b@x.com"}\n',
        encoding="utf-8",
    )
    out = tmp_path / "outj"
    rc = cli_main.main([
        "pii", "text-batch", "--jsonl", str(jsonl),
        "--text-field", "body", "--id-field", "id",
        "--engines", "regex", "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    names = {p.stem for p in (out / "documents").glob("*.json")}
    assert names == {"t1", "t2"}

    csv_path = tmp_path / "tickets.csv"
    csv_path.write_text("ticket_id,note\nA1,c@x.com\nA2,d@x.com\n", encoding="utf-8")
    outc = tmp_path / "outc"
    rc = cli_main.main([
        "pii", "text-batch", "--csv", str(csv_path),
        "--text-column", "note", "--id-column", "ticket_id",
        "--engines", "regex", "--out-dir", str(outc), "--quiet",
    ])
    assert rc == 0
    names = {p.stem for p in (outc / "documents").glob("*.json")}
    assert names == {"A1", "A2"}


def test_duplicate_ids_abort_before_scanning(tmp_path, capsys):
    jsonl = tmp_path / "dup.jsonl"
    jsonl.write_text(
        '{"id":"same","body":"a"}\n{"id":"same","body":"b"}\n',
        encoding="utf-8",
    )
    rc = cli_main.main([
        "pii", "text-batch", "--jsonl", str(jsonl), "--id-field", "id",
        "--text-field", "body", "--engines", "regex",
        "--out-dir", str(tmp_path / "out"), "--quiet",
    ])
    assert rc == 2
    assert "duplicate" in capsys.readouterr().err.lower()


def test_symlink_outside_input_root_is_not_scanned(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret@x.com", encoding="utf-8")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "ok.txt").write_text("visible@x.com", encoding="utf-8")
    (notes / "link.txt").symlink_to(secret)
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    names = {p.stem for p in (out / "documents").glob("*.json")}
    assert names == {"ok.txt"}
    blob = (out / "scan-report.json").read_text(encoding="utf-8")
    assert "secret@x.com" not in blob


def test_one_malformed_document_does_not_stop_the_run(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "ok.txt").write_text("ok@x.com", encoding="utf-8")
    bad = notes / "bad.txt"
    bad.write_bytes(b"\xff\xfe not utf8 \xff")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    errors = json.loads((out / "errors.json").read_text(encoding="utf-8"))
    assert errors
    ok = list((out / "documents").glob("*.json"))
    assert any("ok" in p.name for p in ok)


def test_resume_skips_completed_documents(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("a@x.com", encoding="utf-8")
    (notes / "b.txt").write_text("b@x.com", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out), "--resume", "--quiet",
    ])
    assert rc == 0
    report = json.loads((out / "scan-report.json").read_text(encoding="utf-8"))
    assert report["aggregates"]["documents"]["skipped"] == 2
    assert report["aggregates"]["documents"]["scanned"] == 0


def test_model_is_loaded_once_for_the_whole_run():
    class DummyNer:
        loads = 0

        def health_check(self):
            return {"loadable": True}

        def analyze_text(self, *args, **kwargs):
            if not getattr(self, "_loaded", False):
                DummyNer.loads += 1
                self._loaded = True
            return []

        @property
        def name(self):
            return "dummy"

    DummyNer.loads = 0
    ner = DummyNer()
    svc = TextPIIService(ner_backend=ner)
    docs = [
        BatchDocument(doc_id=f"d{i}", source=f"d{i}", text="hello there")
        for i in range(5)
    ]
    run_text_batch(
        svc, docs,
        BatchRunConfig(engines="ner", workers=1, quiet=True),
        out_dir=None,
    )
    assert DummyNer.loads == 1


def test_concurrent_workers_produce_identical_results():
    text = "Contact alice@example.com please"
    serial_docs = [
        BatchDocument(doc_id=f"d{i}", source=f"d{i}", text=text)
        for i in range(200)
    ]
    parallel_docs = [
        BatchDocument(doc_id=f"d{i}", source=f"d{i}", text=text)
        for i in range(200)
    ]
    svc = TextPIIService()
    serial = run_text_batch(
        svc, serial_docs,
        BatchRunConfig(engines="regex", workers=1, quiet=True, min_score=0.2),
        out_dir=None,
    )
    parallel = run_text_batch(
        svc, parallel_docs,
        BatchRunConfig(engines="regex", workers=8, quiet=True, min_score=0.2),
        out_dir=None,
    )

    def canon(run):
        rows = []
        for row in run.documents:
            if row.get("status") != "ok":
                continue
            spans = tuple(sorted(
                (
                    s.get("start"),
                    s.get("end"),
                    s.get("entity_type"),
                    s.get("engine"),
                    s.get("score"),
                    s.get("recognizer"),
                    s.get("validator"),
                )
                for s in (row.get("spans") or [])
            ))
            rows.append((row.get("id"), spans, tuple(sorted((row.get("entity_counts") or {}).items()))))
        return sorted(rows)

    serial_c = canon(serial)
    parallel_c = canon(parallel)
    assert len(serial_c) == 200
    assert serial_c == parallel_c


def test_batch_report_contains_no_source_digits_in_llm_reason(tmp_path):
    from redibis.pii.text_llm import LlmTextRefiner

    msisdn = "01001234567"
    text = f"please call {msisdn} today"
    start = text.find(msisdn)
    end = start + len(msisdn)

    class Stub:
        def complete(self, system, prompt):
            return json.dumps({
                "spans": [],
                "review": [{
                    "start": start,
                    "end": end,
                    "verdict": "PII",
                    "entity_type": "PHONE_NUMBER",
                    "confidence": 0.99,
                    "reason": f"this is {msisdn} a phone",
                }],
            })

    svc = TextPIIService()
    refiner = LlmTextRefiner(provider=Stub())
    svc._scanner._llm = refiner
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "ticket.txt").write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    docs = [BatchDocument(doc_id="ticket.txt", source=str(notes / "ticket.txt"), text=text)]
    cfg = BatchRunConfig(
        engines="regex",
        use_llm=True,
        equation="balanced",
        include_arbitration=True,
        workers=1,
        quiet=True,
        min_score=0.2,
        return_text=False,
    )
    run = run_text_batch(svc, docs, cfg, out_dir=out)
    report = build_scan_report(run, cfg=cfg, include_text=False)
    (out / "scan-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    payloads = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((out / "documents").glob("*.json"))
    ]
    write_findings_csv(payloads, out / "findings.csv", include_text=False)
    report_blob = (out / "scan-report.json").read_text(encoding="utf-8")
    findings = (out / "findings.csv").read_text(encoding="utf-8")
    assert msisdn not in report_blob
    assert msisdn not in findings
    doc_blob = "\n".join(p.read_text(encoding="utf-8") for p in (out / "documents").glob("*.json"))
    assert msisdn not in doc_blob
    assert "llm_reason" in findings.splitlines()[0]

def test_llm_circuit_breaker_trips_and_run_exits_nonzero_under_require_llm():
    class FakeSvc:
        def scan(self, text, **kwargs):
            unavail = {}
            ran = ["regex"]
            if kwargs.get("use_llm"):
                unavail["llm"] = "connection refused"
            return DetectionResult(
                kind="span",
                detections=(),
                entity_counts={},
                ruleset_id="t",
                ruleset_version="1",
                language="en",
                engines_ran=tuple(ran),
                engines_unavailable=unavail,
            )

        def get_policy(self, policy_id):
            return None

    docs = [BatchDocument(doc_id=f"d{i}", source=f"d{i}", text="hi") for i in range(8)]
    run = run_text_batch(
        FakeSvc(), docs,
        BatchRunConfig(
            use_llm=True, require_llm=True, quiet=True,
            llm_circuit_threshold=3, workers=1,
        ),
        out_dir=None,
    )
    assert run.exit_code == 3
    assert run.aggregates.get("llm_circuit_open") is True


def test_report_contains_no_matched_text_by_default(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("reach me at alice@example.com thanks", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    report = (out / "scan-report.json").read_text(encoding="utf-8")
    assert "alice@example.com" not in report


def test_findings_csv_omits_the_text_column_under_no_text(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("alice@example.com", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out), "--no-text", "--quiet",
    ])
    assert rc == 0
    header = (out / "findings.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "text" not in header.split(",")


def test_arbitration_totals_appear_in_the_batch_report(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("alice@example.com", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--equation", "balanced",
        "--out-dir", str(out), "--quiet",
    ])
    assert rc == 0
    report = json.loads((out / "scan-report.json").read_text(encoding="utf-8"))
    assert "arbitration" in report["aggregates"]
    assert set(report["aggregates"]["arbitration"]) >= {
        "contested", "confirmed", "vetoed", "type_conflicts",
    }


def test_deidentify_writes_redacted_copies_and_never_into_the_input_dir(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.txt").write_text("mail alice@example.com now", encoding="utf-8")
    out = tmp_path / "out"
    rc = cli_main.main([
        "pii", "text-batch", "--input", str(notes),
        "--engines", "regex", "--out-dir", str(out),
        "--deidentify", "--policy-id", "full-redact", "--quiet",
    ])
    assert rc == 0
    redacted = list((out / "redacted").glob("*.txt"))
    assert redacted
    assert "alice@example.com" not in redacted[0].read_text(encoding="utf-8")
    assert not (notes / "a.txt.txt").exists()
    assert list(notes.glob("*.txt")) == [notes / "a.txt"]
