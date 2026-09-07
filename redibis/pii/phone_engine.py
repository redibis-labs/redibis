"""libphonenumber-backed phone validation (core dependency)."""

from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberType

_PHONE_NAME_TOKENS = frozenset({
    "phone", "mobile", "contact", "tel", "fax", "whatsapp", "msisdn", "number",
})
_NETWORK_NAME_RE = re.compile(
    r"(?i)cell_global|(^|_)lac$|tac_lte|\bcgi_identity\b",
)
_NULLISH = frozenset({"NONE", "NAN", "N/A", "NULL"})
_DATE_LIKE = re.compile(
    r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{1,2}[-/]\d{1,2}[-/]\d{4}$",
)
_ID_NAME_TOKENS = frozenset({
    "id", "account", "order", "transaction", "reference", "ref", "code", "key", "seq",
})


def name_suggests_identifier(column_name: str) -> bool:
    """True when the column name reads as an identifier key (not telephony)."""
    blob = (column_name or "").lower()
    tokens = set(re.split(r"[^a-z0-9]+", blob))
    if blob.endswith("_id") or "id" in tokens:
        return True
    return bool(tokens & _ID_NAME_TOKENS)


def _looks_like_date(val: str) -> bool:
    return bool(_DATE_LIKE.match(str(val).strip()))


def name_suggests_network_identifier(col_name: str) -> bool:
    """True for CGI / LAC / TAC columns — never treat as telephony by name."""
    return bool(_NETWORK_NAME_RE.search(col_name or ""))


def name_has_phone_token(col_name: str) -> bool:
    """True when the column name hints at telephony (§3.3 union gate)."""
    if name_suggests_network_identifier(col_name):
        return False
    blob = (col_name or "").lower()
    tokens = set(re.split(r"[^a-z0-9]+", blob))
    return bool(tokens & _PHONE_NAME_TOKENS) or any(t in blob for t in _PHONE_NAME_TOKENS)


def _is_non_null_value(val) -> bool:
    if val is None:
        return False
    s = str(val).strip()
    return bool(s) and s.upper() not in _NULLISH


def _value_structural_numeric(val) -> bool:
    """Digit-dominant string with ~7–15 digits (allows + - space parentheses)."""
    s = str(val).strip()
    if not s:
        return False
    if not re.match(r"^[\d+\-\s().]+$", s):
        return False
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7 or len(digits) > 15:
        return False
    compact = re.sub(r"\s", "", s)
    digit_chars = sum(c.isdigit() for c in compact)
    return digit_chars / max(len(compact), 1) >= 0.70


def structural_numeric(values: list) -> bool:
    """True when a majority of non-null sample values look phone-shaped."""
    checked = 0
    hits = 0
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        checked += 1
        if _value_structural_numeric(raw):
            hits += 1
    if not checked:
        return False
    return (hits / checked) >= 0.50


def any_value_starts_with_plus(values: list) -> bool:
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        if str(raw).strip().startswith("+"):
            return True
    return False


def should_run_phone(
    col_name: str,
    values: list,
    *,
    regex_gliner_conf: float = 0.0,
    conf_thr: float = 0.10,
) -> bool:
    """
    Cheap union gate — run libphonenumber when ANY independent signal holds (§3.3).

    ``regex_gliner_conf`` is an optional extra trigger only; never the sole gate.
    """
    if name_has_phone_token(col_name):
        return True
    if structural_numeric(values):
        return True
    if any_value_starts_with_plus(values):
        return True
    if regex_gliner_conf > conf_thr:
        return True
    return False


def phone_rates(values: list, *, region: str = "EG") -> dict:
    """Column-level libphonenumber stats (international + default region)."""
    valid = mobile = 0
    regions: dict[str, int] = {}
    for raw in values:
        if not _is_non_null_value(raw):
            continue
        text = str(raw).strip()
        if _looks_like_date(text):
            continue
        try:
            n = phonenumbers.parse(text, region)
            if phonenumbers.is_valid_number(n):
                valid += 1
                t = phonenumbers.number_type(n)
                if t in (
                    PhoneNumberType.MOBILE,
                    PhoneNumberType.FIXED_LINE_OR_MOBILE,
                ):
                    mobile += 1
                rc = phonenumbers.region_code_for_number(n)
                if rc:
                    regions[rc] = regions.get(rc, 0) + 1
        except Exception:
            continue

    total = max(len(values), 1)
    return {
        "available": True,
        "valid_rate": valid / total,
        "mobile_rate": mobile / total,
        "regions": regions,
    }


def egypt_mobile_rate(values: list[str], *, region: str = "EG") -> dict:
    """Back-compat alias — prefer ``phone_rates``."""
    return phone_rates(values, region=region)
