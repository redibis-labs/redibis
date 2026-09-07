"""
redibis.scan.profile_metrics
==============================
Pure profiling metrics for the evidence bundle (PLAN §4.1-4.8). Every function
takes a plain Python ``list`` of already-extracted column values (as produced
by a caller that holds the DataFrame — e.g. ``report_bundle.py``) and returns
a JSON-safe ``dict``. No I/O, no pandas at module scope; a column's values
have already been pulled out of the frame by the time they reach this module.

Groups 4.1-4.4 (``counts``, ``length``, ``format_masks``, ``charset``) always
apply to a string-shaped column. Groups 4.5-4.7 (``numeric``, ``temporal``,
``frequency``) return ``None`` — and the caller omits the key — when the
group does not apply.
"""

from __future__ import annotations

import math
import re
import statistics
import string
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, Optional

#: Arabic-Indic + Extended Arabic-Indic digits -> ASCII digits. Mirrors
#: ``redibis/pii/national_id_egypt.py::_ARABIC_INDIC`` (duplicated — this
#: module stays a pure leaf with no redibis imports).
_ARABIC_INDIC = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)

_MASK_TABLE = {ord(c): "9" for c in "0123456789"}
_MASK_TABLE.update({ord(c): "A" for c in string.ascii_letters})

_ARABIC_RANGE_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_LATIN_RANGE_RE = re.compile(r"[A-Za-z]")
_NUMERIC_ONLY_RE = re.compile(r"^[0-9]+$")
_WHITESPACE_RE = re.compile(r"^\s+$")

_NULLISH_STRINGS = frozenset({"", "none", "null", "nan", "n/a", "na"})


def _is_null(v: Any) -> bool:
    if v is None:
        return True
    try:
        if isinstance(v, float) and math.isnan(v):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _to_str(v: Any) -> str:
    return v if isinstance(v, str) else str(v)


def _non_null_strings(values: list) -> list[str]:
    return [_to_str(v) for v in values if not _is_null(v)]


def _mask(value: str, *, max_len: int = 64) -> str:
    normalized = value.translate(_ARABIC_INDIC)
    return normalized[:max_len].translate(_MASK_TABLE)


# ─────────────────────────────────────────────────────────────────────────────
# 4.1 Counts and completeness
# ─────────────────────────────────────────────────────────────────────────────

