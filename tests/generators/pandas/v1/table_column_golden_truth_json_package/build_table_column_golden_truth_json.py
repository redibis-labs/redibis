#!/usr/bin/env python3
"""
Build per-table tabular-column golden-truth JSON files.

The output follows the user's requested evaluation shape for every table column:

{
  "test_id": "TABULAR_EVAL_001",
  "modality": "tabular_column",
  "metadata": {...},
  "input_data": {
    "column_name": "contact_number",
    "sample_values": ["555-0100", "555-0101", "missing"]
  },
  "golden_truth": {
    "column_classification": {
      "expected_semantic_type": "PHONE_NUMBER",
      "is_pii": true,
      "pii_category": "Direct Identifier",
      "sensitivity_level": "HIGH"
    }
  }
}

For each table, this script writes one JSON file containing all column evaluations.
It also writes a combined all-table JSON file and a compact validation summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from eg_pii_pandas_generators import TABLE_SCHEMAS, generate_table, get_table_columns  # noqa: E402

DEFAULT_OUTPUT_DIR = ROOT / "table_column_golden_truth_json"
DEFAULT_SAMPLE_DIR = ROOT / "custom_pii_testcase_outputs" / "appended_tables"
DEFAULT_SAMPLE_SIZE = 5


def title_from_snake(value: Optional[str]) -> str:
    """Convert snake_case metadata values into human-readable title case."""
    if value is None:
        return "Non PII"
    text = str(value).strip()
    if not text:
        return "Non PII"
    return " ".join(part.capitalize() for part in text.replace("-", "_").split("_") if part)


def normalize_sensitivity(value: Optional[str]) -> str:
    """Return the sensitivity level in the uppercase style shown by the user."""
    if value is None:
        return "INTERNAL"
    text = str(value).strip()
    return text.upper() if text else "INTERNAL"


def clean_sample(value: Any) -> str:
    """Serialize a sample value safely for JSON evaluation files."""
    if value is None or pd.isna(value):
        return "missing"
    text = str(value)
    return text if text != "" else "missing"


def load_or_generate_samples(table_name: str, sample_dir: Optional[Path], sample_size: int) -> pd.DataFrame:
    """Load sample values from generated CSV outputs where available; otherwise generate synthetic samples."""
    if sample_dir:
        for filename in (f"{table_name}.csv", f"{table_name}_appended.csv", f"{table_name}_generated.csv"):
            candidate = sample_dir / filename
            if candidate.exists():
                df = pd.read_csv(candidate, dtype=str, keep_default_na=False)
                expected = get_table_columns(table_name)
                missing_cols = [col for col in expected if col not in df.columns]
                if missing_cols:
                    raise ValueError(f"Sample file {candidate} is missing expected columns: {missing_cols}")
                return df[expected].head(max(sample_size, 1)).copy()
    return generate_table(table_name, num_rows=max(sample_size, 1), seed=880001).astype("string")


def sample_values_for_column(samples: pd.DataFrame, column_name: str, sample_size: int) -> List[str]:
    """Return up to sample_size values for one column, padded with 'missing' if needed."""
    values = [clean_sample(v) for v in samples[column_name].head(sample_size).tolist()]
    while len(values) < sample_size:
        values.append("missing")
    return values


def build_column_eval(
    table_name: str,
    table_meta: Dict[str, Any],
    column_meta: Dict[str, Any],
    sample_values: List[str],
    table_index: int,
    column_index: int,
) -> Dict[str, Any]:
    """Build one column-level evaluation object matching the requested schema."""
    semantic_type = str(column_meta.get("semantic_type") or "UNKNOWN")
    is_pii = bool(column_meta.get("is_pii", False))
    pii_category = column_meta.get("pii_category") or ("non_pii" if not is_pii else "unknown")
    sensitivity = column_meta.get("sensitivity_level") or column_meta.get("classification") or "internal"

    return {
        "test_id": f"TABULAR_EVAL_{table_index:02d}_{column_index:03d}",
        "modality": "tabular_column",
        "metadata": {
            "language": "en",
            "domain": table_meta.get("domain", "customer_records"),
            "test_type": "column_classification",
            "table_name": table_name,
            "table_business_name": table_meta.get("businessName", table_name),
            "table_description": table_meta.get("description", ""),
            "column_business_name": column_meta.get("businessName", column_meta.get("name")),
            "column_description": column_meta.get("description", ""),
            "logical_type": column_meta.get("logicalType", "string"),
            "physical_type": column_meta.get("physicalType", ""),
            "required": bool(column_meta.get("required", False)),
            "primary_key": bool(column_meta.get("primaryKey", False)),
            "unique": bool(column_meta.get("unique", False)),
        },
        "input_data": {
            "column_name": column_meta["name"],
            "sample_values": sample_values,
        },
        "golden_truth": {
            "column_classification": {
                "expected_semantic_type": semantic_type,
                "is_pii": is_pii,
                "pii_category": title_from_snake(pii_category),
                "sensitivity_level": normalize_sensitivity(sensitivity),
            }
        },
    }


def build_table_file(table_name: str, table_index: int, output_dir: Path, sample_dir: Optional[Path], sample_size: int) -> Dict[str, Any]:
    """Build and write one full table golden-truth JSON file."""
    table_meta = TABLE_SCHEMAS[table_name]
    samples = load_or_generate_samples(table_name, sample_dir, sample_size)
    evaluations: List[Dict[str, Any]] = []

    for column_index, column_meta in enumerate(table_meta["columns"], start=1):
        column_name = column_meta["name"]
        evaluations.append(
            build_column_eval(
                table_name=table_name,
                table_meta=table_meta,
                column_meta=column_meta,
                sample_values=sample_values_for_column(samples, column_name, sample_size),
                table_index=table_index,
                column_index=column_index,
            )
        )

    pii_column_count = sum(1 for item in evaluations if item["golden_truth"]["column_classification"]["is_pii"])
    table_payload = {
        "table_name": table_name,
        "table_business_name": table_meta.get("businessName", table_name),
        "description": table_meta.get("description", ""),
        "domain": table_meta.get("domain", "customer_records"),
        "granularity": table_meta.get("granularity", ""),
        "modality": "tabular_column",
        "test_type": "column_classification",
        "column_count": len(evaluations),
        "pii_column_count": pii_column_count,
        "non_pii_column_count": len(evaluations) - pii_column_count,
        "column_evaluations": evaluations,
    }

    output_path = output_dir / f"{table_name}_golden_truth.json"
    output_path.write_text(json.dumps(table_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return table_payload


def run(output_dir: Path = DEFAULT_OUTPUT_DIR, sample_dir: Optional[Path] = DEFAULT_SAMPLE_DIR, sample_size: int = DEFAULT_SAMPLE_SIZE) -> Dict[str, Any]:
    """Build every per-table golden-truth JSON and a combined all-table file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tables: List[Dict[str, Any]] = []

    for table_index, table_name in enumerate(TABLE_SCHEMAS.keys(), start=1):
        tables.append(build_table_file(table_name, table_index, output_dir, sample_dir, sample_size))

    combined = {
        "dataset_name": "egyptian_market_pii_column_classification_golden_truth",
        "modality": "tabular_column",
        "test_type": "column_classification",
        "table_count": len(tables),
        "column_count": sum(table["column_count"] for table in tables),
        "pii_column_count": sum(table["pii_column_count"] for table in tables),
        "non_pii_column_count": sum(table["non_pii_column_count"] for table in tables),
        "sample_size_per_column": sample_size,
        "tables": tables,
    }
    (output_dir / "all_tables_golden_truth.json").write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

    flat_rows = []
    for table in tables:
        for item in table["column_evaluations"]:
            gt = item["golden_truth"]["column_classification"]
            flat_rows.append(
                {
                    "test_id": item["test_id"],
                    "table_name": table["table_name"],
                    "column_name": item["input_data"]["column_name"],
                    "expected_semantic_type": gt["expected_semantic_type"],
                    "is_pii": gt["is_pii"],
                    "pii_category": gt["pii_category"],
                    "sensitivity_level": gt["sensitivity_level"],
                }
            )
    pd.DataFrame(flat_rows).to_csv(output_dir / "all_columns_golden_truth_index.csv", index=False)

    summary = {
        "status": "generated",
        "output_dir": str(output_dir),
        "table_count": combined["table_count"],
        "column_count": combined["column_count"],
        "pii_column_count": combined["pii_column_count"],
        "non_pii_column_count": combined["non_pii_column_count"],
        "per_table_files": [str(output_dir / f"{table['table_name']}_golden_truth.json") for table in tables],
        "combined_file": str(output_dir / "all_tables_golden_truth.json"),
        "index_csv": str(output_dir / "all_columns_golden_truth_index.csv"),
    }
    (output_dir / "generation_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate per-table JSON golden truth for tabular column PII classification.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory for JSON outputs.")
    parser.add_argument("--sample-dir", type=Path, default=DEFAULT_SAMPLE_DIR, help="Directory containing sample table CSVs.")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE, help="Number of sample values per column.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sample_size <= 0:
        raise ValueError("--sample-size must be a positive integer")
    summary = run(output_dir=args.output_dir, sample_dir=args.sample_dir, sample_size=args.sample_size)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
