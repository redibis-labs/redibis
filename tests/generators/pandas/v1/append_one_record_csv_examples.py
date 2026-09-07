"""Append one true CSV fixture record to an existing pandas table, then save to disk.

This script is agent-friendly: each function call is deterministic, uses an
actual one-row CSV file from ./one_record_csv_fixtures, appends it to an
existing DataFrame, and writes the result to ./example_appended_outputs.
All records are synthetic and are intended for PII framework testing only.
"""

from pathlib import Path

from eg_pii_pandas_generators import (
    generate_eshop_customer_account,
    load_eshop_customer_account_csv,
    generate_eshop_order_header,
    load_eshop_order_header_csv,
    generate_eshop_shipping_address,
    load_eshop_shipping_address_csv,
    generate_fintech_mobile_wallet_account,
    load_fintech_mobile_wallet_account_csv,
    generate_fintech_payment_transaction,
    load_fintech_payment_transaction_csv,
    generate_merchant_seller_registry,
    load_merchant_seller_registry_csv,
    generate_sensitive_customer_risk_profile,
    load_sensitive_customer_risk_profile_csv,
    generate_support_ticket_free_text,
    load_support_ticket_free_text_csv,
    generate_telco_billing_invoice,
    load_telco_billing_invoice_csv,
    generate_telco_cdr_event,
    load_telco_cdr_event_csv,
    generate_telco_customer_profile,
    load_telco_customer_profile_csv,
    generate_telco_device_and_cpe_inventory,
    load_telco_device_and_cpe_inventory_csv,
    generate_telco_kyc_identity_document,
    load_telco_kyc_identity_document_csv,
    generate_telco_subscriber_sim_registry,
    load_telco_subscriber_sim_registry_csv,
)

ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "one_record_csv_fixtures"
OUTPUT_DIR = ROOT / "example_appended_outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def append_one_eshop_customer_account_csv_to_existing_table():
    """Append one CSV fixture row to an existing `eshop_customer_account` DataFrame and save it to disk."""
    existing_df = generate_eshop_customer_account(num_rows=2, seed=7001)
    one_record_csv = FIXTURE_DIR / "eshop_customer_account.csv"
    saved_path = OUTPUT_DIR / "eshop_customer_account_appended.csv"
    appended_df = load_eshop_customer_account_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_eshop_order_header_csv_to_existing_table():
    """Append one CSV fixture row to an existing `eshop_order_header` DataFrame and save it to disk."""
    existing_df = generate_eshop_order_header(num_rows=2, seed=7002)
    one_record_csv = FIXTURE_DIR / "eshop_order_header.csv"
    saved_path = OUTPUT_DIR / "eshop_order_header_appended.csv"
    appended_df = load_eshop_order_header_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_eshop_shipping_address_csv_to_existing_table():
    """Append one CSV fixture row to an existing `eshop_shipping_address` DataFrame and save it to disk."""
    existing_df = generate_eshop_shipping_address(num_rows=2, seed=7003)
    one_record_csv = FIXTURE_DIR / "eshop_shipping_address.csv"
    saved_path = OUTPUT_DIR / "eshop_shipping_address_appended.csv"
    appended_df = load_eshop_shipping_address_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_fintech_mobile_wallet_account_csv_to_existing_table():
    """Append one CSV fixture row to an existing `fintech_mobile_wallet_account` DataFrame and save it to disk."""
    existing_df = generate_fintech_mobile_wallet_account(num_rows=2, seed=7004)
    one_record_csv = FIXTURE_DIR / "fintech_mobile_wallet_account.csv"
    saved_path = OUTPUT_DIR / "fintech_mobile_wallet_account_appended.csv"
    appended_df = load_fintech_mobile_wallet_account_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_fintech_payment_transaction_csv_to_existing_table():
    """Append one CSV fixture row to an existing `fintech_payment_transaction` DataFrame and save it to disk."""
    existing_df = generate_fintech_payment_transaction(num_rows=2, seed=7005)
    one_record_csv = FIXTURE_DIR / "fintech_payment_transaction.csv"
    saved_path = OUTPUT_DIR / "fintech_payment_transaction_appended.csv"
    appended_df = load_fintech_payment_transaction_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_merchant_seller_registry_csv_to_existing_table():
    """Append one CSV fixture row to an existing `merchant_seller_registry` DataFrame and save it to disk."""
    existing_df = generate_merchant_seller_registry(num_rows=2, seed=7006)
    one_record_csv = FIXTURE_DIR / "merchant_seller_registry.csv"
    saved_path = OUTPUT_DIR / "merchant_seller_registry_appended.csv"
    appended_df = load_merchant_seller_registry_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_sensitive_customer_risk_profile_csv_to_existing_table():
    """Append one CSV fixture row to an existing `sensitive_customer_risk_profile` DataFrame and save it to disk."""
    existing_df = generate_sensitive_customer_risk_profile(num_rows=2, seed=7007)
    one_record_csv = FIXTURE_DIR / "sensitive_customer_risk_profile.csv"
    saved_path = OUTPUT_DIR / "sensitive_customer_risk_profile_appended.csv"
    appended_df = load_sensitive_customer_risk_profile_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_support_ticket_free_text_csv_to_existing_table():
    """Append one CSV fixture row to an existing `support_ticket_free_text` DataFrame and save it to disk."""
    existing_df = generate_support_ticket_free_text(num_rows=2, seed=7008)
    one_record_csv = FIXTURE_DIR / "support_ticket_free_text.csv"
    saved_path = OUTPUT_DIR / "support_ticket_free_text_appended.csv"
    appended_df = load_support_ticket_free_text_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_telco_billing_invoice_csv_to_existing_table():
    """Append one CSV fixture row to an existing `telco_billing_invoice` DataFrame and save it to disk."""
    existing_df = generate_telco_billing_invoice(num_rows=2, seed=7009)
    one_record_csv = FIXTURE_DIR / "telco_billing_invoice.csv"
    saved_path = OUTPUT_DIR / "telco_billing_invoice_appended.csv"
    appended_df = load_telco_billing_invoice_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_telco_cdr_event_csv_to_existing_table():
    """Append one CSV fixture row to an existing `telco_cdr_event` DataFrame and save it to disk."""
    existing_df = generate_telco_cdr_event(num_rows=2, seed=7010)
    one_record_csv = FIXTURE_DIR / "telco_cdr_event.csv"
    saved_path = OUTPUT_DIR / "telco_cdr_event_appended.csv"
    appended_df = load_telco_cdr_event_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_telco_customer_profile_csv_to_existing_table():
    """Append one CSV fixture row to an existing `telco_customer_profile` DataFrame and save it to disk."""
    existing_df = generate_telco_customer_profile(num_rows=2, seed=7011)
    one_record_csv = FIXTURE_DIR / "telco_customer_profile.csv"
    saved_path = OUTPUT_DIR / "telco_customer_profile_appended.csv"
    appended_df = load_telco_customer_profile_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_telco_device_and_cpe_inventory_csv_to_existing_table():
    """Append one CSV fixture row to an existing `telco_device_and_cpe_inventory` DataFrame and save it to disk."""
    existing_df = generate_telco_device_and_cpe_inventory(num_rows=2, seed=7012)
    one_record_csv = FIXTURE_DIR / "telco_device_and_cpe_inventory.csv"
    saved_path = OUTPUT_DIR / "telco_device_and_cpe_inventory_appended.csv"
    appended_df = load_telco_device_and_cpe_inventory_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_telco_kyc_identity_document_csv_to_existing_table():
    """Append one CSV fixture row to an existing `telco_kyc_identity_document` DataFrame and save it to disk."""
    existing_df = generate_telco_kyc_identity_document(num_rows=2, seed=7013)
    one_record_csv = FIXTURE_DIR / "telco_kyc_identity_document.csv"
    saved_path = OUTPUT_DIR / "telco_kyc_identity_document_appended.csv"
    appended_df = load_telco_kyc_identity_document_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def append_one_telco_subscriber_sim_registry_csv_to_existing_table():
    """Append one CSV fixture row to an existing `telco_subscriber_sim_registry` DataFrame and save it to disk."""
    existing_df = generate_telco_subscriber_sim_registry(num_rows=2, seed=7014)
    one_record_csv = FIXTURE_DIR / "telco_subscriber_sim_registry.csv"
    saved_path = OUTPUT_DIR / "telco_subscriber_sim_registry_appended.csv"
    appended_df = load_telco_subscriber_sim_registry_csv(
        csv_path=one_record_csv,
        existing_df=existing_df,
        target_path=saved_path,
        mode="overwrite",
        validate_required=True,
        strict_columns=False,
        add_missing_columns=True,
    )
    return appended_df, saved_path

