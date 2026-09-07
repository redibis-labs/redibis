#!/usr/bin/env python3
"""Validate per-table tabular-column golden-truth JSON outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from eg_pii_pandas_generators import TABLE_SCHEMAS, get_table_columns

OUTPUT_DIR = Path('/home/ubuntu/pii_pandas_generators/table_column_golden_truth_json')
REQUIRED_EVAL_KEYS = {'test_id', 'modality', 'metadata', 'input_data', 'golden_truth'}
REQUIRED_METADATA_KEYS = {'language', 'domain', 'test_type'}
REQUIRED_GT_KEYS = {'expected_semantic_type', 'is_pii', 'pii_category', 'sensitivity_level'}


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def validate_eval(eval_obj: Dict[str, Any], table_name: str, expected_column: str) -> None:
    missing = REQUIRED_EVAL_KEYS - set(eval_obj)
    assert not missing, f'{table_name}.{expected_column}: missing eval keys {missing}'
    assert eval_obj['modality'] == 'tabular_column', f'{table_name}.{expected_column}: wrong modality'
    assert eval_obj['metadata']['language'] == 'en', f'{table_name}.{expected_column}: wrong language'
    assert REQUIRED_METADATA_KEYS <= set(eval_obj['metadata']), f'{table_name}.{expected_column}: missing metadata keys'
    assert eval_obj['metadata']['test_type'] == 'column_classification', f'{table_name}.{expected_column}: wrong test type'
    assert eval_obj['input_data']['column_name'] == expected_column, f'{table_name}.{expected_column}: column mismatch'
    assert isinstance(eval_obj['input_data']['sample_values'], list), f'{table_name}.{expected_column}: sample_values is not list'
    assert len(eval_obj['input_data']['sample_values']) >= 1, f'{table_name}.{expected_column}: no sample values'
    gt = eval_obj['golden_truth']['column_classification']
    assert REQUIRED_GT_KEYS <= set(gt), f'{table_name}.{expected_column}: missing golden truth keys'
    assert isinstance(gt['is_pii'], bool), f'{table_name}.{expected_column}: is_pii must be boolean'
    assert isinstance(gt['expected_semantic_type'], str) and gt['expected_semantic_type'], f'{table_name}.{expected_column}: empty semantic type'
    assert isinstance(gt['pii_category'], str) and gt['pii_category'], f'{table_name}.{expected_column}: empty PII category'
    assert isinstance(gt['sensitivity_level'], str) and gt['sensitivity_level'], f'{table_name}.{expected_column}: empty sensitivity'


def main() -> int:
    total_columns = 0
    total_pii = 0
    table_results = []

    for table_name in TABLE_SCHEMAS:
        path = OUTPUT_DIR / f'{table_name}_golden_truth.json'
        assert path.exists(), f'Missing file: {path}'
        payload = load_json(path)
        expected_columns = get_table_columns(table_name)
        evaluations = payload['column_evaluations']
        assert payload['table_name'] == table_name, f'{table_name}: table_name mismatch'
        assert len(evaluations) == len(expected_columns), f'{table_name}: expected {len(expected_columns)} columns, got {len(evaluations)}'
        for eval_obj, expected_column in zip(evaluations, expected_columns):
            validate_eval(eval_obj, table_name, expected_column)
        pii_count = sum(1 for item in evaluations if item['golden_truth']['column_classification']['is_pii'])
        assert pii_count == payload['pii_column_count'], f'{table_name}: PII count mismatch'
        total_columns += len(evaluations)
        total_pii += pii_count
        table_results.append({'table_name': table_name, 'column_count': len(evaluations), 'pii_column_count': pii_count})

    combined = load_json(OUTPUT_DIR / 'all_tables_golden_truth.json')
    assert combined['table_count'] == len(TABLE_SCHEMAS), 'Combined table count mismatch'
    assert combined['column_count'] == total_columns, 'Combined column count mismatch'
    assert combined['pii_column_count'] == total_pii, 'Combined PII count mismatch'

    result = {
        'status': 'passed',
        'validated_table_count': len(TABLE_SCHEMAS),
        'validated_column_count': total_columns,
        'validated_pii_column_count': total_pii,
        'validated_non_pii_column_count': total_columns - total_pii,
        'validated_schema': 'tabular_column column_classification golden_truth',
        'table_results': table_results,
    }
    (OUTPUT_DIR / 'validation_result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
