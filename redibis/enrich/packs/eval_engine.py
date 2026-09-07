"""Deterministic evaluation assertions for enrichment packs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AssertionResult:
    path: str
    ok: bool
    message: str
    weight: float = 1.0


@dataclass
class CaseEvalResult:
    case_id: str
    ok: bool
    score: float
    minimum_score: float
    assertions: list[AssertionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "score": self.score,
            "minimum_score": self.minimum_score,
            "assertions": [
                {"path": a.path, "ok": a.ok, "message": a.message, "weight": a.weight}
                for a in self.assertions
            ],
            "errors": list(self.errors),
        }


def _resolve_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _check_equals(actual: Any, expected: Any) -> bool:
    return actual == expected


def evaluate_assertions(
    delta: dict[str, Any],
    assertions: dict[str, Any],
) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    required = assertions.get("required") or {}
    for path, rules in required.items():
        actual = _resolve_path(delta, path)
        if isinstance(rules, dict):
            if "equals" in rules and not _check_equals(actual, rules["equals"]):
                results.append(
                    AssertionResult(
                        path=path,
                        ok=False,
                        message=f"expected equals {rules['equals']!r}, got {actual!r}",
                        weight=1.0,
                    )
                )
                continue
            if "type" in rules:
                expected_type = str(rules["type"])
                type_ok = {
                    "string": isinstance(actual, str),
                    "number": isinstance(actual, (int, float)) and not isinstance(actual, bool),
                    "boolean": isinstance(actual, bool),
                    "object": isinstance(actual, dict),
                    "array": isinstance(actual, list),
                }.get(expected_type, False)
                if not type_ok:
                    results.append(
                        AssertionResult(
                            path=path,
                            ok=False,
                            message=f"expected type {expected_type}, got {type(actual).__name__}",
                            weight=1.0,
                        )
                    )
                    continue
            if "minLength" in rules:
                if not isinstance(actual, str) or len(actual) < int(rules["minLength"]):
                    results.append(
                        AssertionResult(
                            path=path,
                            ok=False,
                            message=f"minLength {rules['minLength']} not met",
                            weight=1.0,
                        )
                    )
                    continue
            if "maxLength" in rules:
                if not isinstance(actual, str) or len(actual) > int(rules["maxLength"]):
                    results.append(
                        AssertionResult(
                            path=path,
                            ok=False,
                            message=f"maxLength {rules['maxLength']} exceeded",
                            weight=1.0,
                        )
                    )
                    continue
            if "exists" in rules:
                exists = actual is not None
                if bool(rules["exists"]) != exists:
                    results.append(
                        AssertionResult(
                            path=path,
                            ok=False,
                            message=f"exists={rules['exists']} failed",
                            weight=1.0,
                        )
                    )
                    continue
        results.append(AssertionResult(path=path, ok=True, message="ok", weight=1.0))

    contains = assertions.get("contains") or {}
    for path, expected_items in contains.items():
        actual = _resolve_path(delta, path)
        if not isinstance(actual, list):
            results.append(
                AssertionResult(
                    path=path,
                    ok=False,
                    message="contains requires a list value",
                    weight=0.5,
                )
            )
            continue
        missing = [item for item in (expected_items or []) if item not in actual]
        results.append(
            AssertionResult(
                path=path,
                ok=not missing,
                message="ok" if not missing else f"missing items: {missing}",
                weight=0.5,
            )
        )

    for forbid in assertions.get("forbidden") or []:
        if isinstance(forbid, dict):
            path = str(forbid.get("path") or "")
            reason = str(forbid.get("reason") or "forbidden")
        else:
            path = str(forbid)
            reason = "forbidden"
        actual = _resolve_path(delta, path)
        results.append(
            AssertionResult(
                path=path,
                ok=actual is None,
                message="ok" if actual is None else reason,
                weight=1.0,
            )
        )
    return results


def evaluate_case(
    case: dict[str, Any],
    delta: dict[str, Any],
) -> CaseEvalResult:
    case_id = str(case.get("id") or "unknown")
    assertions = case.get("assertions") or {}
    scoring = case.get("scoring") or {}
    minimum = float(scoring.get("minimumScore") or 1.0)
    results = evaluate_assertions(delta, assertions)
    if not results:
        return CaseEvalResult(
            case_id=case_id,
            ok=True,
            score=1.0,
            minimum_score=minimum,
            assertions=[],
        )
    total_w = sum(a.weight for a in results) or 1.0
    earned = sum(a.weight for a in results if a.ok)
    score = earned / total_w
    return CaseEvalResult(
        case_id=case_id,
        ok=score >= minimum and all(a.ok for a in results if a.weight >= 1.0),
        score=round(score, 4),
        minimum_score=minimum,
        assertions=results,
    )


def evaluate_pack_deltas(
    cases: list[dict[str, Any]],
    deltas_by_case: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    case_results: list[CaseEvalResult] = []
    for case in cases:
        case_id = str(case.get("id") or "")
        delta = deltas_by_case.get(case_id)
        if delta is None:
            case_results.append(
                CaseEvalResult(
                    case_id=case_id or "unknown",
                    ok=False,
                    score=0.0,
                    minimum_score=float((case.get("scoring") or {}).get("minimumScore") or 1.0),
                    errors=[f"missing delta for case {case_id!r}"],
                )
            )
            continue
        case_results.append(evaluate_case(case, delta))
    aggregate = (
        round(sum(r.score for r in case_results) / len(case_results), 4)
        if case_results
        else 0.0
    )
    return {
        "ok": all(r.ok for r in case_results),
        "aggregate_score": aggregate,
        "cases": [r.to_dict() for r in case_results],
    }
