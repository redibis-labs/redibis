from pathlib import Path
import importlib.util
import textwrap

SPARK_MODULE_PATH = Path('/home/ubuntu/pii_spark_generators/eg_pii_spark_generators.py')
OUT_DIR = Path('/home/ubuntu/pii_pandas_generators')
OUT_DIR.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location('eg_pii_spark_generators', SPARK_MODULE_PATH)
source_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source_module)
schemas = source_module.TABLE_SCHEMAS

module_template = r'''"""
Egypt Telecom, E-commerce, and Fintech PII Synthetic Data Utilities for pandas.

This module is the pandas equivalent of the PySpark utility package generated for
an Egyptian PII golden-data catalogue. It exposes two public functions for each
of the 14 table schemas:

1. ``generate_<table_name>(num_rows, seed=42)`` returns a pandas DataFrame with
   realistic but entirely synthetic values aligned to the table schema.
2. ``load_<table_name>_csv(csv_path, existing_df=None, target_path=None, ...)``
   reads records from a CSV file, coerces/reorders columns to the expected
   schema, optionally combines them with an existing DataFrame, and optionally
   writes the result to a file.

The generated records are synthetic and are intended for PII scanner evaluation,
classification benchmarking, anonymization tests, data-contract validation, and
agent-driven tool testing. They must not be treated as real customer data.

Agent-tool usage hint:
    Agents can call ``describe_table(table_name)`` and ``get_table_columns`` to
    understand expected inputs before invoking ``generate_table`` or
    ``load_table_csv``. For user-uploaded CSV files, use the table-specific
    ``load_*_csv`` function with ``validate_required=True`` and
    ``strict_columns=False`` unless the test intentionally checks schema failure.
"""

from __future__ import annotations

import hashlib
import random
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import pandas as pd

TABLE_SCHEMAS: Dict[str, Dict[str, Any]] = __TABLE_SCHEMAS_REPR__

FIRST_NAMES_EN = ["Ahmed", "Mohamed", "Mahmoud", "Omar", "Youssef", "Mariam", "Nour", "Salma", "Hana", "Farida"]
FATHER_NAMES_EN = ["Hassan", "Ali", "Ibrahim", "Mostafa", "Samir", "Khaled", "Tarek", "Adel", "Nabil", "Fouad"]
FAMILY_NAMES_EN = ["ElSayed", "Abdelrahman", "Mahfouz", "Shoukry", "Fathy", "Gaber", "Zaki", "Kamel", "Nassar", "Darwish"]
FIRST_NAMES_AR = ["أحمد", "محمد", "محمود", "عمر", "يوسف", "مريم", "نور", "سلمى", "هنا", "فريدة"]
FATHER_NAMES_AR = ["حسن", "علي", "إبراهيم", "مصطفى", "سمير", "خالد", "طارق", "عادل", "نبيل", "فؤاد"]
FAMILY_NAMES_AR = ["السيد", "عبد الرحمن", "محفوظ", "شكري", "فتحي", "جابر", "زكي", "كامل", "نصار", "درويش"]
GOVERNORATES = ["Cairo", "Giza", "Alexandria", "Dakahlia", "Red Sea", "Beheira", "Fayoum", "Gharbia", "Ismailia", "Menofia", "Minya", "Qaliubiya", "New Valley", "Suez", "Aswan", "Assiut", "Beni Suef", "Port Said", "Damietta", "Sharkia", "South Sinai", "Kafr El Sheikh", "Matrouh", "Luxor", "Qena", "North Sinai", "Sohag"]
DISTRICTS = ["Nasr City", "Maadi", "Heliopolis", "Dokki", "Mohandessin", "6th of October", "Smouha", "Mansoura", "Tanta", "Zamalek"]
STREETS = ["Tahrir Street", "El Nasr Road", "Gameat El Dowal", "Corniche El Nile", "Makram Ebeid", "Salah Salem", "Abbas El Akkad", "Port Said Street"]
EMAIL_DOMAINS = ["example.eg", "mail.test", "customer.invalid", "synthetic.local"]

TABLE_PREFIXES = {
    "telco_customer_profile": "CUST",
    "telco_kyc_identity_document": "KYC",
    "telco_sim_subscription": "SIM",
    "telco_device_inventory": "DEV",
    "telco_cdr_event": "CDR",
    "telco_network_location_event": "NLE",
    "eshop_customer_account": "ESH-CUST",
    "eshop_order_header": "ORD",
    "eshop_shipping_address": "ADDR",
    "eshop_merchant_profile": "MER",
    "fintech_wallet_account": "WAL",
    "fintech_payment_transaction": "PAY",
    "customer_support_interaction": "SUP",
    "sensitive_special_category_record": "SEN",
}


def _slug_name(i: int) -> str:
    return f"{FIRST_NAMES_EN[i % len(FIRST_NAMES_EN)].lower()}.{FAMILY_NAMES_EN[(i * 7) % len(FAMILY_NAMES_EN)].lower()}"


def _full_name_en(i: int) -> str:
    return f"{FIRST_NAMES_EN[i % len(FIRST_NAMES_EN)]} {FATHER_NAMES_EN[(i * 3) % len(FATHER_NAMES_EN)]} {FAMILY_NAMES_EN[(i * 7) % len(FAMILY_NAMES_EN)]}"


def _full_name_ar(i: int) -> str:
    return f"{FIRST_NAMES_AR[i % len(FIRST_NAMES_AR)]} {FATHER_NAMES_AR[(i * 3) % len(FATHER_NAMES_AR)]} {FAMILY_NAMES_AR[(i * 7) % len(FAMILY_NAMES_AR)]}"


def _egypt_mobile(i: int, e164: bool = True) -> str:
    prefixes = ["010", "011", "012", "015"]
    local10 = prefixes[i % len(prefixes)] + f"{10000000 + (i * 7919) % 90000000:08d}"
    return "+20" + local10[1:] if e164 else local10


def _landline(i: int) -> str:
    areas = ["02", "03", "040", "050", "055", "064", "088", "097"]
    area = areas[i % len(areas)]
    digits_needed = 10 - len(area)
    return area + f"{1000000 + (i * 3571) % 9000000:0{digits_needed}d}"[-digits_needed:]


def _email(i: int) -> str:
    return f"{_slug_name(i)}{i:04d}@{EMAIL_DOMAINS[i % len(EMAIL_DOMAINS)]}"


def _birth_date(i: int) -> date:
    return date(1965 + (i % 42), 1 + (i * 7) % 12, 1 + (i * 13) % 28)


def _egypt_national_id(i: int) -> str:
    dob = _birth_date(i)
    century = "2" if dob.year < 2000 else "3"
    yy = dob.year % 100
    gov_codes = ["01", "02", "03", "04", "11", "12", "13", "14", "15", "16", "17", "18", "19", "21", "22", "23", "24", "25", "26", "27", "28", "29", "31", "32", "33", "34", "35", "88"]
    gov = gov_codes[i % len(gov_codes)]
    seq = f"{1000 + (i * 17) % 9000:04d}"
    check = str((i * 7 + 3) % 10)
    return f"{century}{yy:02d}{dob.month:02d}{dob.day:02d}{gov}{seq}{check}"


def _imei(i: int) -> str:
    return f"49{(154203237000 + i) % 10**13:013d}"[-15:]


def _imsi(i: int) -> str:
    return "602" + ["01", "02", "03", "04"][i % 4] + f"{1000000000 + i:010d}"[-10:]


def _iccid(i: int) -> str:
    return "8920" + f"{1000000000000000 + i:016d}"


def _address(i: int) -> str:
    return f"{12 + i % 80} {STREETS[i % len(STREETS)]}, {DISTRICTS[i % len(DISTRICTS)]}, {GOVERNORATES[i % len(GOVERNORATES)]}"


def _geo(i: int) -> str:
    lat = 30.0444 + ((i % 1000) - 500) / 10000
    lon = 31.2357 + (((i * 3) % 1000) - 500) / 10000
    return f"{lat:.6f}, {lon:.6f}"


def _iban(i: int) -> str:
    return "EG" + f"{38 + i % 61:02d}" + f"{19 + i % 80:04d}" + f"{5000000000263180000 + i:019d}"[:23]


def _crypto(i: int) -> str:
    prefix = ["bc1q", "0x", "T"][i % 3]
    return prefix + hashlib.sha256(f"synthetic-secret-{i}".encode()).hexdigest()


def _timestamp(i: int) -> pd.Timestamp:
    return pd.Timestamp(datetime(2026, 1, 1, 8, 0, 0) + timedelta(minutes=i * 7))


def _semantic_value(table_name: str, column: Dict[str, Any], i: int, rng: random.Random) -> Any:
    """Create a deterministic synthetic value for one schema column."""
    name = column["name"].lower()
    sem = str(column.get("semantic_type") or "").upper()
    logical = str(column.get("logicalType") or "string").lower()

    if column.get("primaryKey") or name.endswith("_id"):
        prefix = TABLE_PREFIXES.get(table_name, "ID")
        return f"{prefix}-{i+1:08d}"
    if sem == "PERSON" or "full_name_en" in name:
        return _full_name_ar(i) if name.endswith("_ar") else _full_name_en(i)
    if name.endswith("_ar") or "arabic" in name:
        return _full_name_ar(i)
    if sem in {"EGYPTIAN_MOBILE", "MSISDN"} or "mobile" in name or "msisdn" in name:
        return _egypt_mobile(i, e164=(sem == "MSISDN" or "e164" in name or "primary" in name))
    if sem == "PHONE_NUMBER" or "phone" in name:
        return _landline(i) if i % 3 == 0 else _egypt_mobile(i, e164=False)
    if sem == "EMAIL_ADDRESS" or "email" in name:
        return _email(i)
    if sem == "EG_NATIONAL_ID" or "national_id" in name:
        return _egypt_national_id(i)
    if sem == "DATE_OF_BIRTH" or "birth" in name:
        return _birth_date(i)
    if sem == "EG_PASSPORT" or "passport" in name:
        return chr(65 + i % 26) + f"{10000000 + i:08d}"[-8:]
    if sem in {"EG_DRIVER_LICENSE", "DOCUMENT_NUMBER"} or "document_number" in name or "license" in name:
        return f"{sem.split('_')[-2][:3] if '_' in sem else 'DOC'}-{100000 + i}"
    if sem == "DOCUMENT_IMAGE_URI" or "image" in name or "artifact" in name:
        return f"s3://synthetic-pii-artifacts/{table_name}/{i+1:08d}.bin"
    if sem == "IMEI" or "imei" in name:
        return _imei(i)
    if sem == "IMSI" or "imsi" in name:
        return _imsi(i)
    if sem == "ICCID" or "iccid" in name:
        return _iccid(i)
    if sem == "MAC_ADDRESS" or "mac" in name:
        return ":".join(f"{(i * k + k) % 256:02x}" for k in range(1, 7))
    if sem == "IP_ADDRESS" or name in {"ip", "ip_address", "source_ip"}:
        return f"10.{i % 255}.{(i * 3) % 255}.{(i * 7) % 255}"
    if sem == "OTP" or "otp" in name:
        return f"{100000 + i % 900000:06d}"
    if sem == "CVV" or "cvv" in name:
        return f"{100 + i % 900:03d}"
    if sem == "CARD_EXPIRY" or "expiry" in name:
        return f"{1 + i % 12:02d}/{26 + i % 8}"
    if sem in {"CREDIT_CARD", "PAYMENT_CARD_TOKEN"} or "card" in name:
        return "627033" + f"{1000000000 + i:010d}" if "meeza" in name else "411111" + f"{1000000000 + i:010d}"
    if sem == "IBAN_CODE" or "iban" in name:
        return _iban(i)
    if sem == "EG_BANK_ACCOUNT" or "bank_account" in name:
        return f"{100000000000 + i:012d}"
    if sem == "INSTAPAY_ADDRESS" or "instapay" in name:
        return f"{_slug_name(i)}{i:03d}@instapay"
    if sem == "FAWRY_REFERENCE" or "fawry" in name:
        return f"FWY{202600000000 + i}"
    if sem == "CRYPTO" or "wallet_address" in name:
        return _crypto(i)
    if sem in {"LOCATION_ADDRESS", "SHIPPING_ADDRESS"} or "address" in name:
        return _address(i)
    if sem == "GOVERNORATE" or "governorate" in name:
        return GOVERNORATES[i % len(GOVERNORATES)]
    if sem in {"GEOLOCATION", "GPS_COORDINATES"} or "latitude" in name or "longitude" in name or "geo" in name:
        if "latitude" in name:
            return round(30.0444 + ((i % 1000) - 500) / 10000, 6)
        if "longitude" in name:
            return round(31.2357 + (((i * 3) % 1000) - 500) / 10000, 6)
        return _geo(i)
    if sem == "CELL_GLOBAL_IDENTITY" or "cell" in name:
        return f"602-{1 + i % 4:02d}-{100 + i % 60000}-{1000 + i % 50000}" if sem == "CELL_GLOBAL_IDENTITY" else str(1000 + i % 60000)
    if sem == "COOKIE_ID" or "cookie" in name:
        return hashlib.md5(f"cookie-{table_name}-{i}".encode()).hexdigest()
    if sem == "DEVICE_ADVERTISING_ID" or "advertising" in name:
        return hashlib.sha1(f"adid-{i}".encode()).hexdigest()[:32]
    if sem == "SOCIAL_MEDIA_HANDLE" or "social" in name:
        return f"https://facebook.com/{_slug_name(i)}"
    if sem == "TAX_ID" or "tax" in name:
        return f"{100000000 + i:09d}"
    if sem == "COMMERCIAL_REGISTRATION" or "commercial" in name:
        return f"CR-{100000 + i}"
    if sem == "TRACKING_NUMBER" or "tracking" in name:
        return f"EG-TRK-{2026000000 + i}"
    if sem == "FREE_TEXT" or "notes" in name or "message" in name or "description" in name or "transcript" in name:
        if "otp" in name or i % 5 == 0:
            return f"Your OTP is {100000 + i % 900000} for login to your account. Do not share it."
        return f"Customer {_egypt_mobile(i)} confirmed NID {_egypt_national_id(i)} and requested update for IMEI {_imei(i)}."
    if sem in {"HEALTH_DATA", "MEDICAL_RECORD_NUMBER"} or "medical" in name or "health" in name:
        return f"MRN-{100000 + i}"
    if sem in {"RELIGION", "POLITICAL_OPINION", "CHILDREN_DATA", "BIOMETRIC_IDENTIFIER"}:
        values = {
            "RELIGION": ["Muslim", "Christian", "Prefer not to say"],
            "POLITICAL_OPINION": ["Not collected", "Union member", "Campaign preference"],
            "CHILDREN_DATA": ["guardian_verified_minor", "school_delivery_contact", "child_profile_flag"],
            "BIOMETRIC_IDENTIFIER": ["face_template_hash_", "voiceprint_hash_", "fingerprint_hash_"],
        }
        base = values[sem][i % len(values[sem])]
        return base + (hashlib.sha256(str(i).encode()).hexdigest()[:12] if base.endswith("_") else "")
    if sem == "PCR_TEST_ID" or "pcr" in name:
        return f"PCR-{100000 + i}"
    if sem == "NRP" or "nationality" in name:
        return ["EGY", "SYR", "SDN", "JOR", "PSE"][i % 5]
    if "gender" in name:
        return ["M", "F", "Unspecified"][i % 3]
    if "status" in name:
        return ["active", "pending", "suspended", "closed"][i % 4]
    if "type" in name or "category" in name:
        return ["prepaid", "postpaid", "wallet", "card", "cash_on_delivery"][i % 5]
    if "amount" in name or logical in {"decimal", "double", "float"}:
        return round(25 + (i * 13.75) % 25000, 2)
    if logical in {"integer", "int", "long", "bigint"} or name.endswith("_count"):
        return int(1 + (i * 17) % 100000)
    if logical == "boolean" or name.endswith("_flag") or name.startswith("is_"):
        return bool(i % 2)
    if logical == "date":
        return date(2025 + (i % 2), 1 + (i % 12), 1 + (i % 28))
    if logical == "timestamp" or name.endswith("_at") or "timestamp" in name or "time" in name:
        return _timestamp(i)
    return f"{name}_{i+1:08d}"


def _expected_columns(table_name: str) -> List[str]:
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    return [c["name"] for c in TABLE_SCHEMAS[table_name]["columns"]]


def _coerce_dataframe(table_name: str, df: pd.DataFrame, validate_required: bool = True, strict_columns: bool = False, add_missing_columns: bool = True) -> pd.DataFrame:
    table = TABLE_SCHEMAS[table_name]
    expected = [c["name"] for c in table["columns"]]
    lower_to_actual = {str(col).lower(): col for col in df.columns}
    renamed = df.rename(columns={actual: expected_name for expected_name in expected for lower, actual in lower_to_actual.items() if lower == expected_name.lower()})

    missing = [c for c in expected if c not in renamed.columns]
    if validate_required:
        required_missing = [c["name"] for c in table["columns"] if c.get("required") and c["name"] not in renamed.columns]
        if required_missing:
            raise ValueError(f"CSV is missing required columns for table {table_name!r}: {required_missing}")
    if missing and not add_missing_columns:
        raise ValueError(f"CSV is missing expected columns for table {table_name!r}: {missing}")
    for col in missing:
        renamed[col] = pd.NA

    extra = [c for c in renamed.columns if c not in expected]
    if strict_columns and extra:
        raise ValueError(f"CSV contains unexpected columns for table {table_name!r}: {extra}")

    result = renamed[expected].copy()
    for col_meta in table["columns"]:
        col = col_meta["name"]
        logical = str(col_meta.get("logicalType") or "string").lower()
        if logical == "date":
            result[col] = pd.to_datetime(result[col], errors="coerce").dt.date
        elif logical == "timestamp":
            result[col] = pd.to_datetime(result[col], errors="coerce")
        elif logical == "boolean":
            result[col] = result[col].map(lambda x: pd.NA if pd.isna(x) else str(x).strip().lower() in {"true", "1", "yes", "y"})
        elif logical in {"integer", "int", "long", "bigint"}:
            result[col] = pd.to_numeric(result[col], errors="coerce").astype("Int64")
        elif logical in {"decimal", "double", "float"}:
            result[col] = pd.to_numeric(result[col], errors="coerce")
        else:
            result[col] = result[col].astype("string")
    return result


def _write_dataframe(df: pd.DataFrame, target_path: Union[str, Path], mode: str = "append", output_format: Optional[str] = None, index: bool = False) -> None:
    path = Path(target_path)
    fmt = (output_format or path.suffix.lstrip(".") or "csv").lower()
    if mode not in {"append", "overwrite", "errorifexists"}:
        raise ValueError("mode must be one of: 'append', 'overwrite', 'errorifexists'")
    if mode == "errorifexists" and path.exists():
        raise FileExistsError(f"Target path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append" and path.exists():
        if fmt == "csv":
            existing = pd.read_csv(path)
        elif fmt == "parquet":
            existing = pd.read_parquet(path)
        elif fmt in {"json", "jsonl"}:
            existing = pd.read_json(path, lines=(fmt == "jsonl"))
        else:
            raise ValueError(f"Unsupported output format: {fmt}")
        df = pd.concat([existing, df], ignore_index=True)

    if fmt == "csv":
        df.to_csv(path, index=index)
    elif fmt == "parquet":
        df.to_parquet(path, index=index)
    elif fmt == "json":
        df.to_json(path, orient="records", force_ascii=False, indent=2)
    elif fmt == "jsonl":
        df.to_json(path, orient="records", lines=True, force_ascii=False)
    else:
        raise ValueError(f"Unsupported output format: {fmt}")


def generate_table(table_name: str, num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic records for any configured table by name.

    Inputs:
        table_name: Exact table schema name, such as ``telco_customer_profile``.
        num_rows: Number of synthetic rows to create. Must be zero or positive.
        seed: Deterministic random seed reserved for future stochastic variants.

    Returns:
        A pandas DataFrame ordered according to the configured table schema.

    Example:
        >>> df = generate_table("telco_cdr_event", 1000, seed=42)
        >>> df.head()

    Agent-use hint:
        Use this dispatcher when the table name is selected dynamically by a
        workflow, benchmark runner, or natural-language agent instruction.
    """
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    if num_rows < 0:
        raise ValueError("num_rows must be zero or positive")
    rng = random.Random(seed)
    table = TABLE_SCHEMAS[table_name]
    rows = []
    for i in range(num_rows):
        rows.append({c["name"]: _semantic_value(table_name, c, i, rng) for c in table["columns"]})
    return pd.DataFrame(rows, columns=_expected_columns(table_name))


def load_table_csv(
    table_name: str,
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records for any configured table, validate shape, and optionally persist.

    Inputs:
        table_name: Exact table schema name.
        csv_path: Local or mounted CSV path accepted by ``pandas.read_csv``.
        existing_df: Optional DataFrame to concatenate before returning. This is
            useful when an agent has an in-memory table and wants to add records.
        target_path: Optional output file. If provided, the resulting DataFrame
            is written to CSV, Parquet, JSON, or JSONL depending on extension or
            ``output_format``.
        mode: ``append``, ``overwrite``, or ``errorifexists`` when writing to
            ``target_path``. For append mode, an existing target file is read and
            concatenated before writing.
        output_format: Optional explicit format: ``csv``, ``parquet``, ``json``,
            or ``jsonl``.
        validate_required: Raise an error if required schema columns are absent.
        strict_columns: Raise an error for unexpected columns.
        add_missing_columns: Add nullable missing schema columns as ``pd.NA``.
        read_csv_kwargs: Optional dictionary forwarded to ``pandas.read_csv``.

    Returns:
        A pandas DataFrame containing schema-ordered, coerced records.

    Example:
        >>> df = load_table_csv("eshop_order_header", "orders.csv", target_path="golden_orders.csv")

    Agent-use hint:
        For user-uploaded files, keep ``validate_required=True``. Use
        ``strict_columns=True`` only when the agent is checking exact schema
        compliance instead of allowing harmless source-system extra columns.
    """
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    kwargs = read_csv_kwargs or {}
    raw = pd.read_csv(csv_path, **kwargs)
    loaded = _coerce_dataframe(table_name, raw, validate_required=validate_required, strict_columns=strict_columns, add_missing_columns=add_missing_columns)
    if existing_df is not None:
        existing = _coerce_dataframe(table_name, existing_df, validate_required=False, strict_columns=False, add_missing_columns=True)
        loaded = pd.concat([existing, loaded], ignore_index=True)
    if target_path is not None:
        _write_dataframe(loaded, target_path=target_path, mode=mode, output_format=output_format)
    return loaded


def get_table_columns(table_name: str) -> List[str]:
    """Return the expected column order for one configured table."""
    return _expected_columns(table_name)


def describe_table(table_name: str) -> Dict[str, Any]:
    """Return a deep copy of table metadata, including column PII semantic labels."""
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(f"Unknown table_name={table_name!r}. Expected one of: {sorted(TABLE_SCHEMAS)}")
    return deepcopy(TABLE_SCHEMAS[table_name])


def list_tables() -> List[str]:
    """Return all configured table names in sorted order."""
    return sorted(TABLE_SCHEMAS)


def pii_columns(table_name: str) -> List[Dict[str, Any]]:
    """Return metadata for columns marked as PII in one table."""
    return [deepcopy(c) for c in describe_table(table_name)["columns"] if c.get("is_pii")]

__FUNCTION_DEFINITIONS__

GENERATOR_FUNCTIONS: Dict[str, Any] = {
__GENERATOR_MAP__
}

CSV_LOADER_FUNCTIONS: Dict[str, Any] = {
__LOADER_MAP__
}
'''

