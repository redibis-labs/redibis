"""Deterministic scoring for portable text-span evaluation datasets."""

from __future__ import annotations

import pytest

from redibis.pii.eval import (
    DATASET_KIND,
    DatasetValidationError,
    evaluate_case,
    evaluate_dataset,
    validate_dataset,
)


def _dataset(cases):
    return {
        "kind": DATASET_KIND,
        "schema_version": "1.0",
        "offset_unit": "unicode_codepoint",
        "id": "unit",
        "cases": cases,
    }


def test_validate_rejects_bad_kind_and_duplicates():
    with pytest.raises(DatasetValidationError, match="kind"):
        validate_dataset({"kind": "nope", "schema_version": "1.0", "cases": []})
    with pytest.raises(DatasetValidationError, match="duplicate"):
        validate_dataset(
            _dataset(
                [
                    {"id": "a", "text": "ab", "expected_spans": []},
                    {"id": "a", "text": "cd", "expected_spans": []},
                ]
            )
        )


def test_validate_codepoints_and_unknown_entity():
    text = "👍Alice"
    with pytest.raises(DatasetValidationError, match="invalid range"):
        validate_dataset(
            _dataset(
                [
                    {
                        "id": "emoji",
                        "text": text,
                        "expected_spans": [{"start": 0, "end": 9, "entity_type": "PERSON"}],
                    }
                ]
            )
        )
    with pytest.raises(DatasetValidationError, match="unknown entity_type"):
        validate_dataset(
            _dataset(
                [
                    {
                        "id": "t",
                        "text": "ab",
                        "expected_spans": [{"start": 0, "end": 2, "entity_type": "NOT_A_TYPE"}],
                    }
                ]
            ),
            allowed_entity_types={"EMAIL_ADDRESS"},
        )


