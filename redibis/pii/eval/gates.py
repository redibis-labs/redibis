"""Post-process eval reports against per-entity gates, class budgets, and a baseline.

``class_budgets`` ratios use matched classes only (exact/equivalent/superset/subset/
overlap_partial/type_mismatch/split/merged). ``missed``, ``spurious``, and
``guard_violation`` are excluded from the denominator — the budget is not
"% of all spans".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from redibis.pii.eval.classify import MATCH_CLASSES


class GateError(ValueError):
    """Invalid gate file."""


_TIER_KEYS = {
    "strict_f1": ("strict", "f1"),
    "strict_precision": ("strict", "precision"),
    "strict_recall": ("strict", "recall"),
    "value_f1": ("value", "f1"),
    "value_precision": ("value", "precision"),
    "value_recall": ("value", "recall"),
    "overlap_f1": ("overlap", "f1"),
    "overlap_precision": ("overlap", "precision"),
    "overlap_recall": ("overlap", "recall"),
    "type_f1": ("type", "accuracy"),
    "type_accuracy": ("type", "accuracy"),
    "exact_f1": ("exact", "f1"),
}


def load_gate_file(path: str | Path) -> dict[str, Any]:
    raw_path = Path(path)
    try:
        import yaml

        data = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise GateError(f"cannot read gate file {raw_path}: {exc}") from exc
    except Exception as exc:
        raise GateError(f"invalid gate file {raw_path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise GateError("gate file must be a YAML mapping")
    _validate_gate_keys(data)
    return dict(data)


def _validate_gate_keys(gates: Mapping[str, Any]) -> None:
    micro_req = dict(gates.get("micro") or {})
    for key in micro_req:
        if key not in _TIER_KEYS:
            raise GateError(f"unknown micro gate key {key!r}; expected one of {sorted(_TIER_KEYS)}")
    by_entity_req = dict(gates.get("by_entity") or {})
    for entity, reqs in by_entity_req.items():
        if not isinstance(reqs, Mapping):
            raise GateError(f"by_entity.{entity} must be a mapping of metric keys")
        for key in reqs:
            if key not in _TIER_KEYS:
                raise GateError(
                    f"unknown by_entity.{entity} gate key {key!r}; expected one of {sorted(_TIER_KEYS)}"
                )
    budgets = dict(gates.get("class_budgets") or {})
    for key in budgets:
        if not str(key).endswith("_max"):
            raise GateError(
                f"class_budgets key {key!r} must end in _max (e.g. superset_max)"
            )
        cls = str(key)[: -len("_max")]
        if cls not in MATCH_CLASSES:
            raise GateError(f"class_budgets names unknown class {cls!r}")


def _micro(report: Mapping[str, Any], tier: str) -> dict[str, Any]:
    block = report.get(tier) or report.get("exact" if tier == "strict" else "") or {}
    return dict((block or {}).get("micro") or {})


def _entity(report: Mapping[str, Any], tier: str, entity: str) -> dict[str, Any]:
    block = report.get(tier) or report.get("exact" if tier == "strict" else "") or {}
    by_entity = (block or {}).get("by_entity") or {}
    return dict(by_entity.get(entity) or {})


def _metric(block: Mapping[str, Any], name: str) -> float:
    if name == "accuracy":
        if "accuracy" in block:
            return float(block.get("accuracy") or 0.0)
        return float(block.get("f1") or 0.0)
    if name == "f1" and "f1" not in block and "accuracy" in block:
        return float(block.get("accuracy") or 0.0)
    return float(block.get(name) or 0.0)


def _class_counts(report: Mapping[str, Any]) -> dict[str, int]:
    dist = dict(report.get("class_distribution") or {})
    if dist:
        return {k: int(v or 0) for k, v in dist.items()}
    counts = {name: 0 for name in MATCH_CLASSES}
    for case in report.get("cases") or []:
        for row in case.get("match_classes") or []:
            name = str(row.get("class") or "")
            if name in counts:
                counts[name] += 1
    if not dist:
        # batch reports nest cases under files
        for entry in report.get("files") or []:
            for case in entry.get("cases") or []:
                for row in case.get("match_classes") or []:
                    name = str(row.get("class") or "")
                    if name in counts:
                        counts[name] += 1
    return counts


def _report_entities(report: Mapping[str, Any]) -> set[str]:
    ents: set[str] = set()
    for tier in ("strict", "exact", "value", "overlap", "type"):
        block = report.get(tier) or {}
        ents.update(str(k) for k in ((block or {}).get("by_entity") or {}))
    for case in report.get("cases") or []:
        for span in list(case.get("expected_spans") or []) + list(case.get("predicted_spans") or []):
            et = str(span.get("entity_type") or "").strip()
            if et:
                ents.add(et)
    for entry in report.get("files") or []:
        for case in entry.get("cases") or []:
            for span in list(case.get("expected_spans") or []) + list(case.get("predicted_spans") or []):
                et = str(span.get("entity_type") or "").strip()
                if et:
                    ents.add(et)
        for tier in ("strict", "exact", "value", "overlap", "type"):
            block = entry.get(tier) or {}
            ents.update(str(k) for k in ((block or {}).get("by_entity") or {}))
    return ents


def _matched_span_count(counts: Mapping[str, int]) -> int:
    matched = (
        int(counts.get("exact") or 0)
        + int(counts.get("equivalent") or 0)
        + int(counts.get("superset") or 0)
        + int(counts.get("subset") or 0)
        + int(counts.get("overlap_partial") or 0)
        + int(counts.get("type_mismatch") or 0)
        + int(counts.get("split") or 0)
        + int(counts.get("merged") or 0)
    )
    return matched


def evaluate_gates(
    report: Mapping[str, Any],
    gates: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a verdict block. Advisory spans are never gated (already excluded)."""
    _validate_gate_keys(gates)
    failures: list[dict[str, Any]] = []
    advisory_mode = str(gates.get("advisory") or "report_only")
    present = _report_entities(report)

    def fail(entity: str, tier: str, metric: str, got: float, need: float, extra: str = "") -> None:
        failures.append({
            "entity": entity,
            "tier": tier,
            "metric": metric,
            "got": round(float(got), 6),
            "need": round(float(need), 6),
            "detail": extra,
        })

    micro_req = dict(gates.get("micro") or {})
    for key, threshold in micro_req.items():
        tier, metric = _TIER_KEYS[key]
        got = _metric(_micro(report, tier), metric)
        if got < float(threshold):
            fail("*", tier, metric, got, float(threshold))

    by_entity_req = dict(gates.get("by_entity") or {})
    for entity, reqs in by_entity_req.items():
        name = str(entity)
        if name not in present:
            raise GateError(f"gate names {name}; corpus has no {name} spans")
        for key, threshold in reqs.items():
            tier, metric = _TIER_KEYS[key]
            got = _metric(_entity(report, tier, name), metric)
            if got < float(threshold):
                fail(name, tier, metric, got, float(threshold))

    max_guards = gates.get("guard_violations_max")
    counts = _class_counts(report)
    if max_guards is not None:
        got_g = int(counts.get("guard_violation") or 0)
        if got_g > int(max_guards):
            fail("*", "guard", "violations", got_g, int(max_guards))

    budgets = dict(gates.get("class_budgets") or {})
    matched = _matched_span_count(counts)
    for key, threshold in budgets.items():
        cls = str(key)[: -len("_max")]
        got_n = int(counts.get(cls) or 0)
        ratio = (got_n / matched) if matched else (0.0 if got_n == 0 else 1.0)
        if ratio > float(threshold):
            fail("*", "class", cls, ratio, float(threshold), extra=f"{got_n}/{matched}")

    regression = dict(gates.get("regression") or {})
    base = baseline
    if base is None and regression.get("baseline"):
        base = _load_json_report(str(regression["baseline"]))
    max_drop = regression.get("max_drop")
    entity_drops = dict(regression.get("by_entity") or {})
    deltas = {}
    if base is not None:
        for tier in ("strict", "value", "overlap", "type", "exact"):
            metric_name = "accuracy" if tier == "type" else "f1"
            cur = _metric(_micro(report, tier if tier != "exact" else "exact"), metric_name)
            prev = _metric(_micro(base, tier if tier != "exact" else "exact"), metric_name)
            delta = cur - prev
            deltas[f"{tier}_{metric_name}"] = round(delta, 6)
            if max_drop is not None and delta < -abs(float(max_drop)):
                fail("*", tier, metric_name, cur, prev - abs(float(max_drop)), extra=f"drop {delta}")
        if max_drop is not None or entity_drops:
            cur_ent = ((report.get("value") or report.get("strict") or {}).get("by_entity") or {})
            prev_ent = ((base.get("value") or base.get("strict") or {}).get("by_entity") or {})
            for entity, cur_block in cur_ent.items():
                limit = entity_drops.get(entity)
                drop = None
                if isinstance(limit, Mapping):
                    drop = limit.get("max_drop", max_drop)
                elif limit is not None:
                    drop = limit
                elif max_drop is not None:
                    drop = max_drop
                if drop is None:
                    continue
                prev_block = prev_ent.get(entity) or {}
                if not prev_block:
                    continue
                cur_f1 = _metric(cur_block, "f1")
                prev_f1 = _metric(prev_block, "f1")
                delta = cur_f1 - prev_f1
                deltas[f"{entity}_value_f1"] = round(delta, 6)
                if delta < -abs(float(drop)):
                    fail(str(entity), "value", "f1", cur_f1, prev_f1 - abs(float(drop)), extra=f"drop {delta}")

    truncated = list(report.get("truncated_cases") or [])
    if truncated:
        fail(
            "*",
            "coverage",
            "truncated",
            len(truncated),
            0,
            extra=",".join(str(x) for x in truncated),
        )

    passed = not failures
    named = ""
    if failures:
        first = failures[0]
        named = f"{first['entity']} {first['tier']} {first['metric']} {first['got']} < {first['need']}"
    return {
        "passed": passed,
        "advisory": advisory_mode,
        "failures": failures,
        "summary": "pass" if passed else f"fail: {named}",
        "deltas": deltas,
        "class_counts": counts,
    }


def apply_gates_to_report(
    report: dict[str, Any],
    gates: Mapping[str, Any] | None,
    *,
    baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not gates:
        return report
    report["gates"] = evaluate_gates(report, gates, baseline=baseline)
    return report


def format_gate_failure(verdict: Mapping[str, Any]) -> str:
    failures: Sequence[Mapping[str, Any]] = verdict.get("failures") or []
    if not failures:
        return "pii eval: gates passed"
    parts = []
    for row in failures:
        parts.append(
            f"{row.get('entity')} {row.get('tier')} {row.get('metric')} "
            f"{row.get('got')} < {row.get('need')}"
            + (f" ({row.get('detail')})" if row.get("detail") else "")
        )
    return "pii eval: gate failure: " + "; ".join(parts)


def _load_json_report(path: str) -> dict[str, Any]:
    import json

    return json.loads(Path(path).read_text(encoding="utf-8"))