function_blocks = []
generator_map_lines = []
loader_map_lines = []

for table_name, meta in schemas.items():
    business_name = meta.get('businessName', table_name)
    description = meta.get('description', '')
    gen_name = f"generate_{table_name}"
    load_name = f"load_{table_name}_csv"
    function_blocks.append(f'''

def {gen_name}(num_rows: int, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic pandas DataFrame for ``{table_name}``.

    Table definition:
        {business_name}. {description}

    Inputs:
        num_rows: Number of synthetic rows to generate. Use a small value such
            as 10 for unit tests and a larger value such as 10_000 for benchmark
            runs. Must be zero or positive.
        seed: Deterministic seed. The current generator is mostly deterministic
            by row index, but the seed is included for repeatable agent workflows
            and future stochastic variants.

    Returns:
        pandas.DataFrame with the exact column order defined for ``{table_name}``.

    Example:
        >>> df = {gen_name}(num_rows=100, seed=42)
        >>> df.columns.tolist() == get_table_columns("{table_name}")
        True

    Agent-use hint:
        Call this function when an agent needs realistic synthetic golden data
        for the ``{table_name}`` scenario before passing the DataFrame to a PII
        scanner, anonymizer, profiler, classifier, or data-quality test.
    """
    return generate_table("{table_name}", num_rows=num_rows, seed=seed)


def {load_name}(
    csv_path: Union[str, Path],
    *,
    existing_df: Optional[pd.DataFrame] = None,
    target_path: Optional[Union[str, Path]] = None,
    mode: str = "append",
    output_format: Optional[str] = None,
    validate_required: bool = True,
    strict_columns: bool = False,
    add_missing_columns: bool = True,
    read_csv_kwargs: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Load CSV records and add them to the pandas representation of ``{table_name}``.

    Table definition:
        {business_name}. {description}

    Inputs:
        csv_path: CSV file path containing rows to load for this table.
        existing_df: Optional existing pandas DataFrame. If provided, the loaded
            CSV rows are schema-normalized and appended to this DataFrame.
        target_path: Optional file path where the combined result should be
            written. The extension or ``output_format`` controls CSV, Parquet,
            JSON, or JSONL output.
        mode: Write behavior for ``target_path``. Use ``append`` to add records
            to an existing file, ``overwrite`` to replace it, or ``errorifexists``
            for defensive agent workflows.
        output_format: Optional explicit output format: ``csv``, ``parquet``,
            ``json``, or ``jsonl``.
        validate_required: When true, missing required columns raise an error.
        strict_columns: When true, unexpected source columns raise an error.
        add_missing_columns: When true, missing optional columns are added as
            ``pd.NA`` to preserve the schema contract.
        read_csv_kwargs: Optional dictionary passed to ``pandas.read_csv``.

    Returns:
        pandas.DataFrame containing schema-ordered, type-coerced records for
        ``{table_name}``.

    Example:
        >>> df = {load_name}("{table_name}.csv", target_path="out/{table_name}.csv", mode="overwrite")

    Agent-use hint:
        Use this function after a user uploads CSV records for ``{table_name}``.
        Keep ``validate_required=True`` to catch incomplete input. Enable
        ``strict_columns=True`` only when the benchmark should fail files with
        extra source columns.
    """
    return load_table_csv(
        "{table_name}",
        csv_path=csv_path,
        existing_df=existing_df,
        target_path=target_path,
        mode=mode,
        output_format=output_format,
        validate_required=validate_required,
        strict_columns=strict_columns,
        add_missing_columns=add_missing_columns,
        read_csv_kwargs=read_csv_kwargs,
    )
''')
    generator_map_lines.append(f'    "{table_name}": {gen_name},')
    loader_map_lines.append(f'    "{table_name}": {load_name},')

