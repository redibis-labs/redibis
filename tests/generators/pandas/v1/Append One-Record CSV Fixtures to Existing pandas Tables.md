# Append One-Record CSV Fixtures to Existing pandas Tables

This document provides concrete examples for appending one true CSV fixture file to an existing pandas DataFrame for each of the 14 Egyptian Telecom, E-commerce, and Fintech PII tables. The fixture files are real CSV files stored in `one_record_csv_fixtures/`; they contain synthetic data and are designed for repeatable PII framework evaluation.

The examples use the package CSV loaders with `existing_df=` and `target_path=`, so the output is both returned as a pandas DataFrame and saved to disk under `example_appended_outputs/`.

| Table | One-row CSV fixture | Appended output path |
|---|---|---|
| `eshop_customer_account` | `one_record_csv_fixtures/eshop_customer_account.csv` | `example_appended_outputs/eshop_customer_account_appended.csv` |
| `eshop_order_header` | `one_record_csv_fixtures/eshop_order_header.csv` | `example_appended_outputs/eshop_order_header_appended.csv` |
| `eshop_shipping_address` | `one_record_csv_fixtures/eshop_shipping_address.csv` | `example_appended_outputs/eshop_shipping_address_appended.csv` |
| `fintech_mobile_wallet_account` | `one_record_csv_fixtures/fintech_mobile_wallet_account.csv` | `example_appended_outputs/fintech_mobile_wallet_account_appended.csv` |
| `fintech_payment_transaction` | `one_record_csv_fixtures/fintech_payment_transaction.csv` | `example_appended_outputs/fintech_payment_transaction_appended.csv` |
| `merchant_seller_registry` | `one_record_csv_fixtures/merchant_seller_registry.csv` | `example_appended_outputs/merchant_seller_registry_appended.csv` |
| `sensitive_customer_risk_profile` | `one_record_csv_fixtures/sensitive_customer_risk_profile.csv` | `example_appended_outputs/sensitive_customer_risk_profile_appended.csv` |
| `support_ticket_free_text` | `one_record_csv_fixtures/support_ticket_free_text.csv` | `example_appended_outputs/support_ticket_free_text_appended.csv` |
| `telco_billing_invoice` | `one_record_csv_fixtures/telco_billing_invoice.csv` | `example_appended_outputs/telco_billing_invoice_appended.csv` |
| `telco_cdr_event` | `one_record_csv_fixtures/telco_cdr_event.csv` | `example_appended_outputs/telco_cdr_event_appended.csv` |
| `telco_customer_profile` | `one_record_csv_fixtures/telco_customer_profile.csv` | `example_appended_outputs/telco_customer_profile_appended.csv` |
| `telco_device_and_cpe_inventory` | `one_record_csv_fixtures/telco_device_and_cpe_inventory.csv` | `example_appended_outputs/telco_device_and_cpe_inventory_appended.csv` |
| `telco_kyc_identity_document` | `one_record_csv_fixtures/telco_kyc_identity_document.csv` | `example_appended_outputs/telco_kyc_identity_document_appended.csv` |
| `telco_subscriber_sim_registry` | `one_record_csv_fixtures/telco_subscriber_sim_registry.csv` | `example_appended_outputs/telco_subscriber_sim_registry_appended.csv` |

## Generic append pattern

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_customer_profile, load_telco_customer_profile_csv

existing_df = generate_telco_customer_profile(num_rows=2, seed=7001)
one_record_csv = Path("one_record_csv_fixtures/telco_customer_profile.csv")
saved_path = Path("example_appended_outputs/telco_customer_profile_appended.csv")

