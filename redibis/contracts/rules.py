"""
redibis.contracts.rules — quality rules round-trip (v2 §1.4 / §3.4).

The contract is the single source of truth. Quality rules are stored
ODCS-native (portable) and regenerated to any target on demand:

    extract_rules(contract)                 → list[QualityRule]   (canonical)
    regenerate(rules, "great_expectations") → GE ExpectationSuite (JSON dict)
    regenerate(rules, "sodacl")             → SodaCL checks.yml   (str)
    regenerate(rules, "dbt")                → dbt schema.yml      (str)

``mapper.py`` already does GE → ODCS; this module adds the **reverse**
(ODCS → GE) so a hand-edited contract still regenerates a valid GE suite.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def stable_rule_id(column: Optional[str], q: dict) -> str:
    """Deterministic rule id for overlay matching (survives re-ordering)."""
    meta = q.get("meta") if isinstance(q.get("meta"), dict) else {}
    if meta.get("redibis_rule_id"):
        return str(meta["redibis_rule_id"])
    body = {k: v for k, v in q.items() if k != "meta"}
    payload = json.dumps({"column": column, "rule": body}, sort_keys=True, default=str)
    return "q_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Canonical portable rule
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class QualityRule:
    """A portable, regenerable quality rule (architecture §1.4)."""
    rule_id: str
    type: str                              # odcs DataQuality type: not_null, unique, between, set, regex, row_count...
    column: Optional[str] = None           # None = table-level
    params: dict = field(default_factory=dict)
    severity: str = "P1"                   # P0 | P1 | P2
    source: str = "profiler"               # profiler | discovery | manual | llm
    engine_hint: Optional[dict] = None     # GE-only fallback when no portable mapping
    description: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ─────────────────────────────────────────────────────────────────────────────
# Extract canonical rules from an ODCS contract
# ─────────────────────────────────────────────────────────────────────────────

def _odcs_quality_to_rule(rule_id: str, q: dict, column: Optional[str]) -> QualityRule:
    """Map one ODCS quality block to a canonical QualityRule."""
    description = q.get("description")
    severity = q.get("severity", "P1")

    # Engine-specific (GE-only) fallback block
    if q.get("engine") == "greatExpectations" or "implementation" in q:
        impl = q.get("implementation", {})
        return QualityRule(
            rule_id=rule_id, type="engine", column=column,
            params={}, severity=severity, source="profiler",
            engine_hint={"expectation_type": impl.get("expectation_type"),
                         "kwargs": impl.get("kwargs", {})},
            description=description,
        )

    if q.get("type") == "sql" or "query" in q:
        return QualityRule(rule_id=rule_id, type="sql", column=column,
                           params={k: q[k] for k in ("query", "mustBeLessThan") if k in q},
                           severity=severity, source="manual", description=description)

    rule = q.get("rule")
    # Collect the constraint params (everything that isn't structural)
    params = {k: v for k, v in q.items()
              if k not in ("rule", "description", "severity", "engine", "implementation", "unit")}
    if "unit" in q:
        params["unit"] = q["unit"]
    type_map = {
        "missingCount": "not_null",
        "duplicateCount": "unique",
        "rowCount": "row_count",
        "uniqueCount": "unique_count",
        "validValues": "set",
    }
    rtype = type_map.get(rule, rule or "custom")
    return QualityRule(rule_id=rule_id, type=rtype, column=column, params=params,
                       severity=severity, source="profiler", description=description)


def extract_rules(contract: dict) -> list[QualityRule]:
    """Pull every quality rule (table-level + column-level) out of an ODCS contract."""
    rules: list[QualityRule] = []
    for schema_obj in contract.get("schema", []) or []:
        for q in schema_obj.get("quality", []) or []:
            rules.append(_odcs_quality_to_rule(stable_rule_id(None, q), q, column=None))
        for prop in schema_obj.get("properties", []) or []:
            col = prop.get("name")
            for q in prop.get("quality", []) or []:
                rules.append(_odcs_quality_to_rule(stable_rule_id(col, q), q, column=col))
    return rules


# ─────────────────────────────────────────────────────────────────────────────
# ODCS → GE reverse mapper
# ─────────────────────────────────────────────────────────────────────────────

def _rule_to_ge(rule: QualityRule) -> Optional[dict]:
    """Convert one canonical QualityRule to a GE ExpectationConfiguration dict."""
    p = rule.params

    if rule.type == "engine" and rule.engine_hint:
        return {"expectation_type": rule.engine_hint.get("expectation_type"),
                "kwargs": dict(rule.engine_hint.get("kwargs", {}))}

    if rule.type == "not_null":
        kwargs: dict[str, Any] = {"column": rule.column}
        if "mustBeLessThan" in p and p.get("unit") == "percent":
            kwargs["mostly"] = round(1.0 - p["mustBeLessThan"] / 100.0, 4)
        return {"expectation_type": "expect_column_values_to_not_be_null", "kwargs": kwargs}

    if rule.type == "unique":
        kwargs = {"column": rule.column}
        if "mustBeLessThan" in p and p.get("unit") == "percent":
            kwargs["mostly"] = round(1.0 - p["mustBeLessThan"] / 100.0, 4)
        return {"expectation_type": "expect_column_values_to_be_unique", "kwargs": kwargs}

    if rule.type == "row_count":
        if "mustBe" in p:
            return {"expectation_type": "expect_table_row_count_to_equal",
                    "kwargs": {"value": p["mustBe"]}}
        if "mustBeBetween" in p:
            lo, hi = p["mustBeBetween"]
            return {"expectation_type": "expect_table_row_count_to_be_between",
                    "kwargs": {"min_value": lo, "max_value": hi}}
        if "mustBeGreaterOrEqualTo" in p:
            return {"expectation_type": "expect_table_row_count_to_be_between",
                    "kwargs": {"min_value": p["mustBeGreaterOrEqualTo"]}}

    if rule.type == "unique_count":
        if "mustBeBetween" in p:
            lo, hi = p["mustBeBetween"]
            return {"expectation_type": "expect_column_unique_value_count_to_be_between",
                    "kwargs": {"column": rule.column, "min_value": lo, "max_value": hi}}

    if rule.type == "set":
        value_set = (p.get("arguments", {}) or {}).get("validValues", p.get("value_set", []))
        return {"expectation_type": "expect_column_values_to_be_in_set",
                "kwargs": {"column": rule.column, "value_set": value_set}}

    if rule.type == "regex":
        return {"expectation_type": "expect_column_values_to_match_regex",
                "kwargs": {"column": rule.column, "regex": p.get("pattern", p.get("regex", ""))}}

    return None


def odcs_to_ge(contract: dict, suite_name: Optional[str] = None) -> dict:
    """
    Reverse mapper: build a Great Expectations ExpectationSuite (JSON dict)
    from an ODCS contract's quality rules. The inverse of mapper.py.
    """
    rules = extract_rules(contract)
    expectations = []
    for r in rules:
        ge = _rule_to_ge(r)
        if ge and ge.get("expectation_type"):
            expectations.append({
                "expectation_type": ge["expectation_type"],
                "kwargs": {k: v for k, v in ge["kwargs"].items() if v is not None},
                "meta": {"redibis_rule_id": r.rule_id, "severity": r.severity,
                         "source": r.source},
            })
    name = suite_name or f"{contract.get('table_name', 'contract')}_suite"
    return {
        "expectation_suite_name": name,
        "ge_cloud_id": None,
        "expectations": expectations,
        "data_asset_type": None,
        "meta": {"redibis": {"generated_from": "odcs_contract",
                             "contract_uuid": contract.get("contract_uuid")}},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Regenerate to any target
# ─────────────────────────────────────────────────────────────────────────────

_TARGET_ALIASES = {
    "ge": "great_expectations",
    "great_expectations": "great_expectations",
    "great-expectations": "great_expectations",
    "sodacl": "sodacl",
    "soda": "sodacl",
    "dbt": "dbt",
    "dbt-sources": "dbt",
}


def regenerate(contract: dict, target: str):
    """
    Regenerate quality artifacts from a contract for a given target.

    Returns a dict for great_expectations (ExpectationSuite JSON) or a str
    for sodacl / dbt (delegated to datacontract-cli via exporter.py).
    """
    canonical = _TARGET_ALIASES.get(target.lower())
    if canonical is None:
        raise ValueError(f"Unknown target {target!r}. "
                         f"Valid: {sorted(set(_TARGET_ALIASES))}")
    if canonical == "great_expectations":
        return odcs_to_ge(contract)

    from redibis.contracts.exporter import export_contract
    fmt = {"sodacl": "sodacl", "dbt": "dbt-sources"}[canonical]
    return export_contract(contract, fmt)
