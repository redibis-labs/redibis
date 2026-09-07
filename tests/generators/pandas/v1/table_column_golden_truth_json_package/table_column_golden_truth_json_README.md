# Table Column Golden-Truth JSON Files

This deliverable contains one full JSON golden-truth file for every generated table. Each table file contains one `column_evaluations` object per column, and every object follows the requested tabular-column evaluation structure with `test_id`, `modality`, `metadata`, `input_data`, and `golden_truth`.

The generated JSON files use the existing table schema metadata as the source of truth for semantic type, PII status, PII category, and sensitivity level. Sample values are drawn from the current appended table CSV outputs, so the JSON can be used directly for PII engine validation and regression testing.

| Output | Purpose |
|---|---|
| `table_column_golden_truth_json/*_golden_truth.json` | One JSON file per table, containing all column-level evaluation objects. |
| `table_column_golden_truth_json/all_tables_golden_truth.json` | Combined JSON containing all 14 tables and all 147 column evaluations. |
| `table_column_golden_truth_json/all_columns_golden_truth_index.csv` | Flat CSV index of every table-column expected label. |
| `table_column_golden_truth_json/generation_summary.json` | Generation coverage summary. |
| `table_column_golden_truth_json/validation_result.json` | Validation results proving the files conform to the requested structure. |
| `build_table_column_golden_truth_json.py` | Re-runnable generator script. |
| `validate_table_column_golden_truth_json.py` | Re-runnable validation script. |

## Coverage

The current generated output covers **14 tables**, **147 columns**, **123 PII columns**, and **24 non-PII columns**. The validator confirms that every per-table JSON file includes all expected table columns, every column object has the requested `golden_truth.column_classification` fields, and the combined file totals match the per-table files.

## Example object shape

Each column evaluation follows this shape:

```json
{
  "test_id": "TABULAR_EVAL_01_001",
  "modality": "tabular_column",
  "metadata": {
    "language": "en",
    "domain": "customer_records",
    "test_type": "column_classification",
    "table_name": "telco_customer_profile"
  },
  "input_data": {
    "column_name": "primary_phone",
    "sample_values": ["+201001234567", "+201001234568", "missing"]
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
```

## Regenerate

Run the following from `/home/ubuntu/pii_pandas_generators`:

```bash
python3.11 build_table_column_golden_truth_json.py \
  --output-dir /home/ubuntu/pii_pandas_generators/table_column_golden_truth_json \
  --sample-dir /home/ubuntu/pii_pandas_generators/custom_pii_testcase_outputs/appended_tables \
  --sample-size 5
```

## Validate

```bash
python3.11 validate_table_column_golden_truth_json.py
```

The current validation status is **passed**.
