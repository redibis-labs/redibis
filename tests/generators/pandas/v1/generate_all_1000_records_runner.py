"""
Runner-only script for generating 1,000 synthetic records for every Egyptian
Telecom, E-commerce, and Fintech PII table in the pandas generator package.

This file does not contain pre-generated data. It generates CSV files only when
it is executed.

Basic usage from the package directory:

    python generate_all_1000_records_runner.py

Custom usage:

    python generate_all_1000_records_runner.py --num-rows 1000 --seed 42 --output-dir generated_1000_records

Example equivalent to the requested style:

    customers = generate_telco_customer_profile(num_rows=1000, seed=42)
    customers.to_csv('generated_1000_records/telco_customer_profile.csv', index=False)
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Dict

import pandas as pd

from eg_pii_pandas_generators import (
    generate_eshop_customer_account,
    generate_eshop_order_header,
    generate_eshop_shipping_address,
    generate_fintech_mobile_wallet_account,
    generate_fintech_payment_transaction,
    generate_merchant_seller_registry,
    generate_sensitive_customer_risk_profile,
    generate_support_ticket_free_text,
    generate_telco_billing_invoice,
    generate_telco_cdr_event,
    generate_telco_customer_profile,
    generate_telco_device_and_cpe_inventory,
    generate_telco_kyc_identity_document,
    generate_telco_subscriber_sim_registry,
)


# Table name -> generator function.
# The output CSV name will be: {table_name}.csv
TABLE_GENERATORS: Dict[str, Callable[[int, int], pd.DataFrame]] = {
    "eshop_customer_account": generate_eshop_customer_account,
    "eshop_order_header": generate_eshop_order_header,
    "eshop_shipping_address": generate_eshop_shipping_address,
    "fintech_mobile_wallet_account": generate_fintech_mobile_wallet_account,
    "fintech_payment_transaction": generate_fintech_payment_transaction,
    "merchant_seller_registry": generate_merchant_seller_registry,
    "sensitive_customer_risk_profile": generate_sensitive_customer_risk_profile,
    "support_ticket_free_text": generate_support_ticket_free_text,
    "telco_billing_invoice": generate_telco_billing_invoice,
    "telco_cdr_event": generate_telco_cdr_event,
    "telco_customer_profile": generate_telco_customer_profile,
    "telco_device_and_cpe_inventory": generate_telco_device_and_cpe_inventory,
    "telco_kyc_identity_document": generate_telco_kyc_identity_document,
    "telco_subscriber_sim_registry": generate_telco_subscriber_sim_registry,
}


def generate_all_tables(
    num_rows: int = 1000,
    seed: int = 42,
    output_dir: str | Path = "generated_1000_records",
    index: bool = False,
) -> Dict[str, Path]:
    """
    Generate synthetic records for all 14 tables and save each table as CSV.

    Parameters
    ----------
    num_rows:
        Number of synthetic records to generate per table. Default is 1,000.
    seed:
        Base random seed. Each table receives a deterministic derived seed by
        adding its position to the base seed, so outputs are reproducible while
        still varying across tables.
    output_dir:
        Directory where CSV files will be written. It is created if missing.
    index:
        Whether to include the pandas index in the saved CSV files. Default is
        False, which is usually preferred for golden-data fixtures.

    Returns
    -------
    Dict[str, Path]
        Mapping from table name to the CSV path written for that table.

    Example
    -------
    >>> written_files = generate_all_tables(num_rows=1000, seed=42)
    >>> written_files["telco_customer_profile"]
    PosixPath('generated_1000_records/telco_customer_profile.csv')

    Agent/tool hint
    ---------------
    Use this function when an agent needs a single deterministic command to
    materialize the complete 14-table pandas golden dataset. If only one table
    is needed, call the table-specific generator directly instead.
    """
    if num_rows < 1:
        raise ValueError("num_rows must be >= 1")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    written_files: Dict[str, Path] = {}

    for offset, (table_name, generator_func) in enumerate(TABLE_GENERATORS.items()):
        table_seed = seed + offset
        df = generator_func(num_rows=num_rows, seed=table_seed)
        csv_path = output_path / f"{table_name}.csv"
        df.to_csv(csv_path, index=index)
        written_files[table_name] = csv_path
        print(f"{table_name}: rows={len(df)} saved_to={csv_path}")

    return written_files


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the runner script."""
    parser = argparse.ArgumentParser(
        description="Generate CSV files with synthetic records for all 14 pandas PII tables."
    )
    parser.add_argument(
        "--num-rows",
        type=int,
        default=1000,
        help="Number of records to generate per table. Default: 1000.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Base random seed for deterministic output. Default: 42.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="generated_1000_records",
        help="Directory where table CSV files will be saved. Default: generated_1000_records.",
    )
    parser.add_argument(
        "--include-index",
        action="store_true",
        help="Include pandas index in output CSV files. Default: false.",
    )
    return parser.parse_args()


def main() -> None:
    """Command-line entry point."""
    args = parse_args()
    generate_all_tables(
        num_rows=args.num_rows,
        seed=args.seed,
        output_dir=args.output_dir,
        index=args.include_index,
    )


if __name__ == "__main__":
    main()
