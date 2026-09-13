"""Portable, deterministic evaluation helpers for free-text PII spans."""

from .batch import assemble_batch_report, discover_eval_files, evaluate_path
from .builder import CorpusBuildError, build_dataset, build_path
from .gates import GateError, apply_gates_to_report, evaluate_gates, format_gate_failure, load_gate_file
from .report_html import render_report_html
from .runner import EvalCancelled, eval_limiter_weight, evaluate_with_service
from .span_metrics import (
    BATCH_REPORT_KIND,
    DATASET_KIND,
    OFFSET_UNIT,
    REPORT_KIND,
    SCHEMA_VERSION,
    SCHEMA_VERSION_1_2,
    DatasetValidationError,
    classify_eval_payload,
    current_redibis_version,
    evaluate_case,
    evaluate_dataset,
    validate_dataset,
)

__all__ = [
    "BATCH_REPORT_KIND",
    "DATASET_KIND",
    "OFFSET_UNIT",
    "REPORT_KIND",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_1_2",
    "CorpusBuildError",
    "DatasetValidationError",
    "EvalCancelled",
    "GateError",
    "apply_gates_to_report",
    "assemble_batch_report",
    "build_dataset",
    "build_path",
    "classify_eval_payload",
    "current_redibis_version",
    "discover_eval_files",
    "eval_limiter_weight",
    "evaluate_case",
    "evaluate_dataset",
    "evaluate_gates",
    "evaluate_path",
    "evaluate_with_service",
    "format_gate_failure",
    "load_gate_file",
    "render_report_html",
    "validate_dataset",
]
