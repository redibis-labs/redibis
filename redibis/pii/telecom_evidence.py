"""Telecom column evidence profiles for deep-scan LLM reconciliation."""

from __future__ import annotations

from typing import Any, Optional

from redibis.pii.phone_engine import phone_rates
from redibis.pii.telecom_signals import (
    checksum_pass_rates,
    compute_msisdn_valid_rate,
    length_distribution,
    msisdn_format_breakdown,
    prefix_histogram,
)

_DEFAULT_PREFIXES = ("010", "011", "012", "015")


def build_telecom_evidence(
    col: str,
    values: list[Any],
    *,
    sample_policy: str = "raw",
    sample_n: int = 10,
    prefixes: tuple[str, ...] | list[str] | None = None,
    use_phonenumbers: bool = True,
) -> dict[str, Any]:
    """
    Build a per-column telecom evidence dict (md+json payload).

    Masking-agnostic: ``sample_policy`` is recorded only. When ``masked``,
    the caller must pass already-masked ``values`` — this function never masks.
    """
    pref = tuple(prefixes or _DEFAULT_PREFIXES)
    str_values = [str(v) for v in values if v is not None and str(v).strip()]
    non_null = len(str_values)
    unique = len(set(str_values)) if str_values else 0

    msisdn_rate = compute_msisdn_valid_rate(str_values, pref)
    phone_stats = phone_rates(str_values, region="EG") if use_phonenumbers else {"available": False}

    sample = list(str_values[:sample_n])

    return {
        "column": col,
        "sample_policy": sample_policy,
        "sample": sample,
        "msisdn_valid_rate": round(msisdn_rate, 4),
        "msisdn_format_breakdown": msisdn_format_breakdown(str_values),
        "prefix_histogram": {
            k: round(v, 4) for k, v in prefix_histogram(str_values, pref).items()
        },
        "length_distribution": length_distribution(str_values),
        "checksum_pass_rates": {
            k: round(v, 4) for k, v in checksum_pass_rates(str_values).items()
        },
        "phonenumbers": {
            k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in phone_stats.items()
        },
        "null_rate": round(1 - (non_null / max(len(values), 1)), 4),
        "uniqueness": round(unique / max(non_null, 1), 4),
        "row_count": len(values),
    }