def profile_counts(values: list, *, population_rows: Optional[int] = None) -> dict:
    sampled_rows = len(values)
    non_null_vals = [v for v in values if not _is_null(v)]
    non_null = len(non_null_vals)
    null = sampled_rows - non_null
    strs = [_to_str(v) for v in non_null_vals]
    empty_string = sum(1 for s in strs if s == "")
    whitespace_only = sum(1 for s in strs if s != "" and _WHITESPACE_RE.match(s))
    distinct_vals = set(strs)
    distinct = len(distinct_vals)
    duplicate_rows = max(0, non_null - distinct)
    null_rate = (null / sampled_rows) if sampled_rows else 0.0
    distinct_rate = (distinct / non_null) if non_null else 0.0
    return {
        "sampled_rows": sampled_rows,
        "population_rows": population_rows if population_rows is not None else sampled_rows,
        "non_null": non_null,
        "null": null,
        "null_rate": null_rate,
        "empty_string": empty_string,
        "whitespace_only": whitespace_only,
        "distinct": distinct,
        "distinct_rate": distinct_rate,
        "duplicate_rows": duplicate_rows,
        "is_candidate_key": non_null > 0 and distinct == non_null and null == 0,
        "constant": non_null > 0 and distinct == 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.2 Length
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return float(d0 + d1)


def profile_length(values: list, *, bins: int = 20) -> dict:
    strs = _non_null_strings(values)
    lengths = sorted(len(s) for s in strs)
    if not lengths:
        return {
            "min": None, "max": None, "mean": None, "median": None, "stddev": 0.0,
            "p05": None, "p25": None, "p50": None, "p75": None, "p95": None,
            "histogram": [], "distinct_lengths": 0, "fixed_width": False,
        }
    distinct_lengths = sorted(set(lengths))
    counts: dict[int, int] = {}
    for length in lengths:
        counts[length] = counts.get(length, 0) + 1
    histogram = [
        {"bucket": str(length), "count": counts[length]} for length in distinct_lengths
    ]
    return {
        "min": lengths[0],
        "max": lengths[-1],
        "mean": statistics.fmean(lengths),
        "median": statistics.median(lengths),
        "stddev": statistics.pstdev(lengths) if len(lengths) > 1 else 0.0,
        "p05": _percentile(lengths, 0.05),
        "p25": _percentile(lengths, 0.25),
        "p50": _percentile(lengths, 0.50),
        "p75": _percentile(lengths, 0.75),
        "p95": _percentile(lengths, 0.95),
        "histogram": histogram,
        "distinct_lengths": len(distinct_lengths),
        "fixed_width": len(distinct_lengths) == 1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.3 Format masks
# ─────────────────────────────────────────────────────────────────────────────

def profile_format_masks(values: list, *, top_n: int = 10) -> dict:
    strs = _non_null_strings(values)
    total = len(strs)
    if total == 0:
        return {"top": [], "distinct_masks": 0, "dominant_mask_rate": 0.0}
    counts: dict[str, int] = {}
    for s in strs:
        m = _mask(s)
        counts[m] = counts.get(m, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top = [
        {"mask": mask, "count": count, "rate": count / total}
        for mask, count in ordered[:top_n]
    ]
    dominant_rate = ordered[0][1] / total if ordered else 0.0
    return {"top": top, "distinct_masks": len(counts), "dominant_mask_rate": dominant_rate}


# ─────────────────────────────────────────────────────────────────────────────
# 4.4 Character composition
# ─────────────────────────────────────────────────────────────────────────────

def profile_charset(values: list) -> dict:
    strs = _non_null_strings(values)
    total = len(strs)
    if total == 0:
        return {
            "digits_only_rate": 0.0, "alpha_only_rate": 0.0, "alphanumeric_rate": 0.0,
            "has_punctuation_rate": 0.0, "has_whitespace_rate": 0.0,
            "leading_zeros_rate": 0.0,
            "scripts": {"latin": 0.0, "arabic": 0.0, "numeric": 0.0, "mixed": 0.0},
            "unicode_categories": {},
        }
    digits_only = 0
    alpha_only = 0
    alnum_only = 0
    has_punct = 0
    has_ws = 0
    leading_zero = 0
    latin_ct = 0
    arabic_ct = 0
    numeric_ct = 0
    mixed_ct = 0
    category_counts: dict[str, int] = {}

    for s in strs:
        normalized = s.translate(_ARABIC_INDIC)
        if normalized.isdigit():
            digits_only += 1
        if s.isalpha():
            alpha_only += 1
        if s.isalnum():
            alnum_only += 1
        if any(not (c.isalnum() or c.isspace()) for c in s):
            has_punct += 1
        if any(c.isspace() for c in s):
            has_ws += 1
        if normalized.isdigit() and normalized.startswith("0") and len(normalized) > 1:
            leading_zero += 1

        has_latin = bool(_LATIN_RANGE_RE.search(s))
        has_arabic = bool(_ARABIC_RANGE_RE.search(s))
        has_numeric = bool(normalized.isdigit())
        script_hits = sum([has_latin, has_arabic, has_numeric])
        if script_hits > 1:
            mixed_ct += 1
        elif has_latin:
            latin_ct += 1
        elif has_arabic:
            arabic_ct += 1
        elif has_numeric:
            numeric_ct += 1

        for c in s:
            cat = unicodedata.category(c)
            category_counts[cat] = category_counts.get(cat, 0) + 1

    total_chars = sum(category_counts.values()) or 1
    return {
        "digits_only_rate": digits_only / total,
        "alpha_only_rate": alpha_only / total,
        "alphanumeric_rate": alnum_only / total,
        "has_punctuation_rate": has_punct / total,
        "has_whitespace_rate": has_ws / total,
        "leading_zeros_rate": leading_zero / total,
        "scripts": {
            "latin": latin_ct / total,
            "arabic": arabic_ct / total,
            "numeric": numeric_ct / total,
            "mixed": mixed_ct / total,
        },
        "unicode_categories": {cat: n / total_chars for cat, n in category_counts.items()},
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.5 Numeric
# ─────────────────────────────────────────────────────────────────────────────

def _try_float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            if isinstance(v, float) and math.isnan(v):
                return None
        except (TypeError, ValueError):
            pass
        return float(v)
    if isinstance(v, str):
        s = v.strip().translate(_ARABIC_INDIC)
        if not s or s.lower() in _NULLISH_STRINGS:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def profile_numeric(values: list, *, bins: int = 20, min_parse_rate: float = 0.5) -> Optional[dict]:
    non_null = [v for v in values if not _is_null(v)]
    if not non_null:
        return None
    parsed = [f for f in (_try_float(v) for v in non_null) if f is not None]
    if not parsed or (len(parsed) / len(non_null)) < min_parse_rate:
        return None

    parsed_sorted = sorted(parsed)
    n = len(parsed_sorted)
    total = sum(parsed_sorted)
    mean = total / n
    median = statistics.median(parsed_sorted)
    stddev = statistics.pstdev(parsed_sorted) if n > 1 else 0.0
    variance = statistics.pvariance(parsed_sorted) if n > 1 else 0.0
    quantiles = {
        "p01": _percentile(parsed_sorted, 0.01),
        "p05": _percentile(parsed_sorted, 0.05),
        "p25": _percentile(parsed_sorted, 0.25),
        "p50": _percentile(parsed_sorted, 0.50),
        "p75": _percentile(parsed_sorted, 0.75),
        "p95": _percentile(parsed_sorted, 0.95),
        "p99": _percentile(parsed_sorted, 0.99),
    }
    lo = parsed_sorted[0]
    hi = parsed_sorted[-1]
    if hi > lo:
        edges = [lo + (hi - lo) * i / bins for i in range(bins + 1)]
        counts = [0] * bins
        for v in parsed_sorted:
            idx = int((v - lo) / (hi - lo) * bins)
            idx = min(idx, bins - 1)
            counts[idx] += 1
    else:
        edges = [lo, hi]
        counts = [n]

    parsed_count = n
    zero_count = sum(1 for v in parsed_sorted if v == 0)
    negative_count = sum(1 for v in parsed_sorted if v < 0)

    if stddev > 0:
        skewness = sum(((v - mean) / stddev) ** 3 for v in parsed_sorted) / n
        kurtosis = sum(((v - mean) / stddev) ** 4 for v in parsed_sorted) / n - 3
    else:
        skewness = 0.0
        kurtosis = 0.0

    q1, q3 = quantiles["p25"], quantiles["p75"]
    iqr = q3 - q1
    lo_fence, hi_fence = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    outliers_iqr = sum(1 for v in parsed_sorted if v < lo_fence or v > hi_fence)

    integer_valued = sum(1 for v in parsed_sorted if float(v).is_integer())
    decimal_places = []
    for v in parsed_sorted:
        s = repr(v)
        if "." in s:
            decimal_places.append(len(s.split(".", 1)[1].rstrip("0")))
        else:
            decimal_places.append(0)

    return {
        "min": lo, "max": hi, "sum": total,
        "mean": mean, "median": median, "stddev": stddev, "variance": variance,
        "quantiles": quantiles,
        "histogram": {"bins": bins, "edges": edges, "counts": counts},
        "parsed_count": parsed_count,
        "zero_count": zero_count, "negative_count": negative_count,
        "skewness": skewness, "kurtosis": kurtosis,
        "outliers_iqr": outliers_iqr, "outlier_rate": outliers_iqr / n,
        "integer_valued_rate": integer_valued / n,
        "decimal_places_median": statistics.median(decimal_places) if decimal_places else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.6 Temporal
# ─────────────────────────────────────────────────────────────────────────────

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
)


def _try_datetime(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        iso_candidate = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(iso_candidate)
        except ValueError:
            pass
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def profile_temporal(values: list, *, min_parse_rate: float = 0.5) -> Optional[dict]:
    non_null = [v for v in values if not _is_null(v)]
    if not non_null:
        return None
    parsed = [dt for dt in (_try_datetime(v) for v in non_null) if dt is not None]
    if not parsed or (len(parsed) / len(non_null)) < min_parse_rate:
        return None

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt

    naive = [_naive(dt) for dt in parsed]
    lo, hi = min(naive), max(naive)
    range_days = (hi - lo).days

    monotonic_steps = sum(
        1 for a, b in zip(naive, naive[1:]) if b >= a
    )
    monotonic_rate = (monotonic_steps / (len(naive) - 1)) if len(naive) > 1 else 1.0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    future_count = sum(1 for dt in naive if dt > now)

    epoch_like = all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null
    )

    has_sub_minute = any(dt.second or dt.microsecond for dt in naive)
    has_time = any(dt.hour or dt.minute for dt in naive)
    granularity = "second" if has_sub_minute else ("minute" if has_time else "day")

    by_year: dict[str, int] = {}
    for dt in naive:
        key = str(dt.year)
        by_year[key] = by_year.get(key, 0) + 1

    return {
        "min": lo.isoformat(), "max": hi.isoformat(), "range_days": range_days,
        "monotonic_rate": monotonic_rate, "future_count": future_count,
        "epoch_like": epoch_like, "granularity": granularity, "by_year": by_year,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.7 Categorical / frequency
# ─────────────────────────────────────────────────────────────────────────────

def profile_frequency(values: list, *, top_n: int = 20, categorical_threshold: int = 50) -> dict:
    strs = _non_null_strings(values)
    total = len(strs)
    if total == 0:
        return {
            "top_values": [], "top_n": top_n, "entropy_bits": 0.0,
            "normalized_entropy": 0.0, "is_categorical": False,
            "categorical_threshold": categorical_threshold, "coverage_of_top_10": 0.0,
        }
    counts: dict[str, int] = {}
    for s in strs:
        counts[s] = counts.get(s, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_values = [
        {"value": value, "count": count, "rate": count / total}
        for value, count in ordered[:top_n]
    ]
    probabilities = [count / total for count in counts.values()]
    entropy_bits = -sum(p * math.log2(p) for p in probabilities if p > 0)
    # Normalized against log2(total), not log2(distinct) — a column with a few
    # dominant values scores low even if it happens to have many distinct
    # values in its long tail (PLAN §4.7: 1.0 = every value unique).
    normalized_entropy = entropy_bits / math.log2(total) if total > 1 else 0.0
    coverage_of_top_10 = sum(count for _, count in ordered[:10]) / total
    return {
        "top_values": top_values,
        "top_n": top_n,
        "entropy_bits": entropy_bits,
        "normalized_entropy": normalized_entropy,
        "is_categorical": len(counts) <= categorical_threshold,
        "categorical_threshold": categorical_threshold,
        "coverage_of_top_10": coverage_of_top_10,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.8 Per-engine provenance
# ─────────────────────────────────────────────────────────────────────────────

def profile_by_engine(*, ran_engines: dict[str, dict]) -> dict:
    """``{engine_id: {ran, duration_ms, metrics}}`` — caller supplies which
    engine produced which metric groups; this just shapes the block."""
    return dict(ran_engines)
