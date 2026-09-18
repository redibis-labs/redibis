"""Validation and deterministic scoring for portable text-span datasets."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence

from redibis.pii.eval.classify import MATCH_CLASSES, class_distribution, classify_case
from redibis.pii.eval.normalize import (
    PROFILE_V1_ID,
    compare_span_values,
    load_profile,
    profile_id_of,
    span_canonical,
)

DATASET_KIND = "redibis.text_span_eval_dataset"
REPORT_KIND = "redibis.text_span_eval_report"
BATCH_REPORT_KIND = "redibis.text_span_eval_batch_report"
SCHEMA_VERSION = "1.0"
SCHEMA_VERSION_1_2 = "1.2"
SUPPORTED_DATASET_VERSIONS = frozenset({"1.0", "1.2"})
SUPPORTED_REPORT_VERSIONS = frozenset({"1.0", "1.2"})
OFFSET_UNIT = "unicode_codepoint"
GRADES = frozenset({"strict", "advisory", "structure_only"})
DEFAULT_TIERS = ("strict", "value", "overlap", "type")
VALID_TIERS = frozenset({"strict", "exact", "value", "overlap", "type"})


class DatasetValidationError(ValueError):
    """Raised when an evaluation dataset cannot be scored safely."""


def current_redibis_version() -> str:
    from redibis import __version__

    return str(__version__)


def _span_dict(span: Any) -> dict[str, Any]:
    if isinstance(span, Mapping):
        return dict(span)
    return {
        "start": getattr(span, "start", None),
        "end": getattr(span, "end", None),
        "entity_type": getattr(span, "entity_type", ""),
        "is_proposal": bool(getattr(span, "is_proposal", False)),
        "score": getattr(span, "score", None),
        "engine": getattr(span, "engine", None),
        "recognizer": getattr(span, "recognizer", None),
        "validator": getattr(span, "validator", None),
        "id": getattr(span, "id", None),
        "grade": getattr(span, "grade", None),
        "canonical": getattr(span, "canonical", None),
        "spoken": getattr(span, "spoken", None),
        "note": getattr(span, "note", None),
        "defect": getattr(span, "defect", None),
        "explain": getattr(span, "explain", None),
        "tags": getattr(span, "tags", None),
    }


def canonical_span(
    span: Any,
    *,
    text_len: int,
    location: str,
    default_id: str = "",
    require_id: bool = False,
) -> dict[str, Any]:
    """Normalise a span and drop matched substrings."""
    value = _span_dict(span)
    try:
        start = int(value.get("start"))
        end = int(value.get("end"))
    except (TypeError, ValueError) as exc:
        raise DatasetValidationError(f"{location}: start and end must be integers") from exc
    entity_type = str(value.get("entity_type") or "").strip().upper()
    if not entity_type:
        raise DatasetValidationError(f"{location}: entity_type is required")
    if start < 0 or end <= start or end > text_len:
        raise DatasetValidationError(
            f"{location}: invalid range [{start}, {end}) for {text_len} codepoints"
        )
    out: dict[str, Any] = {
        "start": start,
        "end": end,
        "entity_type": entity_type,
        "is_proposal": bool(value.get("is_proposal", False)),
    }
    span_id = str(value.get("id") or default_id or "").strip()
    if require_id and not span_id:
        raise DatasetValidationError(f"{location}: id is required")
    if span_id:
        out["id"] = span_id
    grade = str(value.get("grade") or "strict").strip().lower() or "strict"
    if grade not in GRADES:
        raise DatasetValidationError(f"{location}: grade must be one of {sorted(GRADES)}")
    if grade != "strict" or value.get("grade"):
        out["grade"] = grade
    else:
        out["grade"] = "strict"
    for key in ("score", "engine", "recognizer", "validator", "canonical", "note", "defect",
                "agreement", "arbitration_rule", "llm_verdict", "llm_score", "llm_reason"):
        if value.get(key) not in (None, ""):
            out[key] = value[key]
    if not out.get("canonical"):
        inferred = span_canonical(value)
        if inferred:
            out["canonical"] = inferred
    if value.get("spoken"):
        out["spoken"] = True
    tags = [str(t).strip() for t in (value.get("tags") or []) if str(t).strip()]
    if tags:
        out["tags"] = tags
    explain = value.get("explain")
    if explain:
        out["explain"] = explain
    return out


def resolve_tiers(tiers: Sequence[str] | str | None) -> tuple[str, ...]:
    """Return requested scoring tiers. Unknown names raise before any scoring."""
    if tiers is None:
        return DEFAULT_TIERS
    if isinstance(tiers, str):
        parts = tuple(part.strip() for part in tiers.split(",") if part.strip())
    else:
        parts = tuple(str(part).strip() for part in tiers if str(part).strip())
    if not parts:
        return DEFAULT_TIERS
    unknown = sorted({part for part in parts if part not in VALID_TIERS})
    if unknown:
        raise ValueError(f"unknown tier(s): {unknown}")
    return parts


def _sort_pairs(candidates: list[tuple[float, int, int]]) -> None:
    """Highest score first; ties resolve to the earliest gold then earliest prediction."""
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))


def _span_key(span: Mapping[str, Any]) -> tuple[int, int, str]:
    return (int(span["start"]), int(span["end"]), str(span["entity_type"]))


def classify_eval_payload(raw: Any) -> tuple[str, str]:
    """Return (status, reason) before full validation.

    ``failed`` means the file is not a usable evaluation dataset (wrong kind,
    missing version, or unreadable shape). Callers continue to the next file.
    """
    if not isinstance(raw, Mapping):
        return "failed", "not a JSON object"
    kind = raw.get("kind")
    if kind != DATASET_KIND:
        return "failed", f"not an evaluation dataset (kind={kind!r})"
    version = str(raw.get("schema_version") or "")
    if version not in SUPPORTED_DATASET_VERSIONS:
        return "failed", f"unsupported schema_version {raw.get('schema_version')!r}"
    return "ok", ""


def validate_dataset(
    dataset: Mapping[str, Any],
    *,
    allowed_entity_types: Iterable[str] | None = None,
    default_language: str = "en",
    require_redibis_version: bool = False,
) -> dict[str, Any]:
    """Return a normalized copy of a portable evaluation dataset."""
    if not isinstance(dataset, Mapping):
        raise DatasetValidationError("dataset must be a JSON object")
    if dataset.get("kind") != DATASET_KIND:
        raise DatasetValidationError(f"kind must be {DATASET_KIND!r}")
    version = str(dataset.get("schema_version") or "")
    if version not in SUPPORTED_DATASET_VERSIONS:
        raise DatasetValidationError(
            f"unsupported schema_version {dataset.get('schema_version')!r}"
        )
    if require_redibis_version and not str(dataset.get("redibis_version") or "").strip():
        raise DatasetValidationError("redibis_version is required")
    if dataset.get("offset_unit", OFFSET_UNIT) != OFFSET_UNIT:
        raise DatasetValidationError(f"offset_unit must be {OFFSET_UNIT!r}")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        raise DatasetValidationError("cases must be a non-empty array")

    allowed = {str(v).strip().upper() for v in (allowed_entity_types or ()) if str(v).strip()}
    fallback_lang = str(default_language or "en").strip() or "en"
    seen: set[str] = set()
    normalized_cases: list[dict[str, Any]] = []
    for index, raw_case in enumerate(cases):
        if not isinstance(raw_case, Mapping):
            raise DatasetValidationError(f"cases[{index}] must be an object")
        case_id = str(raw_case.get("id") or "").strip()
        if not case_id:
            raise DatasetValidationError(f"cases[{index}].id is required")
        if case_id in seen:
            raise DatasetValidationError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        text = raw_case.get("text")
        if not isinstance(text, str):
            raise DatasetValidationError(f"case {case_id!r}: text must be a string")
        text_len = len(text)
        raw_spans = raw_case.get("expected_spans", raw_case.get("gold_spans", []))
        if not isinstance(raw_spans, list):
            raise DatasetValidationError(f"case {case_id!r}: expected_spans must be an array")
        spans = [
            canonical_span(
                span,
                text_len=text_len,
                location=f"case {case_id!r} span[{i}]",
                default_id=f"s{i + 1}",
            )
            for i, span in enumerate(raw_spans)
        ]
        keys = [_span_key(span) for span in spans]
        if len(keys) != len(set(keys)):
            raise DatasetValidationError(f"case {case_id!r}: duplicate expected spans")
        span_ids = [s.get("id") for s in spans if s.get("id")]
        if len(span_ids) != len(set(span_ids)):
            raise DatasetValidationError(f"case {case_id!r}: duplicate expected span ids")
        if allowed:
            unknown = sorted({span["entity_type"] for span in spans} - allowed)
            if unknown:
                raise DatasetValidationError(
                    f"case {case_id!r}: unknown entity_type(s): {', '.join(unknown)}"
                )
        raw_forbidden = raw_case.get("forbidden_spans") or []
        if raw_forbidden and not isinstance(raw_forbidden, list):
            raise DatasetValidationError(f"case {case_id!r}: forbidden_spans must be an array")
        forbidden: list[dict[str, Any]] = []
        for i, span in enumerate(raw_forbidden or []):
            item = canonical_span(
                span,
                text_len=text_len,
                location=f"case {case_id!r} forbidden[{i}]",
                default_id=f"f{i + 1}",
            )
            reason = ""
            if isinstance(span, Mapping):
                reason = str(span.get("reason") or "")
            item["reason"] = reason
            forbidden.append(item)
        tags = [str(t).strip() for t in (raw_case.get("tags") or []) if str(t).strip()]
        language = str(raw_case.get("language") or "").strip() or fallback_lang
        case_out = {
            "id": case_id,
            "text": text,
            "language": language,
            "tags": tags,
            "expected_spans": spans,
            "forbidden_spans": forbidden,
        }
        name = str(raw_case.get("name") or "").strip()
        if name:
            case_out["name"] = name
        normalized_cases.append(case_out)

    return {
        "kind": DATASET_KIND,
        "schema_version": version,
        "redibis_version": str(dataset.get("redibis_version") or current_redibis_version()),
        "offset_unit": OFFSET_UNIT,
        "id": str(dataset.get("id") or ""),
        "normalization_profile": str(dataset.get("normalization_profile") or PROFILE_V1_ID),
        "cases": normalized_cases,
    }


def _iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    overlap = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
    if not overlap:
        return 0.0
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    return overlap / union


def _pair(
    candidates: list[tuple[float, int, int]],
    n_expected: int,
    n_predicted: int,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    _sort_pairs(candidates)
    expected_used: set[int] = set()
    predicted_used: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _score, expected_index, predicted_index in candidates:
        if expected_index in expected_used or predicted_index in predicted_used:
            continue
        expected_used.add(expected_index)
        predicted_used.add(predicted_index)
        matches.append((expected_index, predicted_index))
    return (
        matches,
        [i for i in range(n_expected) if i not in expected_used],
        [i for i in range(n_predicted) if i not in predicted_used],
    )


def _match(
    expected: Sequence[dict[str, Any]],
    predicted: Sequence[dict[str, Any]],
    *,
    exact: bool,
    iou_threshold: float,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    candidates: list[tuple[float, int, int]] = []
    for expected_index, gold in enumerate(expected):
        for predicted_index, found in enumerate(predicted):
            if gold["entity_type"] != found["entity_type"]:
                continue
            score = (
                1.0
                if gold["start"] == found["start"] and gold["end"] == found["end"]
                else 0.0
            )
            if not exact:
                score = _iou(gold, found)
            if score >= (1.0 if exact else iou_threshold):
                candidates.append((score, expected_index, predicted_index))
    return _pair(candidates, len(expected), len(predicted))


def _match_value(
    text: str,
    expected: Sequence[dict[str, Any]],
    predicted: Sequence[dict[str, Any]],
    *,
    profile: Mapping[str, Any],
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    candidates: list[tuple[float, int, int]] = []
    for expected_index, gold in enumerate(expected):
        for predicted_index, found in enumerate(predicted):
            if gold["entity_type"] != found["entity_type"]:
                continue
            equal, _status = compare_span_values(text, gold, found, profile=profile)
            if not equal:
                continue
            exact = gold["start"] == found["start"] and gold["end"] == found["end"]
            score = 1.0 if exact else (0.5 + 0.5 * _iou(gold, found))
            candidates.append((score, expected_index, predicted_index))
    return _pair(candidates, len(expected), len(predicted))


def _match_type(
    text: str,
    expected: Sequence[dict[str, Any]],
    predicted: Sequence[dict[str, Any]],
    *,
    profile: Mapping[str, Any],
    iou_threshold: float,
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Pair by normalized value or overlap, ignoring entity_type."""
    candidates: list[tuple[float, int, int]] = []
    for expected_index, gold in enumerate(expected):
        for predicted_index, found in enumerate(predicted):
            value_hit, _status = compare_span_values(text, gold, found, profile=profile)
            overlap_hit = _iou(gold, found) >= iou_threshold
            if not value_hit and not overlap_hit:
                continue
            exact = gold["start"] == found["start"] and gold["end"] == found["end"]
            score = (
                (2.0 if value_hit else 0.0)
                + (1.0 if exact else 0.0)
                + _iou(gold, found)
            )
            candidates.append((score, expected_index, predicted_index))
    return _pair(candidates, len(expected), len(predicted))


