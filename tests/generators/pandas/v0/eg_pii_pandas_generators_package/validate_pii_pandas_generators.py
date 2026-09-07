import importlib.util
from pathlib import Path
import tempfile

module_path = Path('/home/ubuntu/pii_pandas_generators/eg_pii_pandas_generators.py')
spec = importlib.util.spec_from_file_location('eg_pii_pandas_generators', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

expected_tables = 14
assert len(module.TABLE_SCHEMAS) == expected_tables, len(module.TABLE_SCHEMAS)
assert len(module.GENERATOR_FUNCTIONS) == expected_tables, len(module.GENERATOR_FUNCTIONS)
assert len(module.CSV_LOADER_FUNCTIONS) == expected_tables, len(module.CSV_LOADER_FUNCTIONS)

for table_name in module.TABLE_SCHEMAS:
    gen_name = f'generate_{table_name}'
    load_name = f'load_{table_name}_csv'
    assert hasattr(module, gen_name), gen_name
    assert hasattr(module, load_name), load_name
    gen = getattr(module, gen_name)
    load = getattr(module, load_name)
    assert gen.__doc__ and 'Inputs:' in gen.__doc__, gen_name
    assert load.__doc__ and 'Agent-use hint:' in load.__doc__, load_name
    df = gen(3)
    assert list(df.columns) == module.get_table_columns(table_name), table_name
    assert len(df) == 3
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / f'{table_name}.csv'
        df.to_csv(p, index=False)
        loaded = load(p)
        assert list(loaded.columns) == module.get_table_columns(table_name), table_name
        assert len(loaded) == 3

sample = module.generate_table('telco_customer_profile', 5)
assert len(sample) == 5
assert module.pii_columns('telco_customer_profile')
print({
    'module': str(module_path),
    'tables': len(module.TABLE_SCHEMAS),
    'generators': len(module.GENERATOR_FUNCTIONS),
    'csv_loaders': len(module.CSV_LOADER_FUNCTIONS),
    'roundtrip_csv_validated': True,
})
