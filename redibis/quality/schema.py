"""
redibis.quality.schema — versioned, backend-neutral quality interchange.

No single open standard currently covers both portable quality-rule
*definitions* and complete execution *results* across engines (Great
Expectations, Soda, dbt) and catalogs (OpenMetadata, DataHub, Atlas). This
module is Redibis' own small, versioned envelope for that gap:

  - ``QualityRuleSetV1`` — a table's quality rules, in a portable
    metric/predicate shape wherever ODCS 3.1's metric vocabulary applies,
    with a full engine binding retained for anything it doesn't cover.
  - ``QualityRunV1``     — one execution's outcome, keyed back to the rule
    set that produced it, with privacy-safe diagnostics by default.

Both are JSON-serializable envelopes (``apiVersion`` / ``kind``, k8s/ODCS/
OpenLineage-style) with published JSON Schemas under ``schemas/quality/``.

This module does **not** replace ODCS — ``redibis.contracts.rules`` remains
the contract's source of truth. It sits *between* the contract and any
execution engine or result sink, so a future Soda/DataHub/OpenLineage
adapter has one stable shape to read instead of engine- or catalog-specific
JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

QUALITY_API_VERSION = "redibis.io/quality/v1alpha1"
RULESET_KIND = "QualityRuleSet"
RUN_KIND = "QualityRun"

# Portable metric names — the subset that lines up with ODCS 3.1's data
# quality metric vocabulary (nullValues | missingValues | invalidValues |
# duplicateValues | rowCount). Anything outside this table keeps only its
# engine binding (``metric_fidelity="unsupported"``).
_TYPE_TO_METRIC: dict[str, str] = {
    "not_null": "nullValues",
    "unique": "duplicateValues",
    "row_count": "rowCount",
    "unique_count": "distinctValueCount",  # Redibis extension, not core ODCS
    "set": "invalidValues",
    "regex": "invalidValues",
}
# ODCS-3.1-shaped comparator keys already used verbatim in contract params.
_COMPARATOR_KEYS: tuple[str, ...] = (
    "mustBe",
    "mustNotBe",
    "mustBeLessThan",
    "mustBeGreaterThan",
    "mustBeLessOrEqualTo",
    "mustBeGreaterOrEqualTo",
    "mustBeBetween",
)


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _sha256_json(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────
# Shared reference types
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class AssetRef:
    """The dataset a rule set / run applies to."""

    namespace: str = ""
    name: str = ""
    physical_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AssetRef":
        data = data or {}
        return cls(
            namespace=str(data.get("namespace") or ""),
            name=str(data.get("name") or ""),
            physical_name=str(data.get("physical_name") or ""),
        )

    @classmethod
    def for_table(cls, table: str) -> "AssetRef":
        ns, _, name = str(table or "").rpartition(".")
        return cls(namespace=ns, name=name or table, physical_name=table)


@dataclass
class ContractRef:
    """Which ODCS contract (and version) this rule set was derived from."""

    contract_id: str = ""
    version: str = ""
    api_version: str = "odcs-v3"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ContractRef":
        data = data or {}
        return cls(
            contract_id=str(data.get("contract_id") or ""),
            version=str(data.get("version") or ""),
            api_version=str(data.get("api_version") or "odcs-v3"),
        )

    @classmethod
    def for_contract(cls, contract: Optional[dict]) -> "ContractRef":
        contract = contract or {}
        return cls(
            contract_id=str(contract.get("contract_uuid") or ""),
            version=str(contract.get("version") or ""),
        )


@dataclass
class EngineBinding:
    """The exact engine-native rule payload (never lossy by construction)."""

    engine: str
    definition: dict[str, Any] = field(default_factory=dict)
    fidelity: str = "lossless"  # lossless | lossy | unsupported

    def to_dict(self) -> dict[str, Any]:
        return {"engine": self.engine, "definition": dict(self.definition), "fidelity": self.fidelity}

    @classmethod
    def from_dict(cls, data: dict) -> "EngineBinding":
        return cls(
            engine=str(data.get("engine") or ""),
            definition=dict(data.get("definition") or {}),
            fidelity=str(data.get("fidelity") or "lossless"),
        )


# ─────────────────────────────────────────────────────────────────────────
# QualityRuleSetV1
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class QualityRuleV1:
    id: str
    scope: str  # "column" | "dataset"
    column: Optional[str] = None
    metric_namespace: Optional[str] = None
    metric: Optional[str] = None
    operator: Optional[str] = None
    expected: Any = None
    unit: Optional[str] = None
    metric_fidelity: str = "unsupported"  # lossless | lossy | unsupported
    severity: str = "P1"
    description: str = ""
    dimension: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    stable_id: str = ""  # execution-binding identity (engine-shape hash)
    bindings: list[EngineBinding] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["bindings"] = [b.to_dict() for b in self.bindings]
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "QualityRuleV1":
        data = dict(data or {})
        bindings = [EngineBinding.from_dict(b) for b in data.pop("bindings", []) or []]
        return cls(bindings=bindings, **{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class QualityRuleSetV1:
    api_version: str = QUALITY_API_VERSION
    kind: str = RULESET_KIND
    rule_set_id: str = ""
    version: str = "1"
    semantic_digest: str = ""
    contract: ContractRef = field(default_factory=ContractRef)
    asset: AssetRef = field(default_factory=AssetRef)
    rules: list[QualityRuleV1] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def compute_digest(self) -> str:
        return _sha256_json([r.to_dict() for r in self.rules])[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "rule_set_id": self.rule_set_id,
            "version": self.version,
            "semantic_digest": self.semantic_digest,
            "contract": self.contract.to_dict(),
            "asset": self.asset.to_dict(),
            "rules": [r.to_dict() for r in self.rules],
            "extensions": dict(self.extensions),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QualityRuleSetV1":
        data = data or {}
        return cls(
            api_version=str(data.get("apiVersion") or QUALITY_API_VERSION),
            kind=str(data.get("kind") or RULESET_KIND),
            rule_set_id=str(data.get("rule_set_id") or ""),
            version=str(data.get("version") or "1"),
            semantic_digest=str(data.get("semantic_digest") or ""),
            contract=ContractRef.from_dict(data.get("contract")),
            asset=AssetRef.from_dict(data.get("asset")),
            rules=[QualityRuleV1.from_dict(r) for r in data.get("rules") or []],
            extensions=dict(data.get("extensions") or {}),
        )


def _extract_comparator(params: dict) -> tuple[Optional[str], Any, Optional[str]]:
    for key in _COMPARATOR_KEYS:
        if key in params:
            return key, params[key], params.get("unit")
    return None, None, params.get("unit")


def _metric_for_contract_rule(rule_type: str, params: dict) -> tuple[Optional[str], Optional[str], Any, Optional[str], str]:
    """Return ``(metric, operator, expected, unit, fidelity)`` for a contract ``QualityRule``."""
    metric = _TYPE_TO_METRIC.get(rule_type)
    if metric is None:
        return None, None, None, None, "unsupported"
    operator, expected, unit = _extract_comparator(params)
    if operator is None:
        return metric, None, None, unit, "unsupported"
    # Core ODCS metrics keep exact fidelity; Redibis extensions are lossy
    # (portable shape, but not a name any ODCS 3.1 consumer recognizes yet).
    fidelity = "lossless" if rule_type != "unique_count" else "lossy"
    return metric, operator, expected, unit, fidelity


def rule_set_from_contract(contract: dict, *, table: str, version: str = "1") -> QualityRuleSetV1:
    """Build the canonical, versioned rule set straight from an ODCS contract.

    Uses ``redibis.contracts.rules.extract_rules`` (the contract's own
    portable rule vocabulary) rather than the GE-shaped ``QualityRuleSet``,
    so metric/operator/expected come from the contract's own comparator
    fields instead of being re-guessed from a Great Expectations kwargs dict.
    """
    from redibis.contracts.rules import _rule_to_ge, extract_rules
    from redibis.quality.rule_identity import quality_stable_rule_id

    rules_v1: list[QualityRuleV1] = []
    for qr in extract_rules(contract):
        metric, operator, expected, unit, fidelity = _metric_for_contract_rule(qr.type, qr.params)
        ge = _rule_to_ge(qr)
        bindings: list[EngineBinding] = []
        stable_id = ""
        if ge and ge.get("expectation_type"):
            bindings.append(EngineBinding(engine="great_expectations", definition=ge, fidelity="lossless"))
            stable_id = quality_stable_rule_id(ge["expectation_type"], qr.column, ge.get("kwargs") or {})
        rules_v1.append(
            QualityRuleV1(
                id=qr.rule_id,
                scope="dataset" if qr.column is None else "column",
                column=qr.column,
                metric_namespace="redibis.quality" if metric else None,
                metric=metric,
                operator=operator,
                expected=expected,
                unit=unit,
                metric_fidelity=fidelity,
                severity=qr.severity,
                description=qr.description or "",
                stable_id=stable_id,
                bindings=bindings,
                extensions={"source": qr.source, "contract_type": qr.type},
            )
        )

    rule_set = QualityRuleSetV1(
        rule_set_id=table,
        version=version,
        contract=ContractRef.for_contract(contract),
        asset=AssetRef.for_table(table),
        rules=rules_v1,
    )
    rule_set.semantic_digest = rule_set.compute_digest()
    return rule_set


def rule_set_from_ge_rules(
    rules: Sequence[dict[str, Any]],
    *,
    table: str,
    version: str = "1",
    source: str = "session_draft",
) -> QualityRuleSetV1:
    """Build the canonical rule set from raw GE-shaped rule dicts.

    Used when there is no full ODCS contract to derive the portable
    metric/predicate from (e.g. a web review-page's curated, not-yet-merged
    draft rules) — every rule keeps its exact engine binding
    (``fidelity="lossless"``) but ``metric_fidelity`` is ``"unsupported"``
    since the original contract comparator (mustBe/mustBeBetween/...) isn't
    available at this stage, only the Great Expectations kwargs.
    """
    from redibis.quality.rule_identity import quality_stable_rule_id

    rules_v1: list[QualityRuleV1] = []
    for r in rules:
        etype = str(r.get("rule") or r.get("expectation_type") or "")
        column = r.get("column")
        kwargs = dict(r.get("kwargs") or {})
        meta = dict(r.get("meta") or {})
        rule_id = str(meta.get("redibis_rule_id") or quality_stable_rule_id(etype, column, kwargs))
        stable_id = quality_stable_rule_id(etype, column, kwargs)
        rules_v1.append(
            QualityRuleV1(
                id=rule_id,
                scope="dataset" if column is None else "column",
                column=column,
                metric_fidelity="unsupported",
                severity=str(meta.get("severity") or "P1"),
                description=str(r.get("description") or ""),
                stable_id=stable_id,
                bindings=[EngineBinding(engine="great_expectations", definition={"expectation_type": etype, "kwargs": kwargs}, fidelity="lossless")],
                extensions={"source": meta.get("source", source)},
            )
        )
    rule_set = QualityRuleSetV1(
        rule_set_id=table,
        version=version,
        asset=AssetRef.for_table(table),
        rules=rules_v1,
    )
    rule_set.semantic_digest = rule_set.compute_digest()
    return rule_set


def rule_set_to_quality_rule_set(rule_set: QualityRuleSetV1):
    """Reconstruct a GE-shaped ``QualityRuleSet`` from the canonical rules.

    Used to feed an executable engine (currently Great Expectations) from
    the canonical, portable rule set instead of re-reading the contract.
    """
    from redibis.quality.rule_set import QualityRuleSet

    rules: list[dict[str, Any]] = []
    for r in rule_set.rules:
        binding = next((b for b in r.bindings if b.engine == "great_expectations"), None)
        if binding is None:
            continue
        definition = binding.definition
        kwargs = dict(definition.get("kwargs") or {})
        rules.append({
            "rule": definition.get("expectation_type"),
            "column": r.column,
            "kwargs": kwargs,
            "meta": {"redibis_rule_id": r.id, "severity": r.severity, "source": r.extensions.get("source", "contract")},
        })
    return QualityRuleSet(name=rule_set.rule_set_id, rules=rules)


# ─────────────────────────────────────────────────────────────────────────
# QualityRunV1
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class RuleSetRef:
    rule_set_id: str = ""
    version: str = ""
    digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RuleSetRef":
        data = data or {}
        return cls(
            rule_set_id=str(data.get("rule_set_id") or ""),
            version=str(data.get("version") or ""),
            digest=str(data.get("digest") or ""),
        )

    @classmethod
    def from_rule_set(cls, rule_set: QualityRuleSetV1) -> "RuleSetRef":
        return cls(rule_set_id=rule_set.rule_set_id, version=rule_set.version, digest=rule_set.semantic_digest)


@dataclass
class EngineInfo:
    name: str = "great_expectations"
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EngineInfo":
        data = data or {}
        return cls(name=str(data.get("name") or "great_expectations"), version=str(data.get("version") or ""))


@dataclass
class QualityResultV1:
    rule_id: str
    stable_id: str = ""
    column: Optional[str] = None
    outcome: str = "error"  # pass | fail | error | skip
    severity: str = "P1"
    metric: Optional[str] = None
    expected: Any = None
    observed: Any = None
    evaluated_count: Optional[int] = None
    passed_count: Optional[int] = None
    failed_count: int = 0
    null_count: Optional[int] = None
    message: str = ""
    duration_ms: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "QualityResultV1":
        data = dict(data or {})
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class QualityRunV1:
    api_version: str = QUALITY_API_VERSION
    kind: str = RUN_KIND
    run_id: str = ""
    rule_set_ref: RuleSetRef = field(default_factory=RuleSetRef)
    asset: AssetRef = field(default_factory=AssetRef)
    contract: ContractRef = field(default_factory=ContractRef)
    engine: EngineInfo = field(default_factory=EngineInfo)
    status: str = "error"  # success | failed | error | skipped
    started_at: str = ""
    completed_at: str = ""
    batch: dict[str, Any] = field(default_factory=dict)
    results: list[QualityResultV1] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    schema_drift: Optional[dict[str, Any]] = None
    provenance: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "run_id": self.run_id,
            "rule_set_ref": self.rule_set_ref.to_dict(),
            "asset": self.asset.to_dict(),
            "contract": self.contract.to_dict(),
            "engine": self.engine.to_dict(),
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "batch": dict(self.batch),
            "results": [r.to_dict() for r in self.results],
            "summary": dict(self.summary),
            "schema_drift": self.schema_drift,
            "provenance": dict(self.provenance),
            "artifacts": dict(self.artifacts),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QualityRunV1":
        data = data or {}
        return cls(
            api_version=str(data.get("apiVersion") or QUALITY_API_VERSION),
            kind=str(data.get("kind") or RUN_KIND),
            run_id=str(data.get("run_id") or ""),
            rule_set_ref=RuleSetRef.from_dict(data.get("rule_set_ref")),
            asset=AssetRef.from_dict(data.get("asset")),
            contract=ContractRef.from_dict(data.get("contract")),
            engine=EngineInfo.from_dict(data.get("engine")),
            status=str(data.get("status") or "error"),
            started_at=str(data.get("started_at") or ""),
            completed_at=str(data.get("completed_at") or ""),
            batch=dict(data.get("batch") or {}),
            results=[QualityResultV1.from_dict(r) for r in data.get("results") or []],
            summary=dict(data.get("summary") or {}),
            schema_drift=data.get("schema_drift"),
            provenance=dict(data.get("provenance") or {}),
            artifacts=dict(data.get("artifacts") or {}),
        )


def quality_run_from_validate_result(
    result: Any,
    *,
    table: str,
    rule_set_ref: Optional[RuleSetRef] = None,
    contract: Optional[dict] = None,
    engine_name: str = "great_expectations",
    engine_version: str = "",
    batch: Optional[dict] = None,
    include_raw_diagnostics: bool = False,
) -> QualityRunV1:
    """Adapt a ``ContractQualityValidateResult`` (or an equivalent dict payload,
    e.g. a legacy ``quality_results.json``) into the canonical ``QualityRunV1``.

    Raw unexpected values are excluded by default (``include_raw_diagnostics``)
    so the canonical artifact never carries source-data PII by accident.
    """
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})

    results_v1: list[QualityResultV1] = []
    for row in payload.get("results") or []:
        success = bool(row.get("success"))
        outcome = "pass" if success else "fail"
        results_v1.append(
            QualityResultV1(
                rule_id=str(row.get("rule_id") or ""),
                stable_id=str(row.get("stable_id") or ""),
                column=row.get("column"),
                outcome=outcome,
                severity=str(row.get("severity") or "P1"),
                observed=row.get("observed_value") if include_raw_diagnostics else None,
                evaluated_count=row.get("element_count"),
                failed_count=int(row.get("unexpected_count") or 0),
                message=str(row.get("message") or ""),
            )
        )

    return QualityRunV1(
        run_id=str(payload.get("run_id") or ""),
        rule_set_ref=rule_set_ref or RuleSetRef(rule_set_id=table),
        asset=AssetRef.for_table(table),
        contract=ContractRef.for_contract(contract),
        engine=EngineInfo(name=engine_name, version=engine_version),
        status=str(payload.get("status") or "error"),
        started_at=str(payload.get("started_at") or ""),
        completed_at=str(payload.get("completed_at") or ""),
        batch=dict(batch or {}),
        results=results_v1,
        summary={
            "total": int(payload.get("rules_total") or 0),
            "passed": int(payload.get("rules_passed") or 0),
            "failed": int(payload.get("rules_failed") or 0),
        },
        schema_drift=payload.get("schema_drift"),
        provenance={"error": payload["error"]} if payload.get("error") else {},
        artifacts=dict(payload.get("artifact_keys") or {}),
    )


__all__ = [
    "QUALITY_API_VERSION",
    "RULESET_KIND",
    "RUN_KIND",
    "AssetRef",
    "ContractRef",
    "EngineBinding",
    "QualityRuleV1",
    "QualityRuleSetV1",
    "RuleSetRef",
    "EngineInfo",
    "QualityResultV1",
    "QualityRunV1",
    "rule_set_from_contract",
    "rule_set_from_ge_rules",
    "rule_set_to_quality_rule_set",
    "quality_run_from_validate_result",
]