def _counts_from_match(
    matches: Sequence[tuple[int, int]],
    missed: Sequence[int],
    extra: Sequence[int],
) -> dict[str, Any]:
    return {
        "tp": len(matches),
        "fp": len(extra),
        "fn": len(missed),
        "matches": [
            {"expected_index": expected_index, "predicted_index": predicted_index}
            for expected_index, predicted_index in sorted(matches)
        ],
        "missed_expected_indexes": list(missed),
        "extra_predicted_indexes": list(extra),
    }


def _counts(
    expected: Sequence[dict[str, Any]],
    predicted: Sequence[dict[str, Any]],
    *,
    exact: bool,
    iou_threshold: float,
) -> dict[str, Any]:
    matches, missed, extra = _match(
        expected, predicted, exact=exact, iou_threshold=iou_threshold
    )
    return _counts_from_match(matches, missed, extra)


def rates(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else (1.0 if not fn else 0.0)
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def _overlaps_advisory(
    pred: Mapping[str, Any],
    expected: Sequence[Mapping[str, Any]],
) -> bool:
    ps, pe = int(pred["start"]), int(pred["end"])
    for gold in expected:
        if str(gold.get("grade") or "strict") != "advisory":
            continue
        if max(0, min(int(gold["end"]), pe) - max(int(gold["start"]), ps)) > 0:
            return True
    return False


def _remap_counts(
    counts: Mapping[str, Any],
    *,
    expected: Sequence[Mapping[str, Any]],
    predicted: Sequence[Mapping[str, Any]],
    metric: str,
) -> dict[str, Any]:
    """Drop advisory (and structure_only on strict) from tp/fn.

    Predictions that overlap an advisory gold are removed from FP and listed
    under ``advisory_predictions`` — the corpus said not to judge that region.
    """
    skip_fn: set[int] = set()
    for index, span in enumerate(expected):
        grade = str(span.get("grade") or "strict")
        if grade == "advisory":
            skip_fn.add(index)
        elif grade == "structure_only" and metric in {"exact", "strict"}:
            skip_fn.add(index)
    kept_matches = []
    for match in counts["matches"]:
        ei = int(match["expected_index"])
        if ei in skip_fn:
            continue
        kept_matches.append(match)
    missed = [i for i in counts["missed_expected_indexes"] if i not in skip_fn]
    extra: list[int] = []
    advisory_predictions: list[int] = []
    for pi in counts["extra_predicted_indexes"]:
        if 0 <= int(pi) < len(predicted) and _overlaps_advisory(predicted[int(pi)], expected):
            advisory_predictions.append(int(pi))
        else:
            extra.append(int(pi))
    tp = len(kept_matches)
    out = {
        **rates(tp, len(extra), len(missed)),
        "matches": kept_matches,
        "missed_expected_indexes": missed,
        "extra_predicted_indexes": extra,
        "advisory_predictions": advisory_predictions,
    }
    return out


def _type_accuracy_block(
    type_tp: int,
    type_wrong: int,
    matches: Sequence[Mapping[str, Any]],
    missed: Sequence[int],
    extra: Sequence[int],
    advisory_predictions: Sequence[int] | None = None,
) -> dict[str, Any]:
    paired = type_tp + type_wrong
    accuracy = (type_tp / paired) if paired else 1.0
    return {
        "accuracy": round(accuracy, 6),
        "paired": paired,
        "type_match": type_tp,
        "type_mismatch": type_wrong,
        "matches": list(matches),
        "missed_expected_indexes": list(missed),
        "extra_predicted_indexes": list(extra),
        "advisory_predictions": list(advisory_predictions or ()),
    }


def evaluate_case(
    case: Mapping[str, Any],
    predicted_spans: Sequence[Any],
    *,
    overlap_iou: float = 0.5,
    profile: Mapping[str, Any] | None = None,
    tiers: Sequence[str] | str | None = None,
) -> dict[str, Any]:
    """Score one normalized case; LLM-only proposals are reported separately.

    Guard-violating predictions are counted both as ``guard_violation`` and as
    a false positive on every scored tier (double penalty).
    """
    if not 0 < overlap_iou <= 1:
        raise ValueError("overlap_iou must be in (0, 1]")
    wanted = resolve_tiers(tiers)
    need_strict = any(t in wanted for t in ("strict", "exact"))
    need_value = "value" in wanted
    need_overlap = "overlap" in wanted
    need_type = "type" in wanted
    text = str(case.get("text") or "")
    expected = [
        canonical_span(span, text_len=len(text), location="expected span", default_id=f"s{i + 1}")
        for i, span in enumerate(case.get("expected_spans", ()))
    ]
    forbidden = [
        canonical_span(span, text_len=len(text), location="forbidden span", default_id=f"f{i + 1}")
        | {"reason": str(span.get("reason") or "") if isinstance(span, Mapping) else ""}
        for i, span in enumerate(case.get("forbidden_spans", ()))
    ]
    all_predicted = [
        canonical_span(span, text_len=len(text), location="predicted span", default_id=f"p{i + 1}")
        for i, span in enumerate(predicted_spans)
    ]
    proposals = [span for span in all_predicted if span["is_proposal"]]
    predicted = [span for span in all_predicted if not span["is_proposal"]]
    spec = profile or load_profile(str(case.get("normalization_profile") or PROFILE_V1_ID))
    classes = classify_case(
        text,
        expected,
        predicted,
        forbidden=forbidden,
        profile=spec,
        iou_threshold=overlap_iou,
    )
    out: dict[str, Any] = {
        "id": str(case.get("id") or ""),
        "text": text,
        "language": str(case.get("language") or "en"),
        "tags": list(case.get("tags") or []),
        "expected_spans": expected,
        "forbidden_spans": forbidden,
        "predicted_spans": predicted,
        "proposal_spans": proposals,
        "match_classes": classes,
        "class_distribution": class_distribution(classes),
        "normalization_profile": profile_id_of(spec),
        "guard_violations": sum(1 for row in classes if row.get("class") == "guard_violation"),
        "scored_tiers": [t if t != "exact" else "strict" for t in wanted],
    }
    if need_strict:
        exact = _counts(expected, predicted, exact=True, iou_threshold=1.0)
        exact_out = _remap_counts(exact, expected=expected, predicted=predicted, metric="exact")
        out["exact"] = exact_out
        out["strict"] = exact_out
    if need_overlap:
        overlap = _counts(expected, predicted, exact=False, iou_threshold=overlap_iou)
        out["overlap"] = _remap_counts(
            overlap, expected=expected, predicted=predicted, metric="overlap"
        )
    if need_value:
        value_m, value_miss, value_extra = _match_value(text, expected, predicted, profile=spec)
        value = _counts_from_match(value_m, value_miss, value_extra)
        out["value"] = _remap_counts(value, expected=expected, predicted=predicted, metric="value")
    if need_type:
        type_m, type_miss, type_extra = _match_type(
            text, expected, predicted, profile=spec, iou_threshold=overlap_iou
        )
        type_tp = 0
        type_wrong = 0
        type_matches_payload = []
        advisory_extra: list[int] = []
        kept_extra: list[int] = []
        for ei, pi in type_m:
            if str(expected[ei].get("grade") or "strict") == "advisory":
                continue
            same = expected[ei]["entity_type"] == predicted[pi]["entity_type"]
            if same:
                type_tp += 1
            else:
                type_wrong += 1
            type_matches_payload.append({
                "expected_index": ei,
                "predicted_index": pi,
                "type_match": same,
            })
        type_miss_kept = [
            i for i in type_miss
            if str(expected[i].get("grade") or "strict") != "advisory"
        ]
        for pi in type_extra:
            if _overlaps_advisory(predicted[pi], expected):
                advisory_extra.append(pi)
            else:
                kept_extra.append(pi)
        out["type"] = _type_accuracy_block(
            type_tp, type_wrong, type_matches_payload, type_miss_kept, kept_extra, advisory_extra
        )
    return out


def aggregate_cases(cases: Sequence[Mapping[str, Any]], metric: str) -> dict[str, Any]:
    totals = {"tp": 0, "fp": 0, "fn": 0}
    by_entity: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0}
    )
    type_correct = 0
    type_paired = 0
    type_by_entity: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    if metric == "strict":
        metric = "exact"
    for case in cases:
        result = case.get(metric) if metric != "type" else case.get("type") or {}
        if not result:
            continue
        if metric == "type":
            for match in result.get("matches") or []:
                expected = case["expected_spans"][match["expected_index"]]
                if str(expected.get("grade") or "strict") == "advisory":
                    continue
                type_paired += 1
                entity = expected["entity_type"]
                if match.get("type_match"):
                    type_correct += 1
                    type_by_entity[entity]["tp"] += 1
                else:
                    type_by_entity[entity]["fp"] += 1
            continue
        for key in totals:
            totals[key] += int(result[key])
        for match in result["matches"]:
            entity = case["expected_spans"][match["expected_index"]]["entity_type"]
            by_entity[entity]["tp"] += 1
        for index in result["missed_expected_indexes"]:
            by_entity[case["expected_spans"][index]["entity_type"]]["fn"] += 1
        for index in result["extra_predicted_indexes"]:
            by_entity[case["predicted_spans"][index]["entity_type"]]["fp"] += 1
    if metric == "type":
        acc = (type_correct / type_paired) if type_paired else 1.0
        micro = {
            "accuracy": round(acc, 6),
            "paired": type_paired,
            "type_match": type_correct,
            "type_mismatch": type_paired - type_correct,
        }
        entities = {}
        for entity, counts in sorted(type_by_entity.items()):
            paired = counts["tp"] + counts["fp"]
            a = (counts["tp"] / paired) if paired else 1.0
            entities[entity] = {
                "accuracy": round(a, 6),
                "paired": paired,
                "type_match": counts["tp"],
                "type_mismatch": counts["fp"],
            }
        return {"micro": micro, "by_entity": entities}
    return {
        "micro": rates(**totals),
        "by_entity": {
            entity: rates(**counts) for entity, counts in sorted(by_entity.items())
        },
    }


