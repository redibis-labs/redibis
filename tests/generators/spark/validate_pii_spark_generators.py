import importlib.util
from pathlib import Path

module_path = Path('/home/ubuntu/pii_spark_generators/eg_pii_spark_generators.py')
spec = importlib.util.spec_from_file_location('eg_pii_spark_generators', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

expected_tables = 14
assert len(module.TABLE_SCHEMAS) == expected_tables, len(module.TABLE_SCHEMAS)
assert len(module.GENERATOR_FUNCTIONS) == expected_tables, len(module.GENERATOR_FUNCTIONS)
assert len(module.CSV_LOADER_FUNCTIONS) == expected_tables, len(module.CSV_LOADER_FUNCTIONS)

for table_name, meta in module.TABLE_SCHEMAS.items():
    gen_name = f'generate_{table_name}'
    load_name = f'load_{table_name}_csv'
    assert hasattr(module, gen_name), gen_name
    assert hasattr(module, load_name), load_name
    assert getattr(module, gen_name).__doc__ and 'Inputs:' in getattr(module, gen_name).__doc__, gen_name
    assert getattr(module, load_name).__doc__ and 'Agent-use hint:' in getattr(module, load_name).__doc__, load_name
    assert meta['columns'], table_name

sample_table = 'telco_customer_profile'
assert module.get_table_columns(sample_table)[0] == 'customer_id'
assert 'columns' in module.describe_table(sample_table)

print({
    'module': str(module_path),
    'tables': len(module.TABLE_SCHEMAS),
    'generators': len(module.GENERATOR_FUNCTIONS),
    'csv_loaders': len(module.CSV_LOADER_FUNCTIONS),
    'validated_docstrings': True,
})
