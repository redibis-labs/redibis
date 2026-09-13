"""CLI tests for ``redibis pii eval``."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from redibis.cli import main as cli_main
from redibis.pii.eval import DATASET_KIND


def _args(dataset, **extra):
    ns = dict(
        pii_action="eval",
        dataset=dataset,
        out=None,
        language="en",
        engines="regex",
        min_score=0.2,
        use_llm=False,
        llm_provider="",
        llm_model="",
        no_preprocess=True,
        overlap_iou=0.5,
        min_exact_f1=None,
        config=None,
        out_dir=None,
        html=None,
        recursive=False,
        fail_fast=False,
        rules=None,
        rules_defaults=True,
        draft_rules=None,
        pack=None,
        pack_stack=None,
        normalization="v1",
        tier="strict,value,overlap,type",
        gate_file=None,
        baseline=None,
        run_uuid=None,
        label="",
        provenance_out=None,
    )
    ns.update(extra)
    return SimpleNamespace(**ns)


def test_cli_pii_eval_writes_report(tmp_path, capsys):
    dataset = {
        "kind": DATASET_KIND,
        "schema_version": "1.0",
        "offset_unit": "unicode_codepoint",
        "cases": [
            {
                "id": "email",
                "text": "hello alice@example.com",
                "language": "en",
                "expected_spans": [
                    {"start": 6, "end": 23, "entity_type": "EMAIL_ADDRESS"}
                ],
            }
        ],
    }
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    out = tmp_path / "report.json"
    rc = cli_main._run_pii_eval(_args(str(path), out=str(out)))
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["kind"] == "redibis.text_span_eval_report"
    assert report["case_count"] == 1
    assert "exact" in report
    assert "value" in report
    assert report["provenance"]["rules_checksum"].startswith("sha256:")
    assert report["provenance"]["run_uuid"]
    assert report["normalization_profile"] == "v1"


def test_cli_pii_eval_threshold_fails(tmp_path, capsys):
    dataset = {
        "kind": DATASET_KIND,
        "schema_version": "1.0",
        "cases": [
            {
                "id": "none",
                "text": "no pii here",
                "expected_spans": [
                    {"start": 0, "end": 2, "entity_type": "EMAIL_ADDRESS"}
                ],
            }
        ],
    }
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    rc = cli_main._run_pii_eval(_args(str(path), min_exact_f1=0.99))
    assert rc == 1
    err = capsys.readouterr().err
    assert "exact micro F1" in err


def test_cli_pii_eval_folder_continues_on_invalid(tmp_path, capsys):
    good = {
        "kind": DATASET_KIND,
        "schema_version": "1.0",
        "cases": [{
            "id": "email",
            "text": "hello alice@example.com",
            "language": "en",
            "expected_spans": [{"start": 6, "end": 23, "entity_type": "EMAIL_ADDRESS"}],
        }],
    }
    (tmp_path / "ok.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "skip.json").write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    out = tmp_path / "batch.json"
    rc = cli_main._run_pii_eval(_args(str(tmp_path), out=str(out)))
    assert rc == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["kind"] == "redibis.text_span_eval_batch_report"
    assert report["evaluated_count"] == 1
    assert report["failed_count"] == 1
    assert {f["path"] for f in report["files"]} == {"ok.json", "skip.json"}
    html_path = tmp_path / "report.html"
    rc_html = cli_main._run_pii_eval(
        _args(str(tmp_path / "ok.json"), html=str(html_path))
    )
    assert rc_html == 0
    html = html_path.read_text(encoding="utf-8")
    assert "<script>" not in html
    assert "alice@example.com" in html or "EMAIL_ADDRESS" in html


def test_folder_discovery_rejects_symlink_escape(tmp_path):
    from redibis.pii.eval.batch import discover_eval_files

    outside = tmp_path.parent / "outside-eval.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    assert discover_eval_files(tmp_path) == []


def test_folder_eval_cancels_between_files(tmp_path):
    import threading

    from redibis.pii.eval.batch import evaluate_path
    from redibis.pii.eval.runner import EvalCancelled

    good = {
        "kind": DATASET_KIND,
        "schema_version": "1.0",
        "cases": [{"id": "a", "text": "x", "expected_spans": []}],
    }
    (tmp_path / "a.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({**good, "id": "second"}), encoding="utf-8")

    class _Svc:
        def entities(self):
            return {"entities": [{"entity_type": "EMAIL_ADDRESS"}]}

        def scan(self, *_args, **_kwargs):
            return type("R", (), {
                "detections": [],
                "engines_ran": ("regex",),
                "engines_unavailable": {},
                "ruleset_id": "builtin",
                "ruleset_version": "1",
            })()

    cancel = threading.Event()
    seen = []

    def progress(stage, detail):
        if stage == "file":
            seen.append(detail.get("name"))
            if len(seen) == 1:
                cancel.set()

    with pytest.raises(EvalCancelled):
        evaluate_path(_Svc(), tmp_path, progress_cb=progress, cancel_event=cancel)
    assert seen == ["a.json"]