def _span_tags(
    case: Mapping[str, Any],
    span: Mapping[str, Any] | None,
    *,
    n_expected: int,
) -> list[str]:
    if span:
        tags = [str(t).strip() for t in (span.get("tags") or []) if str(t).strip()]
        if tags:
            return tags
    case_tags = [str(t).strip() for t in (case.get("tags") or []) if str(t).strip()]
    if n_expected <= 1:
        return case_tags
    return []


def aggregate_tags(cases: Sequence[Mapping[str, Any]], metric: str = "value") -> dict[str, Any]:
    """Per-span tag metrics. Case-level tags are inherited only on single-span cases."""
    by_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    key = "exact" if metric == "strict" else metric
    for case in cases:
        result = case.get(key) or {}
        expected = list(case.get("expected_spans") or [])
        predicted = list(case.get("predicted_spans") or [])
        n_expected = len(expected)
        if key == "type":
            continue
        for match in result.get("matches") or []:
            ei = int(match["expected_index"])
            span = expected[ei] if 0 <= ei < len(expected) else None
            for tag in _span_tags(case, span, n_expected=n_expected):
                by_tag[tag]["tp"] += 1
        for index in result.get("missed_expected_indexes") or []:
            span = expected[int(index)] if 0 <= int(index) < len(expected) else None
            for tag in _span_tags(case, span, n_expected=n_expected):
                by_tag[tag]["fn"] += 1
        for index in result.get("extra_predicted_indexes") or []:
            span = predicted[int(index)] if 0 <= int(index) < len(predicted) else None
            tags = _span_tags(case, span, n_expected=n_expected)
            for tag in tags:
                by_tag[tag]["fp"] += 1
    return {
        tag: rates(**counts) for tag, counts in sorted(by_tag.items())
    }


