"""Structured, value-free column evaluation (engine / contract / LLM proposal)."""

from .adapters import from_column_evaluations_corpus
from .report_html import render_table_report_html
from .schema import (
    DATASET_KIND,
    REPORT_KIND,
    SCHEMA_VERSION,
    TableEvalError,
    classify_table_eval_payload,
    structural_fingerprint,
    validate_table_dataset,
)
from .scoring import evaluate_table, score_column
from .service import TableEvalCancelled, run_table_evaluation, scaffold_from_frame

__all__ = [
    "DATASET_KIND",
    "REPORT_KIND",
    "SCHEMA_VERSION",
    "TableEvalCancelled",
    "TableEvalError",
    "classify_table_eval_payload",
    "evaluate_table",
    "from_column_evaluations_corpus",
    "render_table_report_html",
    "run_table_evaluation",
    "scaffold_from_frame",
    "score_column",
    "structural_fingerprint",
    "validate_table_dataset",
]
