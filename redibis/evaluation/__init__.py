"""Structured, value-free column evaluation (engine / contract / LLM proposal)."""

from .adapters import from_column_evaluations_corpus
from .from_contract import (
    DEFAULT_SAMPLE_COUNT,
    SAMPLE_MODES,
    dataset_from_contract,
    normalize_tags,
    sample_values_from_frame,
)
from .report_html import render_table_report_html
from .schema import (
    DATASET_KIND,
    REPORT_KIND,
    SCHEMA_VERSION,
    SCHEMA_VERSION_SAMPLES,
    SUPPORTED_SCHEMA_VERSIONS,
    TableEvalError,
    classify_table_eval_payload,
    structural_fingerprint,
    validate_table_dataset,
)
from .scoring import evaluate_table, score_column
from .service import TableEvalCancelled, run_table_evaluation, scaffold_from_frame

__all__ = [
    "DATASET_KIND",
    "DEFAULT_SAMPLE_COUNT",
    "REPORT_KIND",
    "SAMPLE_MODES",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_SAMPLES",
    "SUPPORTED_SCHEMA_VERSIONS",
    "dataset_from_contract",
    "normalize_tags",
    "sample_values_from_frame",
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
