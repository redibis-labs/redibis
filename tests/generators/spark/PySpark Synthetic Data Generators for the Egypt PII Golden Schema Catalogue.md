# PySpark Synthetic Data Generators for the Egypt PII Golden Schema Catalogue

This package contains a single PySpark utility module, `eg_pii_spark_generators.py`, generated from the 14-table ODCS-style catalogue for Egyptian telecom, e-commerce, and fintech PII testing. For every table there are two public functions: a generator that takes `num_rows`, and a CSV loader that can append uploaded records to the matching table schema.

> All generated values are **synthetic** and designed for testing PII tools. They should not be interpreted as real customer, subscriber, wallet, card, or identity records.

## Quick Start

```python
from pyspark.sql import SparkSession
from eg_pii_spark_generators import generate_telco_customer_profile, load_telco_customer_profile_csv

spark = SparkSession.builder.appName("egypt-pii-golden-data").getOrCreate()

# Create 1,000 synthetic telecom customer records.
df = generate_telco_customer_profile(spark, num_rows=1000, seed=42)
df.write.mode("overwrite").saveAsTable("golden.telco_customer_profile")

# Add records from a user-provided CSV file.
added_df = load_telco_customer_profile_csv(
    spark,
    csv_path="/mnt/landing/telco_customer_profile.csv",
    target_table="golden.telco_customer_profile",
    mode="append"
)
```

## Generic Agent-Friendly Dispatchers

Agents that receive a table name dynamically can use the generic dispatchers instead of importing table-specific functions.

```python
from eg_pii_spark_generators import generate_table, load_table_csv

df = generate_table(spark, table_name="fintech_payment_transaction", num_rows=5000, seed=9)
loaded = load_table_csv(
    spark,
    table_name="fintech_payment_transaction",
    csv_path="/mnt/uploads/payments.csv",
    target_path="/mnt/golden/fintech_payment_transaction",
    output_format="parquet",
    mode="append"
)
```

## Function Catalogue

| Table | Domain | Columns | PII Columns | Synthetic Generator | CSV Loader |
|---|---:|---:|---:|---|---|
| `telco_customer_profile` | telecom_crm | 11 | 9 | `generate_telco_customer_profile(spark, num_rows, seed=42)` | `load_telco_customer_profile_csv(spark, csv_path, ...)` |
| `telco_kyc_identity_document` | telecom_kyc | 10 | 8 | `generate_telco_kyc_identity_document(spark, num_rows, seed=42)` | `load_telco_kyc_identity_document_csv(spark, csv_path, ...)` |
| `telco_subscriber_sim_registry` | telecom_bss | 10 | 7 | `generate_telco_subscriber_sim_registry(spark, num_rows, seed=42)` | `load_telco_subscriber_sim_registry_csv(spark, csv_path, ...)` |
| `telco_device_and_cpe_inventory` | telecom_oss | 10 | 9 | `generate_telco_device_and_cpe_inventory(spark, num_rows, seed=42)` | `load_telco_device_and_cpe_inventory_csv(spark, csv_path, ...)` |
| `telco_cdr_event` | telecom_network | 13 | 12 | `generate_telco_cdr_event(spark, num_rows, seed=42)` | `load_telco_cdr_event_csv(spark, csv_path, ...)` |
| `telco_billing_invoice` | telecom_billing | 11 | 10 | `generate_telco_billing_invoice(spark, num_rows, seed=42)` | `load_telco_billing_invoice_csv(spark, csv_path, ...)` |
| `eshop_customer_account` | ecommerce_crm | 9 | 8 | `generate_eshop_customer_account(spark, num_rows, seed=42)` | `load_eshop_customer_account_csv(spark, csv_path, ...)` |
| `eshop_shipping_address` | ecommerce_fulfillment | 12 | 11 | `generate_eshop_shipping_address(spark, num_rows, seed=42)` | `load_eshop_shipping_address_csv(spark, csv_path, ...)` |
| `eshop_order_header` | ecommerce_orders | 9 | 5 | `generate_eshop_order_header(spark, num_rows, seed=42)` | `load_eshop_order_header_csv(spark, csv_path, ...)` |
| `fintech_payment_transaction` | fintech_payments | 14 | 13 | `generate_fintech_payment_transaction(spark, num_rows, seed=42)` | `load_fintech_payment_transaction_csv(spark, csv_path, ...)` |
| `fintech_mobile_wallet_account` | fintech_wallets | 10 | 8 | `generate_fintech_mobile_wallet_account(spark, num_rows, seed=42)` | `load_fintech_mobile_wallet_account_csv(spark, csv_path, ...)` |
| `merchant_seller_registry` | ecommerce_merchant | 11 | 11 | `generate_merchant_seller_registry(spark, num_rows, seed=42)` | `load_merchant_seller_registry_csv(spark, csv_path, ...)` |
| `support_ticket_free_text` | cross_domain_support | 8 | 4 | `generate_support_ticket_free_text(spark, num_rows, seed=42)` | `load_support_ticket_free_text_csv(spark, csv_path, ...)` |
| `sensitive_customer_risk_profile` | cross_domain_sensitive | 9 | 8 | `generate_sensitive_customer_risk_profile(spark, num_rows, seed=42)` | `load_sensitive_customer_risk_profile_csv(spark, csv_path, ...)` |

## CSV Loader Behavior

Each `load_*_csv` function reads a CSV file, matches columns case-insensitively, casts fields to the expected Spark logical types, reorders columns to the target schema, and optionally writes the result using `saveAsTable` or `DataFrameWriter.save`. Missing required columns raise `ValueError` by default. Set `validate_required=False` only when intentionally profiling incomplete files.

## Agent Tool Hints

| Scenario | Recommended Call Pattern | Rationale |
|---|---|---|
| Create benchmark source data | `generate_table(spark, table_name, num_rows, seed)` | Stable deterministic data for scanner comparison. |
| Add user CSV rows to a table | `load_table_csv(spark, table_name, csv_path, target_table=..., mode="append")` | Safely casts and reorders uploaded CSV rows. |
| Write files instead of Hive/Unity Catalog | `target_path="...", output_format="parquet"` | Avoids depending on catalog configuration. |
| Use Delta Lake | `output_format="delta"` | Works only if the Spark runtime has Delta Lake installed. |
| Validate a table before generation | `describe_table(table_name)` and `get_table_columns(table_name)` | Lets an agent explain the schema before acting. |

## Design Notes

The synthetic generators are deterministic, Egypt-oriented, and PII-aware. They create realistic-looking Egyptian mobile numbers, national IDs, SIM identifiers, device identifiers, addresses, wallet/payment references, support-ticket text, and sensitive-data edge cases. The CSV loaders are intentionally conservative and do not silently drop unexpected CSV columns from the original DataFrame until the final projection to the target schema.
