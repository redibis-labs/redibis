"""Gateway scan JSON and use cases coerce into portable eval datasets."""

from __future__ import annotations

import json

from redibis.pii.eval import DATASET_KIND, coerce_eval_dataset, from_gateway_scan, from_usecase
from redibis.pii.eval.runner import evaluate_with_service
from redibis.pii.eval.span_metrics import validate_dataset


def test_from_gateway_scan_keeps_text_offsets_and_rejected():
    raw = {
        "run_uuid": "run-abc",
        "text": "Call 01522345678 now",
        "text_meta": {"language": "en"},
        "analysers": {
            "pii": {
                "spans": [
                    {
                        "start": 5,
                        "end": 16,
                        "entity_type": "PHONE_NUMBER",
                        "text": "01522345678",
                    }
                ],
                "rejected_spans": [
                    {"start": 0, "end": 4, "entity_type": "PERSON", "text": "Call"}
                ],
            }
        },
    }
    ds = from_gateway_scan(raw)
    assert ds is not None
    assert ds["kind"] == DATASET_KIND
    case = ds["cases"][0]
    assert case["text"] == "Call 01522345678 now"
    span = case["expected_spans"][0]
    assert span["start"] == 5
    assert span["end"] == 16
    assert span["entity_type"] == "PHONE_NUMBER"
    assert span["value"] == "01522345678"
    assert case["text"][span["start"]:span["end"]] == span["value"]
    assert case["forbidden_spans"][0]["entity_type"] == "PERSON"


def test_scan_without_text_cannot_coerce():
    assert from_gateway_scan({"analysers": {"pii": {"spans": []}}}) is None


def test_usecase_and_custom_type_round_trip():
    raw = {
        "kind": "redibis.text_usecase",
        "id": "ig",
        "text": "see instagram.com/me",
        "language": "en",
        "expected_spans": [
            {"start": 4, "end": 20, "entity_type": "SOCIAL_URL", "value": "instagram.com/me"}
        ],
    }
    ds = from_usecase(raw)
    assert ds["cases"][0]["expected_spans"][0]["entity_type"] == "SOCIAL_URL"
    normalized = validate_dataset(ds, allowed_entity_types={"EMAIL_ADDRESS", "SOCIAL_URL"})
    assert normalized["cases"][0]["expected_spans"][0]["entity_type"] == "SOCIAL_URL"


def test_coerce_eval_dataset_accepts_dataset_scan_and_usecase():
    dataset = {
        "kind": DATASET_KIND,
        "schema_version": "1.2",
        "offset_unit": "unicode_codepoint",
        "cases": [{"id": "c1", "text": "x", "expected_spans": []}],
    }
    assert coerce_eval_dataset(dataset)["cases"][0]["id"] == "c1"
    scan = {
        "text": "alice@example.com",
        "analysers": {
            "pii": {
                "spans": [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS"}],
            }
        },
    }
    coerced = coerce_eval_dataset(scan)
    assert coerced["cases"][0]["expected_spans"][0]["value"] == "alice@example.com"
    assert coerce_eval_dataset({"hello": "world"}) is None


def test_evaluate_with_service_allows_gold_custom_types():
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

    dataset = {
        "kind": DATASET_KIND,
        "schema_version": "1.2",
        "offset_unit": "unicode_codepoint",
        "cases": [{
            "id": "custom",
            "text": "see instagram.com/me",
            "language": "en",
            "expected_spans": [
                {"start": 4, "end": 20, "entity_type": "SOCIAL_URL"}
            ],
        }],
    }
    report = evaluate_with_service(_Svc(), dataset, options={"engines": "regex"})
    assert report["case_count"] == 1
    assert report["exact"]["micro"]["fn"] == 1


def test_cli_folder_scores_scan_json(tmp_path):
    from redibis.cli import main as cli_main
    from tests.test_cli_pii_eval import _args

    scan = {
        "text": "hello alice@example.com",
        "analysers": {
            "pii": {
                "spans": [{"start": 6, "end": 23, "entity_type": "EMAIL_ADDRESS", "text": "alice@example.com"}],
            }
        },
    }
    (tmp_path / "scan.json").write_text(json.dumps(scan), encoding="utf-8")
    out = tmp_path / "batch.json"
    rc = cli_main._run_pii_eval(_args(str(tmp_path), out=str(out)))
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["kind"] == "redibis.text_span_eval_batch_report"
    assert report["evaluated_count"] == 1
    assert report["failed_count"] == 0
