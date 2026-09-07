"""
Agent-oriented examples for eg_pii_spark_generators.py.

These examples assume they are executed inside a PySpark-capable runtime, such as
Databricks, EMR, Glue, Synapse Spark, or a local Spark shell with PySpark installed.
"""

from pyspark.sql import SparkSession
from eg_pii_spark_generators import (
    describe_table,
    generate_table,
    load_table_csv,
    generate_fintech_payment_transaction,
    load_fintech_payment_transaction_csv,
)

spark = SparkSession.builder.appName("egypt-pii-golden-data-agent-demo").getOrCreate()

# Example 1: Agent receives a dynamic table name and requested row count.
table_name = "telco_cdr_event"
row_count = 10000
schema_info = describe_table(table_name)
print(f"Generating {row_count} rows for {table_name}: {schema_info['description']}")
synthetic_df = generate_table(spark, table_name=table_name, num_rows=row_count, seed=42)
synthetic_df.write.mode("overwrite").saveAsTable(f"golden.{table_name}")

# Example 2: Direct table-specific function for fintech payments.
payments_df = generate_fintech_payment_transaction(spark, num_rows=5000, seed=17)
payments_df.write.mode("overwrite").format("parquet").save("/mnt/golden/fintech_payment_transaction")

# Example 3: Agent receives a user-uploaded CSV and appends it to a managed table.
loaded_df = load_table_csv(
    spark,
    table_name="eshop_shipping_address",
    csv_path="/mnt/uploads/eshop_shipping_address.csv",
    target_table="golden.eshop_shipping_address",
    mode="append",
    validate_required=True,
)

# Example 4: Direct table-specific CSV loader with a path target instead of catalog table.
payment_csv_df = load_fintech_payment_transaction_csv(
    spark,
    csv_path="/mnt/uploads/fintech_payment_transaction.csv",
    target_path="/mnt/golden/fintech_payment_transaction",
    output_format="parquet",
    mode="append",
)

print("Loaded rows:", loaded_df.count(), payment_csv_df.count())