def test_exact_match_and_overlap_and_proposals():
    case = {
        "id": "c1",
        "text": "alice@example.com extra",
        "expected_spans": [
            {"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS"},
        ],
    }
    exact = evaluate_case(
        case,
        [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS", "is_proposal": False}],
    )
    assert exact["exact"]["tp"] == 1
    assert exact["exact"]["f1"] == 1.0

    overlap = evaluate_case(
        case,
        [{"start": 0, "end": 16, "entity_type": "EMAIL_ADDRESS", "is_proposal": False}],
    )
    assert overlap["exact"]["tp"] == 0
    assert overlap["overlap"]["tp"] == 1

    proposals = evaluate_case(
        case,
        [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS", "is_proposal": True}],
    )
    assert proposals["exact"]["fn"] == 1
    assert proposals["proposal_spans"]


def test_evaluate_dataset_aggregates_micro():
    dataset = _dataset(
        [
            {
                "id": "hit",
                "text": "ab",
                "expected_spans": [{"start": 0, "end": 2, "entity_type": "PERSON"}],
            },
            {
                "id": "miss",
                "text": "cd",
                "expected_spans": [{"start": 0, "end": 2, "entity_type": "PERSON"}],
            },
        ]
    )
    report = evaluate_dataset(
        dataset,
        {
            "hit": [{"start": 0, "end": 2, "entity_type": "PERSON"}],
            "miss": [{"start": 0, "end": 2, "entity_type": "EMAIL_ADDRESS"}],
        },
        allowed_entity_types={"PERSON", "EMAIL_ADDRESS"},
    )
    assert report["case_count"] == 2
    assert report["exact"]["micro"]["tp"] == 1
    assert report["exact"]["micro"]["fp"] == 1
    assert report["exact"]["micro"]["fn"] == 1
    assert "PERSON" in report["exact"]["by_entity"]


def test_language_fallback_uses_caller_default():
    ds = validate_dataset(
        _dataset([{"id": "a", "text": "x", "expected_spans": []}]),
        default_language="ar",
    )
    assert ds["cases"][0]["language"] == "ar"
    assert ds["redibis_version"]
    with pytest.raises(DatasetValidationError, match="redibis_version"):
        validate_dataset(
            _dataset([{"id": "a", "text": "x", "expected_spans": []}]),
            require_redibis_version=True,
        )


def test_canonical_span_drops_matched_text():
    case = {"id": "c1", "text": "alice@example.com", "expected_spans": [
        {"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS", "text": "alice@example.com"}
    ]}
    scored = evaluate_case(case, [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS", "text": "alice@example.com"}])
    assert "text" not in scored["expected_spans"][0]
    assert "text" not in scored["predicted_spans"][0]


def test_html_escapes_and_truncates():
    from redibis.pii.eval import REPORT_KIND, render_report_html

    report = {
        "kind": REPORT_KIND,
        "schema_version": "1.0",
        "redibis_version": "0.0-test",
        "dataset_id": "xss",
        "exact": {"micro": {"f1": 1, "precision": 1, "recall": 1, "tp": 1, "fp": 0, "fn": 0}},
        "overlap": {"micro": {"f1": 1, "precision": 1, "recall": 1, "tp": 1, "fp": 0, "fn": 0}},
        "cases": [{
            "id": "c1",
            "text": "<script>alert(1)</script>" + ("x" * 9000),
            "language": "en",
            "expected_spans": [],
            "predicted_spans": [],
            "exact": {"f1": 1},
            "overlap": {"f1": 0.5},
        }],
    }
    html = render_report_html(report)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    overlap = render_report_html(report, metric_name="overlap")
    assert "overlap F1 0.5" in overlap
    with pytest.raises(ValueError, match="unsupported"):
        render_report_html({"kind": "nope"})


def test_eval_limiter_weight_and_cancel():
    from redibis.pii.eval.runner import EvalCancelled, eval_limiter_weight, evaluate_with_service

    assert eval_limiter_weight(1, 10) == 1
    assert eval_limiter_weight(25, 40_000, use_llm=True) >= 7

    class _Svc:
        def entities(self):
            return {"entities": [{"entity_type": "EMAIL_ADDRESS"}]}

        def scan(self, *args, **kwargs):
            raise AssertionError("scan should not run after cancel")

    import threading

    ev = threading.Event()
    ev.set()
    with pytest.raises(EvalCancelled):
        evaluate_with_service(
            _Svc(),
            _dataset([{"id": "a", "text": "x", "expected_spans": []}]),
            cancel_event=ev,
        )


def test_evaluate_attaches_llm_verdict_and_recommendations():
    from redibis.pii.eval.runner import evaluate_with_service

    seen = {}

    class _Result:
        detections = [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS"}]
        engines_ran = ("regex",)
        engines_unavailable = {}
        ruleset_id = "builtin"
        ruleset_version = "1"
        llm_verdict = {"kind": "redibis.llm_verdict", "spans": [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS"}]}
        recommendations = {
            "kind": "redibis.tuning_recommendations",
            "items": [{"target": "noise_terms", "value": "x"}],
        }

    class _Svc:
        def entities(self):
            return {"entities": [{"entity_type": "EMAIL_ADDRESS"}]}

        def scan(self, *_args, **kwargs):
            seen.update(kwargs)
            return _Result()

    report = evaluate_with_service(
        _Svc(),
        _dataset([{
            "id": "c1",
            "text": "alice@example.com",
            "expected_spans": [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS"}],
        }]),
        options={"llm_verdict": "independent", "recommend": True, "trim": True},
    )
    assert seen.get("llm_verdict") == "independent"
    assert seen.get("recommend") is True
    assert seen.get("trim") is True
    case = report["cases"][0]
    assert case["id"] == "c1"
    assert case["llm_verdict"]["kind"] == "redibis.llm_verdict"
    assert case["recommendations"]["items"][0]["target"] == "noise_terms"
    assert (report.get("exact") or report.get("strict") or {}).get("micro", {}).get("tp") == 1


def test_requested_llm_unavailable_fails_closed():
    from redibis.pii.eval.runner import evaluate_with_service

    class _Result:
        detections = []
        engines_ran = ("regex",)
        engines_unavailable = {"llm": "no refiner attached"}
        ruleset_id = "builtin"
        ruleset_version = "1"

    class _Svc:
        def entities(self):
            return {"entities": [{"entity_type": "EMAIL_ADDRESS"}]}

        def scan(self, *_args, **_kwargs):
            return _Result()

    with pytest.raises(DatasetValidationError, match="LLM refiner unavailable"):
        evaluate_with_service(
            _Svc(),
            _dataset([{"id": "a", "text": "x", "expected_spans": []}]),
            options={"use_llm": True},
        )


@pytest.mark.parametrize("version", ["1.0", "1.2"])
def test_optional_case_name_round_trips(version):
    raw = _dataset([
        {
            "id": "c1",
            "name": "Invoice email",
            "text": "alice@example.com",
            "expected_spans": [{"start": 0, "end": 17, "entity_type": "EMAIL_ADDRESS"}],
        }
    ])
    raw["schema_version"] = version
    out = validate_dataset(raw)
    assert out["schema_version"] == version
    assert out["cases"][0]["name"] == "Invoice email"


def test_case_without_name_still_loads():
    out = validate_dataset(_dataset([
        {"id": "c1", "text": "ab", "expected_spans": []},
    ]))
    assert "name" not in out["cases"][0]
