"""Per-span match classification for free-text PII evaluation."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from redibis.pii.eval.normalize import (
    PROFILE_V1,
    compare_span_values,
)

MATCH_CLASSES = (
    "exact",
    "equivalent",
    "superset",
    "subset",
    "overlap_partial",
    "type_mismatch",
    "split",
    "merged",
    "missed",
    "spurious",
    "guard_violation",
)

_PARTIAL = frozenset({"equivalent", "superset", "subset", "overlap_partial"})
_VALUE_HIT = frozenset({"exact", "equivalent", "superset"})
# Named beside ``iou_threshold``: fraction of gold characters a split must cover.
SPLIT_COVERAGE_THRESHOLD = 0.8


def overlap_len(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    return max(0, min(int(left["end"]), int(right["end"])) - max(int(left["start"]), int(right["start"])))


def iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    ov = overlap_len(left, right)
    if not ov:
        return 0.0
    union = max(int(left["end"]), int(right["end"])) - min(int(left["start"]), int(right["start"]))
    return ov / union if union else 0.0


def _contains(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool:
    return int(outer["start"]) <= int(inner["start"]) and int(outer["end"]) >= int(inner["end"])


def coverage_precision(expected: Mapping[str, Any], predicted: Mapping[str, Any]) -> tuple[float, float]:
    ov = overlap_len(expected, predicted)
    exp_len = max(1, int(expected["end"]) - int(expected["start"]))
    pred_len = max(1, int(predicted["end"]) - int(predicted["start"]))
    return ov / exp_len, ov / pred_len


def extra_sides(text: str, expected: Mapping[str, Any], predicted: Mapping[str, Any]) -> dict[str, Any]:
    t = text or ""
    extra_left = t[int(predicted["start"]): int(expected["start"])] if int(predicted["start"]) < int(expected["start"]) else ""
    extra_right = t[int(expected["end"]): int(predicted["end"])] if int(predicted["end"]) > int(expected["end"]) else ""
    missing_left = t[int(expected["start"]): int(predicted["start"])] if int(predicted["start"]) > int(expected["start"]) else ""
    missing_right = t[int(predicted["end"]): int(expected["end"])] if int(predicted["end"]) < int(expected["end"]) else ""
    extra_n = len(extra_left) + len(extra_right)
    missing_n = len(missing_left) + len(missing_right)
    delta = extra_n - missing_n
    parts: list[str] = []
    if extra_n:
        shown = f"{extra_left}{extra_right}"
        parts.append(f'+{extra_n} {shown!r}')
    if missing_n:
        shown = f"{missing_left}{missing_right}"
        parts.append(f'-{missing_n} {shown!r}')
    return {
        "extra_left": extra_left,
        "extra_right": extra_right,
        "missing_left": missing_left,
        "missing_right": missing_right,
        "delta_chars": delta,
        "delta_label": " ".join(parts) if parts else "—",
    }


def _span_id(span: Mapping[str, Any], prefix: str, index: int) -> str:
    raw = str(span.get("id") or "").strip()
    return raw or f"{prefix}{index}"


def _values_equal(
    text: str,
    expected: Mapping[str, Any],
    predicted: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> tuple[bool, str]:
    return compare_span_values(text, expected, predicted, profile=profile)


def _row(
    *,
    cls: str,
    text: str,
    expected: Mapping[str, Any] | None,
    predicted: Mapping[str, Any] | None,
    expected_index: int | None = None,
    predicted_index: int | None = None,
    extra: Optional[Mapping[str, Any]] = None,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cov, prec = (0.0, 0.0)
    sides: dict[str, Any] = {}
    iou_v = 0.0
    value_equal = False
    canonical_match = "n/a"
    exp_type = str(expected.get("entity_type") or "") if expected is not None else None
    got_type = str(predicted.get("entity_type") or "") if predicted is not None else None
    types_match = bool(exp_type) and exp_type == got_type
    if expected is not None and predicted is not None:
        cov, prec = coverage_precision(expected, predicted)
        sides = extra_sides(text, expected, predicted)
        iou_v = round(iou(expected, predicted), 6)
        value_equal, canonical_match = _values_equal(
            text, expected, predicted, profile or PROFILE_V1
        )
    out: dict[str, Any] = {
        "class": cls,
        "expected_index": expected_index,
        "predicted_index": predicted_index,
        "expected_id": _span_id(expected, "e", expected_index or 0) if expected is not None else None,
        "predicted_id": _span_id(predicted, "p", predicted_index or 0) if predicted is not None else None,
        "expected_type": exp_type,
        "got_type": got_type,
        "coverage": round(cov, 6) if expected is not None and predicted is not None else None,
        "char_precision": round(prec, 6) if expected is not None and predicted is not None else None,
        "iou": iou_v if expected is not None and predicted is not None else None,
        "grade": str(expected.get("grade") or "strict") if expected is not None else None,
        "value_equal": bool(value_equal),
        "is_value_hit": bool(value_equal) and types_match,
        "canonical_match": canonical_match,
    }
    out.update(sides)
    if extra:
        out.update(dict(extra))
    return out


def classify_case(
    text: str,
    expected: Sequence[Mapping[str, Any]],
    predicted: Sequence[Mapping[str, Any]],
    *,
    forbidden: Sequence[Mapping[str, Any]] | None = None,
    profile: Mapping[str, Any] | None = None,
    iou_threshold: float = 0.5,
    split_coverage: float = SPLIT_COVERAGE_THRESHOLD,
) -> list[dict[str, Any]]:
    """Assign exactly one class to every expected span and leftover prediction."""
    spec = profile or PROFILE_V1
    golds = list(expected)
    preds = list(predicted)
    forbids = list(forbidden or ())
    used_g: set[int] = set()
    used_p: set[int] = set()
    rows: list[dict[str, Any]] = []

    def row(**kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("profile", spec)
        kwargs.setdefault("text", text)
        return _row(**kwargs)

    for pi, pred in enumerate(preds):
        for forbidden_span in forbids:
            same_type = (
                not forbidden_span.get("entity_type")
                or str(forbidden_span.get("entity_type") or "").upper()
                == str(pred.get("entity_type") or "").upper()
            )
            if same_type and overlap_len(pred, forbidden_span) > 0:
                rows.append(
                    row(
                        cls="guard_violation",
                        expected=forbidden_span,
                        predicted=pred,
                        predicted_index=pi,
                        extra={
                            "reason": str(forbidden_span.get("reason") or ""),
                            "forbidden": {
                                "start": int(forbidden_span["start"]),
                                "end": int(forbidden_span["end"]),
                                "entity_type": str(forbidden_span.get("entity_type") or ""),
                            },
                        },
                    )
                )
                used_p.add(pi)
                break

    for gi, gold in enumerate(golds):
        if gi in used_g:
            continue
        for pi, pred in enumerate(preds):
            if pi in used_p:
                continue
            if (
                int(gold["start"]) == int(pred["start"])
                and int(gold["end"]) == int(pred["end"])
                and gold["entity_type"] == pred["entity_type"]
            ):
                rows.append(row(cls="exact", expected=gold, predicted=pred, expected_index=gi, predicted_index=pi))
                used_g.add(gi)
                used_p.add(pi)
                break

    for gi, gold in enumerate(golds):
        if gi in used_g:
            continue
        for pi, pred in enumerate(preds):
            if pi in used_p:
                continue
            if (
                int(gold["start"]) == int(pred["start"])
                and int(gold["end"]) == int(pred["end"])
                and gold["entity_type"] != pred["entity_type"]
            ):
                rows.append(
                    row(
                        cls="type_mismatch",
                        expected=gold,
                        predicted=pred,
                        expected_index=gi,
                        predicted_index=pi,
                    )
                )
                used_g.add(gi)
                used_p.add(pi)
                break

    for pi, pred in enumerate(preds):
        if pi in used_p:
            continue
        covered = [
            gi
            for gi, gold in enumerate(golds)
            if gi not in used_g
            and gold["entity_type"] == pred["entity_type"]
            and _contains(pred, gold)
            and overlap_len(pred, gold) > 0
        ]
        if len(covered) >= 2:
            ids = [_span_id(golds[gi], "e", gi) for gi in covered]
            rows.append(
                row(
                    cls="merged",
                    expected=golds[covered[0]],
                    predicted=pred,
                    expected_index=covered[0],
                    predicted_index=pi,
                    extra={"expected_ids": ids, "expected_indexes": covered},
                )
            )
            used_p.add(pi)
            used_g.update(covered)
            for gi in covered[1:]:
                rows.append(
                    row(
                        cls="merged",
                        expected=golds[gi],
                        predicted=pred,
                        expected_index=gi,
                        predicted_index=pi,
                        extra={"expected_ids": ids, "expected_indexes": covered},
                    )
                )

    for gi, gold in enumerate(golds):
        if gi in used_g:
            continue
        parts = [
            pi
            for pi, pred in enumerate(preds)
            if pi not in used_p
            and pred["entity_type"] == gold["entity_type"]
            and overlap_len(gold, pred) > 0
        ]
        if len(parts) < 2:
            continue
        covered = 0
        for pi in parts:
            covered += overlap_len(gold, preds[pi])
        exp_len = int(gold["end"]) - int(gold["start"])
        if exp_len and covered / exp_len >= split_coverage:
            pred_ids = [_span_id(preds[pi], "p", pi) for pi in parts]
            rows.append(
                row(
                    cls="split",
                    expected=gold,
                    predicted=preds[parts[0]],
                    expected_index=gi,
                    predicted_index=parts[0],
                    extra={"predicted_ids": pred_ids, "predicted_indexes": parts},
                )
            )
            used_g.add(gi)
            used_p.update(parts)

    candidates: list[tuple[tuple[Any, ...], int, int]] = []
    for gi, gold in enumerate(golds):
        if gi in used_g:
            continue
        for pi, pred in enumerate(preds):
            if pi in used_p:
                continue
            val_eq, _ = _values_equal(text, gold, pred, spec)
            ov = overlap_len(gold, pred)
            if ov == 0 and not val_eq:
                continue
            same_type = gold["entity_type"] == pred["entity_type"]
            score = (1 if same_type else 0, 1 if val_eq else 0, iou(gold, pred), ov)
            candidates.append((score, gi, pi))
    # Highest score first; ties resolve to the earliest gold then earliest prediction.
    candidates.sort(key=lambda c: (tuple(-x for x in c[0]), c[1], c[2]))
    for _score, gi, pi in candidates:
        if gi in used_g or pi in used_p:
            continue
        gold, pred = golds[gi], preds[pi]
        same_type = gold["entity_type"] == pred["entity_type"]
        val_eq, _ = _values_equal(text, gold, pred, spec)
        cov, prec = coverage_precision(gold, pred)
        ov = overlap_len(gold, pred)
        if not same_type:
            if val_eq or iou(gold, pred) >= iou_threshold or (
                int(gold["start"]) == int(pred["start"]) and int(gold["end"]) == int(pred["end"])
            ):
                rows.append(
                    row(
                        cls="type_mismatch",
                        expected=gold,
                        predicted=pred,
                        expected_index=gi,
                        predicted_index=pi,
                    )
                )
                used_g.add(gi)
                used_p.add(pi)
            continue
        if int(gold["start"]) == int(pred["start"]) and int(gold["end"]) == int(pred["end"]):
            cls = "exact"
        elif val_eq and cov >= 0.999 and prec < 0.999:
            cls = "superset"
        elif val_eq:
            cls = "equivalent"
        elif _contains(pred, gold) and cov >= 0.999:
            cls = "superset"
        elif _contains(gold, pred) and prec >= 0.999:
            cls = "subset"
        elif ov > 0:
            cls = "overlap_partial"
        else:
            continue
        rows.append(row(cls=cls, expected=gold, predicted=pred, expected_index=gi, predicted_index=pi))
        used_g.add(gi)
        used_p.add(pi)

    for gi, gold in enumerate(golds):
        if gi in used_g:
            continue
        rows.append(row(cls="missed", expected=gold, predicted=None, expected_index=gi))
    for pi, pred in enumerate(preds):
        if pi in used_p:
            continue
        rows.append(row(cls="spurious", expected=None, predicted=pred, predicted_index=pi))
    return rows


def class_distribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in MATCH_CLASSES}
    for row in rows:
        name = str(row.get("class") or "")
        if name in counts:
            counts[name] += 1
    return counts


def _types_match(row: Mapping[str, Any]) -> bool:
    expected = row.get("expected_type")
    got = row.get("got_type")
    return (
        expected is not None
        and got is not None
        and str(expected) != ""
        and str(expected) == str(got)
    )


def is_value_hit(row_or_cls: Mapping[str, Any] | str) -> bool:
    """Whether this pair counts toward value-tier F1.

    ``value_equal`` is pure string/canonical equality — true on a type_mismatch
    of the same characters. A value hit additionally requires the types to match.
    """
    if isinstance(row_or_cls, Mapping):
        types_match = _types_match(row_or_cls)
        if "value_equal" in row_or_cls:
            return bool(row_or_cls.get("value_equal")) and types_match
        if "is_value_hit" in row_or_cls:
            return bool(row_or_cls.get("is_value_hit"))
        cls = str(row_or_cls.get("class") or "")
        if cls in _VALUE_HIT:
            if row_or_cls.get("expected_type") is None and row_or_cls.get("got_type") is None:
                return True
            return types_match
        return False
    return row_or_cls in _VALUE_HIT
