"""Adapters for test-only tabular golden-truth JSON into the product schema."""

from __future__ import annotations

from typing import Any, Mapping

from redibis.evaluation.schema import DATASET_KIND, SCHEMA_VERSION, TableEvalError, validate_table_dataset
from redibis.pii.eval.span_metrics import current_redibis_version


def from_column_evaluations_corpus(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a generator golden-truth table file into a product dataset."""
    if payload.get("kind") == DATASET_KIND:
        return validate_table_dataset(payload)
    rows = payload.get("column_evaluations")
    if not isinstance(rows, list) or not rows:
        raise TableEvalError("column_evaluations must be a non-empty array")
    return validate_table_dataset(
        {
            "kind": DATASET_KIND,
            "schema_version": SCHEMA_VERSION,
            "redibis_version": current_redibis_version(),
            "table_name": str(payload.get("table_name") or ""),
            "columns": rows,
        }
    )