def run_all_append_examples() -> dict[str, dict[str, object]]:
    """Run all 14 append examples and return row counts plus saved paths."""
    results: dict[str, dict[str, object]] = {}
    eshop_customer_account_df, eshop_customer_account_path = append_one_eshop_customer_account_csv_to_existing_table()
    results["eshop_customer_account"] = {"rows": len(eshop_customer_account_df), "saved_path": str(eshop_customer_account_path)}
    eshop_order_header_df, eshop_order_header_path = append_one_eshop_order_header_csv_to_existing_table()
    results["eshop_order_header"] = {"rows": len(eshop_order_header_df), "saved_path": str(eshop_order_header_path)}
    eshop_shipping_address_df, eshop_shipping_address_path = append_one_eshop_shipping_address_csv_to_existing_table()
    results["eshop_shipping_address"] = {"rows": len(eshop_shipping_address_df), "saved_path": str(eshop_shipping_address_path)}
    fintech_mobile_wallet_account_df, fintech_mobile_wallet_account_path = append_one_fintech_mobile_wallet_account_csv_to_existing_table()
    results["fintech_mobile_wallet_account"] = {"rows": len(fintech_mobile_wallet_account_df), "saved_path": str(fintech_mobile_wallet_account_path)}
    fintech_payment_transaction_df, fintech_payment_transaction_path = append_one_fintech_payment_transaction_csv_to_existing_table()
    results["fintech_payment_transaction"] = {"rows": len(fintech_payment_transaction_df), "saved_path": str(fintech_payment_transaction_path)}
    merchant_seller_registry_df, merchant_seller_registry_path = append_one_merchant_seller_registry_csv_to_existing_table()
    results["merchant_seller_registry"] = {"rows": len(merchant_seller_registry_df), "saved_path": str(merchant_seller_registry_path)}
    sensitive_customer_risk_profile_df, sensitive_customer_risk_profile_path = append_one_sensitive_customer_risk_profile_csv_to_existing_table()
    results["sensitive_customer_risk_profile"] = {"rows": len(sensitive_customer_risk_profile_df), "saved_path": str(sensitive_customer_risk_profile_path)}
    support_ticket_free_text_df, support_ticket_free_text_path = append_one_support_ticket_free_text_csv_to_existing_table()
    results["support_ticket_free_text"] = {"rows": len(support_ticket_free_text_df), "saved_path": str(support_ticket_free_text_path)}
    telco_billing_invoice_df, telco_billing_invoice_path = append_one_telco_billing_invoice_csv_to_existing_table()
    results["telco_billing_invoice"] = {"rows": len(telco_billing_invoice_df), "saved_path": str(telco_billing_invoice_path)}
    telco_cdr_event_df, telco_cdr_event_path = append_one_telco_cdr_event_csv_to_existing_table()
    results["telco_cdr_event"] = {"rows": len(telco_cdr_event_df), "saved_path": str(telco_cdr_event_path)}
    telco_customer_profile_df, telco_customer_profile_path = append_one_telco_customer_profile_csv_to_existing_table()
    results["telco_customer_profile"] = {"rows": len(telco_customer_profile_df), "saved_path": str(telco_customer_profile_path)}
    telco_device_and_cpe_inventory_df, telco_device_and_cpe_inventory_path = append_one_telco_device_and_cpe_inventory_csv_to_existing_table()
    results["telco_device_and_cpe_inventory"] = {"rows": len(telco_device_and_cpe_inventory_df), "saved_path": str(telco_device_and_cpe_inventory_path)}
    telco_kyc_identity_document_df, telco_kyc_identity_document_path = append_one_telco_kyc_identity_document_csv_to_existing_table()
    results["telco_kyc_identity_document"] = {"rows": len(telco_kyc_identity_document_df), "saved_path": str(telco_kyc_identity_document_path)}
    telco_subscriber_sim_registry_df, telco_subscriber_sim_registry_path = append_one_telco_subscriber_sim_registry_csv_to_existing_table()
    results["telco_subscriber_sim_registry"] = {"rows": len(telco_subscriber_sim_registry_df), "saved_path": str(telco_subscriber_sim_registry_path)}
    return results

if __name__ == "__main__":
    for table_name, result in run_all_append_examples().items():
        print(f"{table_name}: rows={result['rows']} saved_path={result['saved_path']}")