appended_df = load_telco_customer_profile_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(len(appended_df))  # existing 2 rows + one CSV fixture row = 3
print(saved_path)
```

## Table-specific append examples

### `eshop_customer_account`

Customer account table for Egyptian e-commerce, marketplace, and digital retail use cases.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_eshop_customer_account, load_eshop_customer_account_csv

existing_df = generate_eshop_customer_account(num_rows=2, seed=7001)
one_record_csv = Path("one_record_csv_fixtures/eshop_customer_account.csv")
saved_path = Path("example_appended_outputs/eshop_customer_account_appended.csv")

appended_df = load_eshop_customer_account_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `eshop_order_header`

Order-level table connecting customers, delivery address, payment channel, and fulfillment status.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_eshop_order_header, load_eshop_order_header_csv

existing_df = generate_eshop_order_header(num_rows=2, seed=7002)
one_record_csv = Path("one_record_csv_fixtures/eshop_order_header.csv")
saved_path = Path("example_appended_outputs/eshop_order_header_appended.csv")

appended_df = load_eshop_order_header_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `eshop_shipping_address`

Address-book table containing Egyptian delivery-address components needed for e-commerce fulfillment.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_eshop_shipping_address, load_eshop_shipping_address_csv

existing_df = generate_eshop_shipping_address(num_rows=2, seed=7003)
one_record_csv = Path("one_record_csv_fixtures/eshop_shipping_address.csv")
saved_path = Path("example_appended_outputs/eshop_shipping_address_appended.csv")

appended_df = load_eshop_shipping_address_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `fintech_mobile_wallet_account`

Mobile wallet account table for Vodafone Cash, Orange Cash, Etisalat Cash, WE Pay, and similar Egyptian wallet products.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_fintech_mobile_wallet_account, load_fintech_mobile_wallet_account_csv

existing_df = generate_fintech_mobile_wallet_account(num_rows=2, seed=7004)
one_record_csv = Path("one_record_csv_fixtures/fintech_mobile_wallet_account.csv")
saved_path = Path("example_appended_outputs/fintech_mobile_wallet_account_appended.csv")

appended_df = load_fintech_mobile_wallet_account_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `fintech_payment_transaction`

Unified payment-transaction table covering card, Meeza, Fawry, mobile-wallet, InstaPay, and bank-transfer flows used by telecom and e-commerce.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_fintech_payment_transaction, load_fintech_payment_transaction_csv

existing_df = generate_fintech_payment_transaction(num_rows=2, seed=7005)
one_record_csv = Path("one_record_csv_fixtures/fintech_payment_transaction.csv")
saved_path = Path("example_appended_outputs/fintech_payment_transaction_appended.csv")

appended_df = load_fintech_payment_transaction_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `merchant_seller_registry`

Business seller registry for Egyptian e-commerce marketplaces, covering tax, commercial, settlement, and contact identifiers.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_merchant_seller_registry, load_merchant_seller_registry_csv

existing_df = generate_merchant_seller_registry(num_rows=2, seed=7006)
one_record_csv = Path("one_record_csv_fixtures/merchant_seller_registry.csv")
saved_path = Path("example_appended_outputs/merchant_seller_registry_appended.csv")

appended_df = load_merchant_seller_registry_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `sensitive_customer_risk_profile`

Synthetic table for high-risk sensitive personal data classes under privacy and sector controls. Use only synthetic values.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_sensitive_customer_risk_profile, load_sensitive_customer_risk_profile_csv

existing_df = generate_sensitive_customer_risk_profile(num_rows=2, seed=7007)
one_record_csv = Path("one_record_csv_fixtures/sensitive_customer_risk_profile.csv")
saved_path = Path("example_appended_outputs/sensitive_customer_risk_profile_appended.csv")

appended_df = load_sensitive_customer_risk_profile_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `support_ticket_free_text`

Free-text ticket data for evaluating entity extraction from notes, SMS bodies, chat transcripts, emails, and logs.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_support_ticket_free_text, load_support_ticket_free_text_csv

existing_df = generate_support_ticket_free_text(num_rows=2, seed=7008)
one_record_csv = Path("one_record_csv_fixtures/support_ticket_free_text.csv")
saved_path = Path("example_appended_outputs/support_ticket_free_text_appended.csv")

appended_df = load_support_ticket_free_text_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `telco_billing_invoice`

Billing-account and invoice data for postpaid, prepaid hybrid, and fixed-line telecom services.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_billing_invoice, load_telco_billing_invoice_csv

existing_df = generate_telco_billing_invoice(num_rows=2, seed=7009)
one_record_csv = Path("one_record_csv_fixtures/telco_billing_invoice.csv")
saved_path = Path("example_appended_outputs/telco_billing_invoice_appended.csv")

appended_df = load_telco_billing_invoice_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `telco_cdr_event`

Synthetic call/SMS/data event table for testing PII detection in CDR-style transactional datasets.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_cdr_event, load_telco_cdr_event_csv

existing_df = generate_telco_cdr_event(num_rows=2, seed=7010)
one_record_csv = Path("one_record_csv_fixtures/telco_cdr_event.csv")
saved_path = Path("example_appended_outputs/telco_cdr_event_appended.csv")

appended_df = load_telco_cdr_event_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `telco_customer_profile`

Core customer profile used by CRM, billing, support, marketing consent, and KYC workflows for Egyptian telecom operators.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_customer_profile, load_telco_customer_profile_csv

existing_df = generate_telco_customer_profile(num_rows=2, seed=7011)
one_record_csv = Path("one_record_csv_fixtures/telco_customer_profile.csv")
saved_path = Path("example_appended_outputs/telco_customer_profile_appended.csv")

appended_df = load_telco_customer_profile_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `telco_device_and_cpe_inventory`

Tracks handset, router, ONT, and eSIM/device identifiers associated with subscribers and fixed broadband customers.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_device_and_cpe_inventory, load_telco_device_and_cpe_inventory_csv

existing_df = generate_telco_device_and_cpe_inventory(num_rows=2, seed=7012)
one_record_csv = Path("one_record_csv_fixtures/telco_device_and_cpe_inventory.csv")
saved_path = Path("example_appended_outputs/telco_device_and_cpe_inventory_appended.csv")

appended_df = load_telco_device_and_cpe_inventory_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `telco_kyc_identity_document`

Stores identity-document attributes captured during SIM registration, account opening, or customer due diligence.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_kyc_identity_document, load_telco_kyc_identity_document_csv

existing_df = generate_telco_kyc_identity_document(num_rows=2, seed=7013)
one_record_csv = Path("one_record_csv_fixtures/telco_kyc_identity_document.csv")
saved_path = Path("example_appended_outputs/telco_kyc_identity_document_appended.csv")

appended_df = load_telco_kyc_identity_document_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

### `telco_subscriber_sim_registry`

Represents active and historical SIM subscriptions, including Egyptian MSISDN, IMSI, ICCID, and operator-specific identifiers.

```python
from pathlib import Path
from eg_pii_pandas_generators import generate_telco_subscriber_sim_registry, load_telco_subscriber_sim_registry_csv

existing_df = generate_telco_subscriber_sim_registry(num_rows=2, seed=7014)
one_record_csv = Path("one_record_csv_fixtures/telco_subscriber_sim_registry.csv")
saved_path = Path("example_appended_outputs/telco_subscriber_sim_registry_appended.csv")

appended_df = load_telco_subscriber_sim_registry_csv(
    csv_path=one_record_csv,
    existing_df=existing_df,
    target_path=saved_path,
    mode="overwrite",
    validate_required=True,
    strict_columns=False,
    add_missing_columns=True,
)

print(appended_df.tail(1))
print(f"Saved appended table to: {saved_path}")
```

