# Egyptian PII Golden Data Generators for pandas

This package provides a pandas implementation of the 14-table Egyptian Telecom, E-commerce, and Fintech PII golden-data catalogue. It is designed for local notebooks, CI pipelines, agent tools, data-quality checks, and PII framework evaluation when Spark is not required.

The package exposes **28 table-specific functions**: one `generate_*` function and one `load_*_csv` function for each table. It also includes generic dispatchers such as `generate_table`, `load_table_csv`, `describe_table`, `list_tables`, `get_table_columns`, and `pii_columns`.

## Quick start

```python
import pandas as pd
from eg_pii_pandas_generators import (
    generate_telco_customer_profile,
    load_eshop_order_header_csv,
    generate_table,
    describe_table,
)

customers = generate_telco_customer_profile(num_rows=1000, seed=42)
print(customers.head())

orders = load_eshop_order_header_csv(
    "input/orders.csv",
    target_path="output/orders.csv",
    mode="overwrite",
)

cdr = generate_table("telco_cdr_event", num_rows=5000)
metadata = describe_table("telco_cdr_event")
```

## Table function catalogue

| Table | Columns | PII Columns | Generator | CSV Loader |
|---|---:|---:|---|---|
| `telco_customer_profile` | 11 | 9 | `generate_telco_customer_profile` | `load_telco_customer_profile_csv` |
| `telco_kyc_identity_document` | 10 | 8 | `generate_telco_kyc_identity_document` | `load_telco_kyc_identity_document_csv` |
| `telco_subscriber_sim_registry` | 10 | 7 | `generate_telco_subscriber_sim_registry` | `load_telco_subscriber_sim_registry_csv` |
| `telco_device_and_cpe_inventory` | 10 | 9 | `generate_telco_device_and_cpe_inventory` | `load_telco_device_and_cpe_inventory_csv` |
| `telco_cdr_event` | 13 | 12 | `generate_telco_cdr_event` | `load_telco_cdr_event_csv` |
| `telco_billing_invoice` | 11 | 10 | `generate_telco_billing_invoice` | `load_telco_billing_invoice_csv` |
| `eshop_customer_account` | 9 | 8 | `generate_eshop_customer_account` | `load_eshop_customer_account_csv` |
| `eshop_shipping_address` | 12 | 11 | `generate_eshop_shipping_address` | `load_eshop_shipping_address_csv` |
| `eshop_order_header` | 9 | 5 | `generate_eshop_order_header` | `load_eshop_order_header_csv` |
| `fintech_payment_transaction` | 14 | 13 | `generate_fintech_payment_transaction` | `load_fintech_payment_transaction_csv` |
| `fintech_mobile_wallet_account` | 10 | 8 | `generate_fintech_mobile_wallet_account` | `load_fintech_mobile_wallet_account_csv` |
| `merchant_seller_registry` | 11 | 11 | `generate_merchant_seller_registry` | `load_merchant_seller_registry_csv` |
| `support_ticket_free_text` | 8 | 4 | `generate_support_ticket_free_text` | `load_support_ticket_free_text_csv` |
| `sensitive_customer_risk_profile` | 9 | 8 | `generate_sensitive_customer_risk_profile` | `load_sensitive_customer_risk_profile_csv` |

## CSV loader behavior

The CSV loaders read a source CSV, normalize case-insensitive column names to the expected schema, add missing optional columns as `pd.NA` when requested, coerce common logical types, optionally append to an in-memory `existing_df`, and optionally write the combined result to CSV, Parquet, JSON, or JSONL.

The recommended agent defaults are `validate_required=True`, `strict_columns=False`, and `add_missing_columns=True`. These defaults make user uploads tolerant of harmless extra operational columns while still protecting required schema fields.

## Important safety note

All generated records are synthetic. They are intended for testing PII tools and should not be confused with real Egyptian customer, telecom, e-commerce, or payment data.
