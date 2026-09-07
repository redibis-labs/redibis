"""
redibis.services.discovery_service
===================================
Discovery service for interactive PII and quality exploration.

This is a pure business logic class — no REST, no CLI, no UI knowledge.
Portable across web, CLI, and Jupyter.

Two discovery modes:
  1. PII Discovery — test PII detection on a single value
  2. Quality Discovery — test quality rules on a data subset

All results are stored in a DiscoverySession which can be:
  - Attached to a ScanSession (web flow)
  - Used standalone (Jupyter/CLI flow)
  - Exported as test cases for Jupyter or CI

Usage (Jupyter):
    from redibis.services.discovery_service import DiscoveryService, PIIProbe, QualityProbe

    ds = DiscoveryService()

    # PII discovery
    result = ds.probe_pii(PIIProbe(
        column="phone",
        value="+201001234567",
        engines=["regex", "gliner"],
    ))
    print(result.detected, result.entity_type, result.confidence)

    # Accept/reject
    ds.accept_result(result.probe_id)
    ds.reject_result(other_result.probe_id)

    # Export accepted as test cases
    test_cases = ds.export_test_cases(format="python")
    print(test_cases)  # prints pytest code

Usage (CLI):
    redibis discover pii --column phone --value "+201001234567" --engines regex,gliner
    redibis discover quality --rule not_null --column phone --data sample.csv
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PIIProbe:
    """Input for a single PII discovery test."""
    column: str
    value: str
    engines: list[str] = field(default_factory=lambda: ["regex", "gliner"])
    confidence_threshold: float = 0.80


@dataclass
class PIIProbeResult:
    """Result of a single PII discovery test."""
    probe_id: str = ""
    column: str = ""
    value: str = ""
    engines_used: list[str] = field(default_factory=list)
    detected: bool = False
    entity_type: str = ""
    confidence: float = 0.0
    engine_scores: dict = field(default_factory=dict)
    pattern_matched: str = ""
    timestamp: str = ""
    status: str = "pending"  # pending | accepted | rejected

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class QualityProbe:
    """Input for a single quality rule test."""
    rule: str  # e.g. "not_null", "regex", "in_set", "sql"
    column: Optional[str] = None
    kwargs: dict = field(default_factory=dict)  # rule-specific params
    sql: Optional[str] = None  # for custom SQL rules
    subset_rows: Optional[int] = None  # use subset of data


@dataclass
class QualityProbeResult:
    """Result of a single quality rule test."""
    probe_id: str = ""
    rule: str = ""
    column: str = ""
    success: bool = False
    element_count: int = 0
    unexpected_count: int = 0
    unexpected_percent: float = 0.0
    partial_unexpected: list = field(default_factory=list)
    observed_value: Any = None
    timestamp: str = ""
    status: str = "pending"  # pending | accepted | rejected

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class DiscoveryRun:
    """
    One scoped discovery scan — PII *or* quality (never both), run over a
    subset of the real data (one column, several, or all). Unlike a scan run,
    a discovery run writes NO contracts; it just produces findings for the
    discovery page. It carries its own ``run_id`` and lives inside the
    session's DiscoverySession.
    """
    run_id: str
    kind: str                                   # "pii" | "quality"
    columns: Optional[List[str]] = None         # None / [] = all columns
    subset_rows: Optional[int] = None           # None = full data
    status: str = "running"                     # running | complete | error
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    error: Optional[str] = None
    config_snapshot: Dict[str, Any] = field(default_factory=dict)
    results: List[dict] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    def finish(self, status: str = "complete", error: Optional[str] = None) -> None:
        self.status = status
        self.completed_at = datetime.now(timezone.utc).isoformat()
        if error:
            self.error = error

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "kind": self.kind, "columns": self.columns,
            "subset_rows": self.subset_rows, "status": self.status,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "error": self.error, "config_snapshot": self.config_snapshot,
            "results": self.results, "summary": self.summary,
        }


@dataclass
class DiscoverySession:
    """
    Holds all discovery exploration results.

    Separate from ScanSession — discovery is for interactive exploration,
    scan is for full pipeline runs. A DiscoverySession can be attached
    to a ScanSession or used standalone.

    Two complementary surfaces:
      - ``runs``           : scoped DiscoveryRun objects (PII or quality over
                             real data columns) — the primary web/CLI surface.
      - ``pii_probes`` /
        ``quality_probes`` : single-value / single-rule probes for ad-hoc
                             Jupyter exploration and test-case export.
    """
    discovery_id: str = ""
    runs: list[DiscoveryRun] = field(default_factory=list)
    pii_probes: list[PIIProbeResult] = field(default_factory=list)
    quality_probes: list[QualityProbeResult] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self):
        if not self.discovery_id:
            self.discovery_id = str(uuid.uuid4())[:8]
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def get_run(self, run_id: str) -> Optional[DiscoveryRun]:
        return next((r for r in self.runs if r.run_id == run_id), None)

    def to_dict(self) -> dict:
        return {
            "discovery_id": self.discovery_id,
            "runs": [r.to_dict() for r in self.runs],
            "pii_probes": [p.to_dict() for p in self.pii_probes],
            "quality_probes": [q.to_dict() for q in self.quality_probes],
            "created_at": self.created_at,
            "run_count": len(self.runs),
            "pii_run_count": len([r for r in self.runs if r.kind == "pii"]),
            "quality_run_count": len([r for r in self.runs if r.kind == "quality"]),
            "pii_accepted": len([p for p in self.pii_probes if p.status == "accepted"]),
            "pii_rejected": len([p for p in self.pii_probes if p.status == "rejected"]),
            "pii_pending": len([p for p in self.pii_probes if p.status == "pending"]),
            "quality_accepted": len([q for q in self.quality_probes if q.status == "accepted"]),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Discovery Service
# ─────────────────────────────────────────────────────────────────────────────

class DiscoveryService:
    """
    Interactive PII and quality exploration.

    Portable: works the same in web, CLI, and Jupyter.
    """

    def __init__(self):
        self.session = DiscoverySession()

    # ── PII Discovery ─────────────────────────────────────────────────────

    def probe_pii(self, probe: PIIProbe) -> PIIProbeResult:
        """
        Test PII detection on a single column + value.

        Runs the specified engines against the value and returns
        the detection result without modifying any scan state.
        """
        probe_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now(timezone.utc).isoformat()

        result = PIIProbeResult(
            probe_id=probe_id,
            column=probe.column,
            value=probe.value,
            engines_used=probe.engines,
            timestamp=timestamp,
        )

        # Create a single-row DataFrame for the detector
        df = pd.DataFrame({probe.column: [probe.value]})

        engine_scores = {}

        # Regex engine
        if "regex" in probe.engines:
            try:
                from redibis.pii.presidio_nlp import build_pattern_analyzer_engine
                from redibis.pii.recognizer_factory import build_recognizers
                from presidio_analyzer import RecognizerRegistry

                recognizers = build_recognizers(
                    group="structured", arabic=False
                )
                registry = RecognizerRegistry()
                for rec in recognizers:
                    registry.add_recognizer(rec)
                engine = build_pattern_analyzer_engine(registry, supported_languages=["en"])
                findings = engine.analyze(text=probe.value, language="en")
                if findings:
                    best = max(findings, key=lambda f: f.score)
                    engine_scores["regex"] = {
                        "score": best.score,
                        "entity_type": best.entity_type,
                        "start": best.start,
                        "end": best.end,
                    }
                    if best.score >= probe.confidence_threshold:
                        result.detected = True
                        result.entity_type = best.entity_type
                        result.confidence = best.score
                        result.pattern_matched = "presidio_regex"
                else:
                    engine_scores["regex"] = {"score": 0, "entity_type": None}
            except ImportError:
                engine_scores["regex"] = {"error": "presidio not installed"}
            except Exception as e:
                engine_scores["regex"] = {"error": str(e)}

        # NER engine (gliner backend; "gliner" and "ner" are aliases)
        if "gliner" in probe.engines or "ner" in probe.engines:
            try:
                from redibis.config import NERConfig
                from redibis.pii.ner_registry import NERModelRegistry

                backend = NERModelRegistry.try_load(ner=NERConfig())
                if backend is None:
                    engine_scores["gliner"] = {"error": "ner model not configured"}
                else:
                    ner_result = backend.score_values([probe.value], probe.column)
                    gliner_result = {
                        "score": ner_result.get("score") or 0,
                        "entity_type": (ner_result.get("label") or "UNKNOWN").upper().replace(" ", "_"),
                        "label": ner_result.get("label"),
                        "match_rate": ner_result.get("match_rate"),
                    }
                    engine_scores["gliner"] = gliner_result
                    if gliner_result.get("score", 0) >= probe.confidence_threshold:
                        if not result.detected:
                            result.detected = True
                            result.entity_type = gliner_result.get("entity_type", "UNKNOWN")
                            result.confidence = gliner_result.get("score", 0)
            except ImportError:
                engine_scores["gliner"] = {"error": "gliner not available"}
            except Exception as e:
                engine_scores["gliner"] = {"error": str(e)}

        # LLM engine
        if "llm" in probe.engines:
            try:
                from redibis.pii.llm_refiner import refine_with_llm
                llm_result = refine_with_llm(
                    column=probe.column,
                    sample_values=[probe.value],
                    current_entity=result.entity_type or "UNKNOWN",
                    current_confidence=result.confidence,
                )
                engine_scores["llm"] = llm_result
                if llm_result.get("verdict") == "PII":
                    result.detected = True
                    result.confidence = max(
                        result.confidence,
                        llm_result.get("confidence", 0),
                    )
            except (ImportError, AttributeError):
                engine_scores["llm"] = {"error": "llm not available"}
            except Exception as e:
                engine_scores["llm"] = {"error": str(e)}

        result.engine_scores = engine_scores
        self.session.pii_probes.append(result)
        return result

    # ── Quality Discovery ─────────────────────────────────────────────────

    def probe_quality(
        self,
        probe: QualityProbe,
        df: pd.DataFrame,
    ) -> QualityProbeResult:
        """
        Test a quality rule against a DataFrame (or subset).

        Runs the rule immediately and returns the result without
        modifying any scan state.
        """
        probe_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now(timezone.utc).isoformat()

        # Use subset if requested
        if probe.subset_rows and len(df) > probe.subset_rows:
            df = df.sample(n=probe.subset_rows, random_state=42)

        result = QualityProbeResult(
            probe_id=probe_id,
            rule=probe.rule,
            column=probe.column or "Table-Level",
            element_count=len(df),
            timestamp=timestamp,
        )

        try:
            if probe.sql:
                # Custom SQL via DuckDB
                result = self._run_sql_probe(probe, df, result)
            else:
                # GE expectation
                result = self._run_ge_probe(probe, df, result)
        except Exception as e:
            log.warning(f"Quality probe failed: {e}")
            result.success = False
            result.observed_value = f"Error: {e}"

        self.session.quality_probes.append(result)
        return result

    def _run_ge_probe(
        self, probe: QualityProbe, df: pd.DataFrame, result: QualityProbeResult
    ) -> QualityProbeResult:
        """Run a single GE expectation and return the result."""
        from redibis.quality.gatekeeper import QualityGatekeeper

        qa = QualityGatekeeper(
            suite_name="discovery_probe", in_memory=True
        )
        qa.attach_dataframe(df, dataset_name="discovery_data")

        # Support both short aliases and full GE expectation names
        rule_map = {
            # Short aliases for convenience
            "not_null":  "expect_column_values_to_not_be_null",
            "unique":    "expect_column_values_to_be_unique",
            "in_set":    "expect_column_values_to_be_in_set",
            "regex":     "expect_column_values_to_match_regex",
            "between":   "expect_column_values_to_be_between",
            "row_count": "expect_table_row_count_to_be_between",
            "not_in_set": "expect_column_values_to_not_be_in_set",
            "length_between": "expect_column_value_lengths_to_be_between",
            "type": "expect_column_values_to_be_of_type",
            "increasing": "expect_column_values_to_be_increasing",
            "decreasing": "expect_column_values_to_be_decreasing",
        }
        # If full GE name already given (starts with expect_), use it directly
        ge_name = rule_map.get(probe.rule, probe.rule)

        kwargs = dict(probe.kwargs)
        if probe.column:
            kwargs["column"] = probe.column

        try:
            raw = qa.evaluate(ge_name, **kwargs)
        except Exception as e:
            # Return a helpful error if kwargs are missing
            result.success = False
            result.observed_value = f"Error: {e} — check required parameters for {ge_name}"
            return result

        result.success = raw.get("success", False)
        r = raw.get("result", {})
        result.unexpected_count = r.get("unexpected_count", 0)
        result.unexpected_percent = r.get("unexpected_percent", 0)
        result.partial_unexpected = r.get("partial_unexpected_list", [])[:5]
        result.observed_value = r.get("observed_value")
        return result

    def _run_sql_probe(
        self, probe: QualityProbe, df: pd.DataFrame, result: QualityProbeResult
    ) -> QualityProbeResult:
        """Run a custom SQL rule via DuckDB."""
        try:
            import duckdb
            con = duckdb.connect()
            con.register("data", df)
            violations = con.execute(probe.sql).fetchdf()
            result.unexpected_count = len(violations)
            result.unexpected_percent = (
                (len(violations) / len(df) * 100) if len(df) > 0 else 0
            )
            result.success = result.unexpected_count == 0
            result.partial_unexpected = (
                violations.head(5).to_dict("records") if len(violations) > 0 else []
            )
        except ImportError:
            result.success = False
            result.observed_value = "duckdb not installed"
        return result

    # ── Accept / Reject ───────────────────────────────────────────────────

    def accept_pii_result(self, probe_id: str) -> bool:
        for p in self.session.pii_probes:
            if p.probe_id == probe_id:
                p.status = "accepted"
                return True
        return False

    def reject_pii_result(self, probe_id: str) -> bool:
        for p in self.session.pii_probes:
            if p.probe_id == probe_id:
                p.status = "rejected"
                return True
        return False

    def accept_quality_result(self, probe_id: str) -> bool:
        for q in self.session.quality_probes:
            if q.probe_id == probe_id:
                q.status = "accepted"
                return True
        return False

    # ── Export ────────────────────────────────────────────────────────────

    def export_test_cases(self, format: str = "python") -> str:
        """
        Export accepted probe results as test cases.

        Formats:
            python  — pytest test functions
            yaml    — YAML test case definitions
            json    — JSON array of test cases
        """
        accepted_pii = [p for p in self.session.pii_probes if p.status == "accepted"]
        accepted_quality = [q for q in self.session.quality_probes if q.status == "accepted"]

        if format == "python":
            return self._export_python(accepted_pii, accepted_quality)
        elif format == "yaml":
            return self._export_yaml(accepted_pii, accepted_quality)
        elif format == "json":
            import json
            return json.dumps({
                "pii_tests": [p.to_dict() for p in accepted_pii],
                "quality_tests": [q.to_dict() for q in accepted_quality],
            }, indent=2, default=str)
        else:
            raise ValueError(f"Unknown format: {format}")

    def _export_python(self, pii: list, quality: list) -> str:
        lines = [
            '"""Auto-generated test cases from redibis discovery session."""',
            "import pytest",
            "import pandas as pd",
            "from redibis.services.discovery_service import DiscoveryService, PIIProbe, QualityProbe",
            "",
            "",
            "ds = DiscoveryService()",
            "",
        ]
        for i, p in enumerate(pii):
            lines.extend([
                f"def test_pii_{i}_{p.column}():",
                f'    result = ds.probe_pii(PIIProbe(',
                f'        column="{p.column}",',
                f'        value="{p.value}",',
                f'        engines={p.engines_used},',
                f"    ))",
                f"    assert result.detected == {p.detected}",
                f'    assert result.entity_type == "{p.entity_type}"',
                "",
            ])
        for i, q in enumerate(quality):
            lines.extend([
                f"def test_quality_{i}_{q.rule}():",
                f"    df = pd.read_csv('test_data.csv')",
                f'    result = ds.probe_quality(QualityProbe(',
                f'        rule="{q.rule}",',
                f'        column="{q.column}",',
                f"    ), df=df)",
                f"    assert result.success == {q.success}",
                "",
            ])
        return "\n".join(lines)

    def _export_yaml(self, pii: list, quality: list) -> str:
        import yaml
        data = {
            "pii_tests": [
                {"column": p.column, "value": p.value, "engines": p.engines_used,
                 "expected_detected": p.detected, "expected_entity": p.entity_type}
                for p in pii
            ],
            "quality_tests": [
                {"rule": q.rule, "column": q.column,
                 "expected_success": q.success}
                for q in quality
            ],
        }
        return yaml.safe_dump(data, default_flow_style=False, sort_keys=False)

    # ── Session management ────────────────────────────────────────────────

    def get_session(self) -> DiscoverySession:
        return self.session

    def reset(self):
        self.session = DiscoverySession()
