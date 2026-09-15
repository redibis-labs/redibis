"""Plan 1: value-tier scoring, match classes, schema 1.2, gates, eval-build."""

from __future__ import annotations

import json

import pytest

from redibis.pii.eval import DATASET_KIND, evaluate_case, evaluate_dataset, validate_dataset
from redibis.pii.eval.classify import MATCH_CLASSES, is_value_hit
from redibis.pii.eval.builder import CorpusBuildError, build_dataset, build_path
from redibis.pii.eval.corpus_patch import apply_corpus_patch, draft_rules_from_actions
from redibis.pii.eval.gates import evaluate_gates, format_gate_failure
from redibis.pii.eval.normalize import normalize_value
from redibis.pii.eval.pinning import resolve_text_rules
from redibis.pii.eval.provenance import rules_checksum
from redibis.pii.eval.registry import compare_runs, put_run
from redibis.pii.text_rules import default_text_rules


def _phone_case():
    text = "Call 01522345678."
    return {
        "id": "phone-dot",
        "text": text,
        "language": "en",
        "expected_spans": [
            {"id": "s1", "start": 5, "end": 16, "entity_type": "PHONE_NUMBER", "canonical": "01522345678"}
        ],
    }


def test_trailing_punct_is_superset_not_miss():
    case = _phone_case()
    scored = evaluate_case(
        case,
        [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER", "is_proposal": False}],
    )
    assert scored["exact"]["tp"] == 0
    assert scored["exact"]["fn"] == 1
    assert scored["value"]["tp"] == 1
    assert scored["value"]["fp"] == 0
    assert scored["value"]["fn"] == 0
    assert scored["value"]["f1"] >= scored["exact"]["f1"]
    classes = [row["class"] for row in scored["match_classes"]]
    assert "missed" not in classes
    assert "spurious" not in classes
    assert "superset" in classes
    row = next(r for r in scored["match_classes"] if r["class"] == "superset")
    assert row["coverage"] == 1.0
    assert row["extra_right"] == "."
    assert row["char_precision"] < 1.0
    assert row["value_equal"] is True
    assert row["is_value_hit"] is True
    assert "+" in (row.get("delta_label") or "")
    assert "." in (row.get("delta_label") or "")


def test_type_mismatch_is_not_miss_plus_fp_in_classes():
    case = {
        "id": "t",
        "text": "12345678901234",
        "expected_spans": [{"id": "s1", "start": 0, "end": 14, "entity_type": "VOUCHER"}],
    }
    scored = evaluate_case(
        case,
        [{"start": 0, "end": 14, "entity_type": "EG_NATIONAL_ID"}],
    )
    classes = [row["class"] for row in scored["match_classes"]]
    assert classes == ["type_mismatch"]
    assert scored["type"]["accuracy"] == 0.0
    row = scored["match_classes"][0]
    assert row["value_equal"] is True
    assert row["is_value_hit"] is False
    assert is_value_hit(row) is False
    assert scored["value"]["tp"] == 0


def test_guard_violation_and_advisory_never_gate():
    text = "عمارة 4 شقة 12"
    case = {
        "id": "g",
        "text": text,
        "expected_spans": [
            {
                "id": "s1",
                "start": 0,
                "end": len(text),
                "entity_type": "LOCATION",
                "grade": "advisory",
                "defect": "demo",
            }
        ],
        "forbidden_spans": [
            {"start": text.find("4"), "end": text.find("4") + 1, "entity_type": "PHONE_NUMBER", "reason": "building"}
        ],
    }
    scored = evaluate_case(
        case,
        [{"start": text.find("4"), "end": text.find("4") + 1, "entity_type": "PHONE_NUMBER"}],
    )
    assert scored["guard_violations"] == 1
    assert scored["exact"]["fn"] == 0  # advisory excluded from exact aggregate
    report = evaluate_dataset(
        {
            "kind": DATASET_KIND,
            "schema_version": "1.2",
            "cases": [case],
        },
        {case["id"]: [{"start": text.find("4"), "end": text.find("4") + 1, "entity_type": "PHONE_NUMBER"}]},
        allowed_entity_types={"LOCATION", "PHONE_NUMBER"},
    )
    verdict = evaluate_gates(report, {"guard_violations_max": 0, "micro": {"strict_f1": 0.0}})
    assert verdict["passed"] is False
    assert "guard" in verdict["summary"]


def test_schema_12_forbidden_and_tags():
    ds = validate_dataset(
        {
            "kind": DATASET_KIND,
            "schema_version": "1.2",
            "cases": [
                {
                    "id": "c1",
                    "text": "ab",
                    "tags": ["eg"],
                    "expected_spans": [{"start": 0, "end": 1, "entity_type": "PERSON", "grade": "strict"}],
                    "forbidden_spans": [{"start": 1, "end": 2, "entity_type": "OTP", "reason": "nope"}],
                }
            ],
        },
        allowed_entity_types={"PERSON", "OTP"},
    )
    assert ds["cases"][0]["tags"] == ["eg"]
    assert ds["cases"][0]["forbidden_spans"][0]["reason"] == "nope"


def test_eval_build_requires_verbatim_value():
    with pytest.raises(CorpusBuildError, match="EV1"):
        build_dataset(
            {
                "kind": "redibis.text_span_eval_corpus",
                "cases": [
                    {
                        "id": "EV1",
                        "text": "hello world",
                        "expected": [{"entity_type": "EMAIL_ADDRESS", "value": "missing@example.com"}],
                    }
                ],
            }
        )


def test_eval_build_writes_offsets(tmp_path):
    corpus = tmp_path / "c.yaml"
    corpus.write_text(
        "kind: redibis.text_span_eval_corpus\n"
        "cases:\n"
        "  - id: e1\n"
        "    text: 'Contact alice@example.com please'\n"
        "    expected:\n"
        "      - entity_type: EMAIL_ADDRESS\n"
        "        value: alice@example.com\n",
        encoding="utf-8",
    )
    out = tmp_path / "ds"
    written = build_path(corpus, out)
    data = json.loads(written[0].read_text(encoding="utf-8"))
    span = data["cases"][0]["expected_spans"][0]
    assert span["start"] == 8
    assert span["end"] == 25


def test_gate_names_entity_tier_metric():
    report = {
        "exact": {"micro": {"f1": 0.5, "precision": 0.5, "recall": 0.5, "tp": 1, "fp": 1, "fn": 1}, "by_entity": {
            "PHONE_NUMBER": {"f1": 0.2, "precision": 0.2, "recall": 0.2, "tp": 0, "fp": 1, "fn": 1}
        }},
        "strict": {"micro": {"f1": 0.5}, "by_entity": {"PHONE_NUMBER": {"f1": 0.2, "recall": 0.2, "precision": 0.2, "tp": 0, "fp": 1, "fn": 1}}},
        "value": {"micro": {"f1": 0.4, "recall": 0.4, "precision": 0.4, "tp": 0, "fp": 0, "fn": 1}, "by_entity": {
            "PHONE_NUMBER": {"value_recall": 0.1, "recall": 0.1, "precision": 1, "f1": 0.18, "tp": 0, "fp": 0, "fn": 1}
        }},
        "class_distribution": {"superset": 1, "exact": 0},
        "cases": [],
    }
    verdict = evaluate_gates(
        report,
        {"micro": {"value_f1": 0.9}, "by_entity": {"PHONE_NUMBER": {"value_recall": 0.95}}},
    )
    assert verdict["passed"] is False
    msg = format_gate_failure(verdict)
    assert "PHONE_NUMBER" in msg or "value" in msg


def test_rules_defaults_checksum_stable():
    a, src_a, merge_a = resolve_text_rules(rules_defaults=True)
    b, src_b, merge_b = resolve_text_rules(rules_defaults=True)
    assert src_a == "defaults"
    assert merge_a is False
    assert rules_checksum(a) == rules_checksum(b)
    assert rules_checksum(a) == rules_checksum(default_text_rules())


def test_digits_and_arabic_normalization():
    assert normalize_value("0100 2030 450", entity_type="PHONE_NUMBER") == "01002030450"
    eastern = "٠١٠٠٢٠٣٠٤٥٠"
    assert normalize_value(eastern, entity_type="PHONE_NUMBER") == "01002030450"


def test_corpus_patch_and_draft_rules():
    dataset = {
        "kind": DATASET_KIND,
        "cases": [{
            "id": "c1",
            "text": "Call 01522345678.",
            "expected_spans": [{"id": "s1", "start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}],
        }],
    }
    out = apply_corpus_patch(dataset, [{
        "action": "accept_as_expected",
        "case_id": "c1",
        "expected_id": "s1",
        "span": {"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"},
    }])
    assert out["dataset"]["cases"][0]["expected_spans"][0]["end"] == 17
    draft = draft_rules_from_actions([{"action": "exclude_term", "term": "agent"}])
    assert "agent" in draft["exclude_terms"]


def test_compare_runs_class_change():
    def report(cls, uid):
        return {
            "provenance": {"run_uuid": uid},
            "cases": [{
                "id": "c1",
                "match_classes": [{"expected_id": "s1", "class": cls}],
            }],
            "exact": {"micro": {"f1": 1}},
            "value": {"micro": {"f1": 1}},
        }
    put_run(report("exact", "run-a"))
    put_run(report("subset", "run-b"))
    diff = compare_runs("run-a", "run-b")
    assert diff["change_count"] == 1
    assert diff["changes"][0]["from"] == "exact"
    assert diff["changes"][0]["to"] == "subset"


def test_html_includes_tiers_and_classes():
    from redibis.pii.eval import REPORT_KIND, render_report_html

    scored = evaluate_case(
        _phone_case(),
        [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}],
    )
    html = render_report_html({
        "kind": REPORT_KIND,
        "schema_version": "1.2",
        "redibis_version": "0.0-test",
        "dataset_id": "t",
        "normalization_profile": "v1",
        "provenance": {"run_uuid": "u", "rules_checksum": "sha256:abc", "normalization_profile": "v1"},
        "exact": {"micro": scored["exact"]},
        "strict": {"micro": scored["strict"]},
        "value": {"micro": scored["value"]},
        "overlap": {"micro": scored["overlap"]},
        "type": {"micro": scored["type"]},
        "class_distribution": scored["class_distribution"],
        "cases": [scored],
    })
    assert "value F1" in html.lower() or "Value" in html
    assert "superset" in html
    assert "hl extra" in html or 'class="hl extra"' in html
    assert "<script>" not in html
    assert "sha256:abc" in html


def _assert_matcher_invariants(scored):
    value = scored["value"]
    matched = {
        (m["expected_index"], m["predicted_index"]) for m in value["matches"]
    }
    missed = set(value["missed_expected_indexes"])
    for row in scored["match_classes"]:
        grade = str(row.get("grade") or "strict")
        types_match = (
            bool(row.get("expected_type"))
            and row.get("expected_type") == row.get("got_type")
        )
        assert row["is_value_hit"] is (bool(row.get("value_equal")) and types_match)
        assert is_value_hit(row) is row["is_value_hit"]
        if row["class"] == "type_mismatch":
            assert row["is_value_hit"] is False
            if grade != "advisory" and row.get("expected_index") is not None:
                assert (row["expected_index"], row["predicted_index"]) not in matched
        if row["class"] == "exact" and grade != "advisory":
            assert (row["expected_index"], row["predicted_index"]) in matched
            assert row["value_equal"] is True
            assert row["is_value_hit"] is True
        if row["class"] == "missed" and grade != "advisory":
            assert row["expected_index"] in missed
            assert row["is_value_hit"] is False
        if (
            row["is_value_hit"]
            and grade != "advisory"
            and row.get("expected_index") is not None
            and row.get("predicted_index") is not None
        ):
            assert (row["expected_index"], row["predicted_index"]) in matched
    missed_rows = [
        row for row in scored["match_classes"]
        if row["class"] == "missed" and str(row.get("grade") or "strict") != "advisory"
    ]
    # Class `missed` ⊆ value.fn. Value FN can still exceed that for
    # value_equal=false supersets until both matchers share one pairing.
    assert len(missed_rows) <= value["fn"]
    guards = sum(1 for row in scored["match_classes"] if row["class"] == "guard_violation")
    assert guards == scored["guard_violations"]


def test_spoken_gold_exact_offset_is_value_tp():
    text = "رقم صفر واحد خمسة اثنين"
    start = text.find("صفر")
    end = len(text)
    case = {
        "id": "spoken",
        "text": text,
        "expected_spans": [{
            "id": "s1",
            "start": start,
            "end": end,
            "entity_type": "PHONE_NUMBER",
            "canonical": "0152",
            "spoken": True,
        }],
    }
    pred = [{"start": start, "end": end, "entity_type": "PHONE_NUMBER"}]
    scored = evaluate_case(case, pred)
    assert scored["exact"]["tp"] == 1
    assert scored["exact"]["fp"] == 0
    assert scored["value"]["tp"] == 1
    assert scored["value"]["fp"] == 0
    assert scored["value"]["fn"] == 0
    assert scored["value"]["f1"] == 1.0
    row = next(r for r in scored["match_classes"] if r["class"] == "exact")
    assert row["canonical_match"] == "unknown"
    assert row["value_equal"] is True
    _assert_matcher_invariants(scored)


def test_spoken_gold_with_pred_canonical_compares_digits():
    text = "رقم صفر واحد خمسة اثنين"
    start = text.find("صفر")
    end = len(text)
    case = {
        "id": "spoken-canon",
        "text": text,
        "expected_spans": [{
            "id": "s1",
            "start": start,
            "end": end,
            "entity_type": "PHONE_NUMBER",
            "canonical": "0152",
            "spoken": True,
        }],
    }
    pred = [{
        "start": start,
        "end": end,
        "entity_type": "PHONE_NUMBER",
        "canonical": "0152",
        "engine": "preprocess",
        "recognizer": "arabic_spoken_digits|0152",
    }]
    scored = evaluate_case(case, pred)
    assert scored["value"]["tp"] == 1
    row = next(r for r in scored["match_classes"] if r["class"] == "exact")
    assert row["canonical_match"] == "yes"
    assert row["value_equal"] is True


def test_superset_value_equal_distinguishes_trailing_punct_from_extra_digits():
    punct = evaluate_case(
        _phone_case(),
        [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}],
    )
    punct_row = next(r for r in punct["match_classes"] if r["class"] == "superset")
    assert punct_row["value_equal"] is True

    text = "Call 01522345678 and 99"
    extra = evaluate_case(
        {
            "id": "extra",
            "text": text,
            "expected_spans": [
                {"id": "s1", "start": 5, "end": 16, "entity_type": "PHONE_NUMBER", "canonical": "01522345678"}
            ],
        },
        [{"start": 5, "end": len(text), "entity_type": "PHONE_NUMBER"}],
    )
    extra_row = next(r for r in extra["match_classes"] if r["class"] == "superset")
    assert extra_row["value_equal"] is False
    assert extra["value"]["tp"] == 0


def test_tier_flag_omits_unrequested_blocks():
    scored = evaluate_case(
        _phone_case(),
        [{"start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}],
        tiers=["strict"],
    )
    assert "exact" in scored
    assert "strict" in scored
    assert "value" not in scored
    assert "overlap" not in scored
    assert "type" not in scored
    with pytest.raises(ValueError, match="unknown"):
        evaluate_case(
            _phone_case(),
            [{"start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}],
            tiers=["nope"],
        )


def test_advisory_overlap_is_not_a_false_positive():
    text = "id 1234567890123 x"
    start = text.find("1")
    end = start + 13
    case = {
        "id": "adv",
        "text": text,
        "expected_spans": [{
            "id": "s1",
            "start": start,
            "end": end,
            "entity_type": "EG_NATIONAL_ID",
            "grade": "advisory",
            "defect": "13-digit",
        }],
    }
    pred = [{"start": start, "end": start + 8, "entity_type": "PHONE_NUMBER"}]
    scored = evaluate_case(case, pred)
    assert scored["value"]["fp"] == 0
    assert scored["value"]["fn"] == 0
    assert scored["exact"]["fp"] == 0
    assert 0 in scored["value"]["advisory_predictions"]


def test_type_tier_emits_accuracy_only():
    scored = evaluate_case(
        {
            "id": "t",
            "text": "12345678901234",
            "expected_spans": [{"id": "s1", "start": 0, "end": 14, "entity_type": "VOUCHER"}],
        },
        [{"start": 0, "end": 14, "entity_type": "EG_NATIONAL_ID"}],
    )
    assert "f1" not in scored["type"]
    assert "recall" not in scored["type"]
    assert scored["type"]["accuracy"] == 0.0
    report = evaluate_dataset(
        {
            "kind": DATASET_KIND,
            "schema_version": "1.2",
            "cases": [{
                "id": "t",
                "text": "12345678901234",
                "expected_spans": [{"start": 0, "end": 14, "entity_type": "VOUCHER"}],
            }],
        },
        {"t": [{"start": 0, "end": 14, "entity_type": "EG_NATIONAL_ID"}]},
        allowed_entity_types={"VOUCHER", "EG_NATIONAL_ID"},
    )
    assert "f1" not in report["type"]["micro"]
    assert "recall" not in report["type"]["micro"]
    assert report["type"]["micro"]["accuracy"] == 0.0


def test_gate_unknown_key_and_absent_entity_fail_closed():
    from redibis.pii.eval.gates import GateError, evaluate_gates

    report = {
        "value": {"micro": {"f1": 1.0}, "by_entity": {"PHONE_NUMBER": {"f1": 1.0, "recall": 1.0, "precision": 1.0}}},
        "strict": {"micro": {"f1": 1.0}, "by_entity": {"PHONE_NUMBER": {"f1": 1.0}}},
        "class_distribution": {"exact": 1},
        "cases": [{"expected_spans": [{"entity_type": "PHONE_NUMBER"}]}],
    }
    with pytest.raises(GateError, match="value_F1"):
        evaluate_gates(report, {"micro": {"value_F1": 0.9}})
    with pytest.raises(GateError, match="PASSPORT"):
        evaluate_gates(report, {"by_entity": {"PASSPORT": {"value_f1": 0.9}}})
    with pytest.raises(GateError, match="_max"):
        evaluate_gates(report, {"class_budgets": {"superset": 0.1}})


def test_by_tag_is_per_span_not_whole_case():
    report = evaluate_dataset(
        {
            "kind": DATASET_KIND,
            "schema_version": "1.2",
            "cases": [{
                "id": "mixed",
                "text": "Cairo 01522345678",
                "tags": ["address", "spoken_number"],
                "expected_spans": [
                    {"id": "a", "start": 0, "end": 5, "entity_type": "LOCATION", "tags": ["address"]},
                    {"id": "p", "start": 6, "end": 17, "entity_type": "PHONE_NUMBER", "tags": ["spoken_number"]},
                ],
            }],
        },
        {"mixed": [{"start": 6, "end": 17, "entity_type": "PHONE_NUMBER"}]},
        allowed_entity_types={"LOCATION", "PHONE_NUMBER"},
    )
    assert report["by_tag"]["spoken_number"]["tp"] == 1
    assert report["by_tag"]["spoken_number"]["fn"] == 0
    assert report["by_tag"]["address"]["fn"] == 1
    assert report["by_tag"]["address"]["tp"] == 0


def test_arabic_steps_are_independent_and_question_mark_strips():
    from redibis.pii.eval.normalize import _apply_step, normalize_value

    assert _apply_step("أ", "unify_alef") == "ا"
    assert "َ" in _apply_step("بَ", "unify_alef")
    assert _apply_step("بَ", "strip_diacritics") == "ب"
    assert normalize_value("hello؟", entity_type="") == "hello"


def test_corrupt_profile_file_is_loud(tmp_path, monkeypatch):
    from redibis.pii.eval import normalize as n

    bad = tmp_path / "v1.yaml"
    bad.write_text("{ this is: [not yaml", encoding="utf-8")
    monkeypatch.setattr(n, "profile_search_paths", lambda _pid: [bad])
    n.load_profile.cache_clear()
    try:
        with pytest.raises(n.NormalizationError, match="invalid"):
            n.load_profile("v1")
    finally:
        n.load_profile.cache_clear()


def test_rules_checksum_none_raises():
    from redibis.pii.eval.provenance import rules_checksum

    with pytest.raises(ValueError, match="overlay is missing"):
        rules_checksum(None)


def test_comparable_report_strips_nested_run_uuid():
    from redibis.pii.eval.provenance import comparable_report

    cmp = comparable_report({
        "provenance": {"run_uuid": "a", "label": "x", "rules_checksum": "sha256:1"},
        "files": [{
            "provenance": {"run_uuid": "b", "label": "y", "rules_checksum": "sha256:1"},
        }],
    })
    assert "run_uuid" not in cmp["provenance"]
    assert "label" not in cmp["provenance"]
    assert "run_uuid" not in cmp["files"][0]["provenance"]
    assert cmp["provenance"]["rules_checksum"] == "sha256:1"


def test_pack_id_version_is_not_a_silent_path_error():
    from redibis.pii.eval.pinning import EvalPinError, _resolve_pack_ref

    with pytest.raises(EvalPinError, match="id@version"):
        _resolve_pack_ref("eg-telecom@1.4.0")


def test_matcher_invariants_on_trailing_punct():
    scored = evaluate_case(
        _phone_case(),
        [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}],
    )
    _assert_matcher_invariants(scored)

    mismatch = evaluate_case(
        {
            "id": "t",
            "text": "12345678901234",
            "expected_spans": [{"id": "s1", "start": 0, "end": 14, "entity_type": "VOUCHER"}],
        },
        [{"start": 0, "end": 14, "entity_type": "EG_NATIONAL_ID"}],
    )
    row = next(r for r in mismatch["match_classes"] if r["class"] == "type_mismatch")
    assert row["value_equal"] is True
    assert row["is_value_hit"] is False
    assert mismatch["value"]["tp"] == 0
    assert mismatch["type"]["accuracy"] == 0.0
    _assert_matcher_invariants(mismatch)


def _class_taxonomy_scored() -> dict[str, object]:
    """One evaluate_case result that should exhibit each MATCH_CLASSES row."""
    phone = "Call 01522345678."
    building = "عمارة 4 شقة 12"
    digit = building.find("4")
    return {
        "exact": evaluate_case(
            _phone_case(),
            [{"start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}],
        ),
        "superset": evaluate_case(
            _phone_case(),
            [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}],
        ),
        "subset": evaluate_case(
            _phone_case(),
            [{"start": 5, "end": 15, "entity_type": "PHONE_NUMBER"}],
        ),
        "overlap_partial": evaluate_case(
            {
                "id": "ov",
                "text": "Call 01522345678 extra",
                "expected_spans": [{
                    "id": "s1",
                    "start": 5,
                    "end": 16,
                    "entity_type": "PHONE_NUMBER",
                    "canonical": "01522345678",
                }],
            },
            [{"start": 10, "end": 22, "entity_type": "PHONE_NUMBER"}],
        ),
        "type_mismatch": evaluate_case(
            {
                "id": "t",
                "text": "12345678901234",
                "expected_spans": [{"id": "s1", "start": 0, "end": 14, "entity_type": "VOUCHER"}],
            },
            [{"start": 0, "end": 14, "entity_type": "EG_NATIONAL_ID"}],
        ),
        "missed": evaluate_case(_phone_case(), []),
        "spurious": evaluate_case(
            {"id": "sp", "text": phone, "expected_spans": []},
            [{"start": 5, "end": 16, "entity_type": "PHONE_NUMBER"}],
        ),
        "split": evaluate_case(
            _phone_case(),
            [
                {"start": 5, "end": 10, "entity_type": "PHONE_NUMBER"},
                {"start": 10, "end": 16, "entity_type": "PHONE_NUMBER"},
            ],
        ),
        "merged": evaluate_case(
            {
                "id": "m",
                "text": "01522345678 01522345679",
                "expected_spans": [
                    {"id": "a", "start": 0, "end": 11, "entity_type": "PHONE_NUMBER"},
                    {"id": "b", "start": 12, "end": 23, "entity_type": "PHONE_NUMBER"},
                ],
            },
            [{"start": 0, "end": 23, "entity_type": "PHONE_NUMBER"}],
        ),
        "equivalent": evaluate_case(
            {
                "id": "eq",
                "text": "Call 01522345678 see 015-223-45678",
                "expected_spans": [{
                    "id": "s1",
                    "start": 5,
                    "end": 16,
                    "entity_type": "PHONE_NUMBER",
                    "canonical": "01522345678",
                }],
            },
            [{"start": 21, "end": 34, "entity_type": "PHONE_NUMBER"}],
        ),
        "guard_violation": evaluate_case(
            {
                "id": "g",
                "text": building,
                "expected_spans": [{
                    "id": "s1",
                    "start": 0,
                    "end": len(building),
                    "entity_type": "LOCATION",
                    "grade": "advisory",
                }],
                "forbidden_spans": [{
                    "start": digit,
                    "end": digit + 1,
                    "entity_type": "PHONE_NUMBER",
                    "reason": "building",
                }],
            },
            [{"start": digit, "end": digit + 1, "entity_type": "PHONE_NUMBER"}],
        ),
    }


def test_matcher_invariants_cover_every_class():
    scored_by_class = _class_taxonomy_scored()
    seen: set[str] = set()
    for name, scored in scored_by_class.items():
        classes = [row["class"] for row in scored["match_classes"]]
        assert name in classes, f"fixture {name!r} produced {classes}"
        _assert_matcher_invariants(scored)
        seen.update(classes)
    missing = set(MATCH_CLASSES) - seen
    assert not missing, f"taxonomy fixtures missed classes: {sorted(missing)}"


def test_report_html_tints_overcaptured_chars():
    from redibis.pii.eval.report_html import render_report_body
    from redibis.pii.eval.span_metrics import REPORT_KIND, SCHEMA_VERSION_1_2

    case = _phone_case()
    pred = [{"start": 5, "end": 17, "entity_type": "PHONE_NUMBER"}]
    scored = evaluate_case(case, pred)
    row = next(r for r in scored["match_classes"] if r["class"] == "superset")
    assert row["coverage"] == 1.0
    assert row["char_precision"] < 1.0
    assert row["value_equal"] is True
    report = evaluate_dataset(
        {
            "kind": DATASET_KIND,
            "schema_version": SCHEMA_VERSION_1_2,
            "id": "overcap",
            "cases": [case],
        },
        {"phone-dot": pred},
    )
    assert report["kind"] == REPORT_KIND
    html = render_report_body(report)
    assert 'class="hl extra"' in html
    assert "dir=\"auto\"" in html