def aggregate_classes(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    totals = {name: 0 for name in MATCH_CLASSES}
    for case in cases:
        dist = case.get("class_distribution") or class_distribution(case.get("match_classes") or [])
        for name, count in dist.items():
            if name in totals:
                totals[name] += int(count or 0)
    return totals


def evaluate_dataset(
    dataset: Mapping[str, Any],
    predictions: Mapping[str, Sequence[Any]],
    *,
    overlap_iou: float = 0.5,
    allowed_entity_types: Iterable[str] | None = None,
    default_language: str = "en",
    require_redibis_version: bool = False,
    profile: Mapping[str, Any] | None = None,
    tiers: Sequence[str] | None = None,
    normalization_profile: str | None = None,
) -> dict[str, Any]:
    """Validate and score all cases against predictions keyed by case id."""
    normalized = validate_dataset(
        dataset,
        allowed_entity_types=allowed_entity_types,
        default_language=default_language,
        require_redibis_version=require_redibis_version,
    )
    spec = profile or load_profile(
        normalization_profile
        or str(normalized.get("normalization_profile") or PROFILE_V1_ID)
    )
    wanted = resolve_tiers(tiers)
    cases = [
        evaluate_case(
            case,
            predictions.get(case["id"], ()),
            overlap_iou=overlap_iou,
            profile=spec,
            tiers=wanted,
        )
        for case in normalized["cases"]
    ]
    need_strict = any(t in wanted for t in ("strict", "exact"))
    need_value = "value" in wanted
    need_overlap = "overlap" in wanted
    need_type = "type" in wanted
    payload: dict[str, Any] = {
        "kind": REPORT_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "redibis_version": current_redibis_version(),
        "dataset_id": str(normalized.get("id") or ""),
        "offset_unit": OFFSET_UNIT,
        "overlap_iou": overlap_iou,
        "normalization_profile": profile_id_of(spec),
        "case_count": len(cases),
        "proposal_count": sum(len(case["proposal_spans"]) for case in cases),
        "scored_tiers": [t if t != "exact" else "strict" for t in wanted],
        "notes": {
            "guard_violations": (
                "A forbidden-span hit is counted as class=guard_violation and as a "
                "false positive on every scored tier."
            ),
            "class_budgets": (
                "Ratios use matched classes only (exclude missed/spurious/guard_violation)."
            ),
            "type_tier": "accuracy only; precision/recall/f1 are not emitted",
            "by_tag": "attributed per span; case tags inherit only on single-span cases",
        },
        "class_distribution": aggregate_classes(cases),
        "cases": cases,
    }
    if need_strict:
        exact = aggregate_cases(cases, "exact")
        payload["exact"] = exact
        payload["strict"] = exact
    if need_value:
        payload["value"] = aggregate_cases(cases, "value")
        payload["by_tag"] = aggregate_tags(cases, "value")
    elif need_strict:
        payload["by_tag"] = aggregate_tags(cases, "exact")
    if need_overlap:
        payload["overlap"] = aggregate_cases(cases, "overlap")
    if need_type:
        payload["type"] = aggregate_cases(cases, "type")
    return payload