module_code = module_template.replace('__TABLE_SCHEMAS_REPR__', repr(schemas))
module_code = module_code.replace('__FUNCTION_DEFINITIONS__', '\n'.join(function_blocks))
module_code = module_code.replace('__GENERATOR_MAP__', '\n'.join(generator_map_lines))
module_code = module_code.replace('__LOADER_MAP__', '\n'.join(loader_map_lines))

(OUT_DIR / 'eg_pii_pandas_generators.py').write_text(module_code, encoding='utf-8')

readme_rows = []
for table_name, meta in schemas.items():
    pii_count = sum(1 for c in meta['columns'] if c.get('is_pii'))
    readme_rows.append(f"| `{table_name}` | {len(meta['columns'])} | {pii_count} | `{f'generate_{table_name}'}` | `{f'load_{table_name}_csv'}` |")

readme = f'''# Egyptian PII Golden Data Generators for pandas

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
{chr(10).join(readme_rows)}

## CSV loader behavior

The CSV loaders read a source CSV, normalize case-insensitive column names to the expected schema, add missing optional columns as `pd.NA` when requested, coerce common logical types, optionally append to an in-memory `existing_df`, and optionally write the combined result to CSV, Parquet, JSON, or JSONL.

The recommended agent defaults are `validate_required=True`, `strict_columns=False`, and `add_missing_columns=True`. These defaults make user uploads tolerant of harmless extra operational columns while still protecting required schema fields.

## Important safety note

All generated records are synthetic. They are intended for testing PII tools and should not be confused with real Egyptian customer, telecom, e-commerce, or payment data.
'''
(OUT_DIR / 'README.md').write_text(readme, encoding='utf-8')

examples = '''"""Agent-oriented pandas examples for Egyptian PII golden data."""

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
'''
(OUT_DIR / 'agent_usage_examples.py').write_text(examples, encoding='utf-8')
(OUT_DIR / 'requirements.txt').write_text('pandas>=2.0\npyarrow>=14.0\n', encoding='utf-8')

validator = '''import importlib.util
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
'''
(OUT_DIR / 'validate_pii_pandas_generators.py').write_text(validator, encoding='utf-8')

print(OUT_DIR / 'eg_pii_pandas_generators.py')
print(OUT_DIR / 'README.md')
print(OUT_DIR / 'requirements.txt')
print(OUT_DIR / 'validate_pii_pandas_generators.py')
