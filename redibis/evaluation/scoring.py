"""Deterministic scoring of structured column evaluation matrices."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from redibis.evaluation.schema import REPORT_KIND, SCHEMA_VERSION, TableEvalError, validate_table_dataset
from redibis.pii.eval.span_metrics import current_redibis_version, rates

CATEGORICAL_FIELDS = (
    "is_pii",
    "entity_type",
    "logicalType",
    "privacy_classification",
    "businessName",
    "description",
    "business_definition",
)


def _norm(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value or "").strip()


def _tag_rates(expected: Sequence[str], actual: Sequence[str]) -> dict[str, Any]:
    exp = set(expected)
    act = set(actual)
    tp = len(exp & act)
    fp = len(act - exp)
    fn = len(exp - act)
    return {**rates(tp, fp, fn), "expected": sorted(exp), "actual": sorted(act)}


def score_column(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    semantic_scores: Optional[Mapping[str, bool]] = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    tp = fp = fn = 0
    missing_actual = bool(actual.get("_missing_actual"))
    fields["present"] = {
        "match": not missing_actual,
        "expected": True,
        "actual": not missing_actual,
    }
    if missing_actual:
        fn += 1
    for field in CATEGORICAL_FIELDS:
        exp = expected.get(field)
        act = actual.get(field)
        if field == "is_pii":
            exp_n, act_n = bool(exp), bool(act)
            if missing_actual:
                fields[field] = {
                    "match": False,
                    "missing": True,
                    "expected": exp_n,
                    "actual": None,
                }
                continue
            hit = exp_n == act_n
            if hit:
                if exp_n:
                    tp += 1
                fields[field] = {"match": True, "expected": exp_n, "actual": act_n}
            elif exp_n and not act_n:
                fn += 1
                fields[field] = {"match": False, "expected": exp_n, "actual": act_n}
            else:
                fp += 1
                fields[field] = {"match": False, "expected": exp_n, "actual": act_n}
            continue
        if field == "entity_type" and not expected.get("is_pii"):
            fields[field] = {"match": True, "skipped": True, "expected": _norm(exp), "actual": _norm(act)}
            continue
        exp_n, act_n = _norm(exp), _norm(act)
        if not exp_n:
            fields[field] = {"match": True, "skipped": True, "expected": exp_n, "actual": act_n}
            continue
        hit = exp_n.casefold() == act_n.casefold()
        if semantic_scores and field in ("description", "business_definition") and field in semantic_scores:
            hit = bool(semantic_scores[field])
        if hit:
            tp += 1
        else:
            fn += 1
        fields[field] = {"match": hit, "expected": exp_n, "actual": act_n}
    tags = _tag_rates(expected.get("tags") or [], actual.get("tags") or [])
    tp += int(tags["tp"])
    fp += int(tags["fp"])
    fn += int(tags["fn"])
    fields["tags"] = tags
    return {
        "name": expected["name"],
        "is_proposal": bool(actual.get("is_proposal")),
        "source": str(actual.get("source") or ""),
        "fields": fields,
        "exact": rates(tp, fp, fn),
    }


def evaluate_table(
    dataset: Mapping[str, Any],
    actuals: Sequence[Mapping[str, Any]],
    *,
    target: str = "engine",
    table_columns: Sequence[str] | None = None,
    table_fingerprint: str = "",
    semantic_by_column: Optional[Mapping[str, Mapping[str, bool]]] = None,
) -> dict[str, Any]:
    if target not in ("engine", "contract", "llm"):
        raise TableEvalError(f"unknown target {target!r}")
    normalized = validate_table_dataset(
        dataset,
        table_columns=table_columns,
        table_fingerprint=table_fingerprint,
    )
    by_name = {str(row.get("name") or ""): dict(row) for row in actuals}
    expected_names = {str(row["name"]) for row in normalized["columns"]}
    actual_names = {name for name in by_name if name}
    actual_missing = sorted(expected_names - actual_names)
    actual_extra = sorted(actual_names - expected_names)
    scored = []
    totals = {"tp": 0, "fp": len(actual_extra), "fn": 0}
    pii_totals = {"tp": 0, "fp": 0, "fn": 0}
    for expected in normalized["columns"]:
        actual = by_name.get(expected["name"])
        if actual is None:
            actual = {
                "name": expected["name"],
                "is_pii": False,
                "_missing_actual": True,
            }
        if target != "llm":
            actual = {**actual, "is_proposal": False}
        elif actual.get("is_proposal") is None:
            actual = {**actual, "is_proposal": True}
        row = score_column(
            expected,
            actual,
            semantic_scores=(semantic_by_column or {}).get(expected["name"]),
        )
        for key in totals:
            totals[key] += int(row["exact"][key])
        pii_field = row["fields"]["is_pii"]
        if pii_field["match"] and expected["is_pii"]:
            pii_totals["tp"] += 1
        elif expected["is_pii"] and not actual.get("is_pii"):
            pii_totals["fn"] += 1
        elif (not expected["is_pii"]) and actual.get("is_pii"):
            pii_totals["fp"] += 1
        scored.append(row)
    return {
        "kind": REPORT_KIND,
        "schema_version": SCHEMA_VERSION,
        "redibis_version": current_redibis_version(),
        "table_name": normalized["table_name"],
        "target": target,
        "fingerprint": normalized["fingerprint"],
        "stale_fingerprint": normalized["stale_fingerprint"],
        "missing_columns": normalized["missing_columns"],
        "extra_columns": normalized["extra_columns"],
        "actual_missing_columns": actual_missing,
        "actual_extra_columns": actual_extra,
        "coverage": {
            "expected": len(expected_names),
            "present": len(expected_names) - len(actual_missing),
            "missing": len(actual_missing),
            "extra": len(actual_extra),
        },
        "column_count": len(scored),
        "exact": {"micro": rates(**totals)},
        "pii": {"micro": rates(**pii_totals)},
        "columns": scored,
    }
