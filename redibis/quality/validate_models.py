"""Typed results for validate-only contract quality monitoring."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class SchemaDriftResult:
    table: str
    contract_columns: list[str]
    observed_columns: list[str]
    added: list[str]
    dropped: list[str]
    type_mismatches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuleValidateResult:
    rule_id: str
    expectation_type: str
    column: Optional[str]
    success: bool
    element_count: Optional[int] = None
    unexpected_count: int = 0
    unexpected_percent: float = 0.0
    partial_unexpected: list[Any] = field(default_factory=list)
    observed_value: Any = None
    severity: str = "P1"
    source: str = "contract"
    message: str = ""
    stable_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContractQualityValidateResult:
    table: str
    run_id: str
    status: str  # success | failed | error | skipped
    rules_total: int
    rules_passed: int
    rules_failed: int
    schema_drift: Optional[SchemaDriftResult] = None
    results: list[RuleValidateResult] = field(default_factory=list)
    error: Optional[str] = None
    started_at: str = ""
    completed_at: str = ""
    artifact_keys: dict[str, str] = field(default_factory=dict)
    # How the rules ran: pandas | spark | spark-persist | spark-fused, plus counts
    # ({"fused": rules in the single aggregate job, "ge": rules run by Great
    # Expectations, "spark_jobs_fused": jobs the fused engine started, "row_count"}).
    engine: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    # Per-sink publish telemetry, e.g. {"openmetadata": {...}, "console": {...}}.
    # Populated by ContinuousQualityService after result-sink dispatch.
    sink_telemetry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.schema_drift is not None:
            payload["schema_drift"] = self.schema_drift.to_dict()
        payload["results"] = [r.to_dict() for r in self.results]
        return payload

    def to_frame(self, *, only_failed: bool = False):
        """One row per rule as a pandas DataFrame — the quickest way to read a result."""
        import pandas as pd

        rows = [{
            "expectation": r.expectation_type,
            "column": r.column,
            "severity": r.severity,
            "passed": r.success,
            "observed": r.observed_value,
            "unexpected": r.unexpected_count,
            "sample": list(r.partial_unexpected[:5]),
            "message": r.message,
        } for r in self.results if not (only_failed and r.success)]
        return pd.DataFrame(rows, columns=["expectation", "column", "severity", "passed", "observed",
                                           "unexpected", "sample", "message"])

    def __str__(self) -> str:
        drift = self.schema_drift
        changes = ([f"+{c}" for c in drift.added] + [f"-{c}" for c in drift.dropped]
                   + [str(m.get("column", m)) if isinstance(m, dict) else str(m)
                      for m in drift.type_mismatches]) if drift else []
        return (f"{self.table}: {self.status} — {self.rules_passed}/{self.rules_total} rules passed"
                + (f"; schema drift: {', '.join(changes)}" if changes else "")
                + (f" (error: {self.error})" if self.error else ""))


@dataclass
class BatchQualityValidateResult:
    run_id: str
    tables_total: int
    tables_passed: int
    tables_failed: int
    tables_skipped: int
    per_table: list[ContractQualityValidateResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tables_total": self.tables_total,
            "tables_passed": self.tables_passed,
            "tables_failed": self.tables_failed,
            "tables_skipped": self.tables_skipped,
            "per_table": [t.to_dict() for t in self.per_table],
        }
