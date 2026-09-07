"""Agent-oriented pandas examples for Egyptian PII golden data."""

from eg_pii_pandas_generators import (
    describe_table,
    generate_table,
    load_table_csv,
    generate_fintech_payment_transaction,
    load_fintech_payment_transaction_csv,
)

# Example 1: Dynamic table selection by an agent or benchmark runner.
table_name = "telco_cdr_event"
row_count = 10000
schema_info = describe_table(table_name)
print(f"Generating {row_count} rows for {table_name}: {schema_info['description']}")
synthetic_df = generate_table(table_name=table_name, num_rows=row_count, seed=42)
synthetic_df.to_csv(f"{table_name}.csv", index=False)

# Example 2: Direct table-specific generation for fintech payment tests.
payments_df = generate_fintech_payment_transaction(num_rows=5000, seed=17)
payments_df.to_parquet("fintech_payment_transaction.parquet", index=False)

# Example 3: Load user-uploaded CSV records and append to an existing DataFrame.
loaded_df = load_table_csv(
    table_name="eshop_shipping_address",
    csv_path="uploads/eshop_shipping_address.csv",
    existing_df=None,
    target_path="golden/eshop_shipping_address.csv",
    mode="append",
    validate_required=True,
)

# Example 4: Direct table-specific CSV loader.
payment_csv_df = load_fintech_payment_transaction_csv(
    csv_path="uploads/fintech_payment_transaction.csv",
    target_path="golden/fintech_payment_transaction.jsonl",
    output_format="jsonl",
    mode="append",
)

print("Loaded rows:", len(loaded_df), len(payment_csv_df))
