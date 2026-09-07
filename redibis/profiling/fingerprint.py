"""
redibis.profiling.fingerprint
=============================
PII-safe structural column fingerprints for governance similarity matching.

Value-free aggregates only — raw samples are never retained in the fingerprint.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import pandas as pd

FEATURE_SPEC_VERSION = "fp-v1"
_PATTERN_K_ANON = 20  # k-anonymity threshold for pattern masks

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def _value_mask(value: str) -> str:
    """Map a string to a token-shape mask (digit→D, alpha→A, keep punct)."""
    out: list[str] = []
    for ch in value:
        if ch.isdigit():
            out.append("D")
        elif ch.isascii() and ch.isalpha():
            out.append("A" if ch.islower() else "a" if ch.isupper() else "A")
        else:
            out.append(ch)
    return "".join(out)


def _char_fractions(values: pd.Series) -> dict[str, float]:
    """Compute character-class fractions over string values."""
    if values.empty:
        return {
            "frac_digit": 0.0,
            "frac_alpha": 0.0,
            "frac_upper": 0.0,
            "frac_space": 0.0,
            "frac_punct": 0.0,
            "frac_arabic": 0.0,
        }
    total = 0
    digit = alpha = upper = space = punct = arabic = 0
    for v in values:
        s = str(v)
        for ch in s:
            total += 1
            if ch.isdigit():
                digit += 1
            elif ch.isascii() and ch.isalpha():
                alpha += 1
                if ch.isupper():
                    upper += 1
            elif ch.isspace():
                space += 1
            elif _ARABIC_RE.match(ch):
                arabic += 1
            else:
                punct += 1
    if total == 0:
        return {
            "frac_digit": 0.0,
            "frac_alpha": 0.0,
            "frac_upper": 0.0,
            "frac_space": 0.0,
            "frac_punct": 0.0,
            "frac_arabic": 0.0,
        }
    return {
        "frac_digit": digit / total,
        "frac_alpha": alpha / total,
        "frac_upper": upper / total if alpha else 0.0,
        "frac_space": space / total,
        "frac_punct": punct / total,
        "frac_arabic": arabic / total,
    }


def _length_entropy(lengths: pd.Series) -> float:
    if lengths.empty:
        return 0.0
    counts = lengths.value_counts(normalize=True)
    ent = 0.0
    for p in counts:
        if p > 0:
            ent -= p * math.log2(p)
    return round(ent, 6)


def _pattern_masks(values: pd.Series, k: int = _PATTERN_K_ANON) -> list[tuple[str, float]]:
    """Top-k value masks with k-anonymity folding."""
    if values.empty:
        return []
    masks = [_value_mask(str(v)) for v in values]
    counts = Counter(masks)
    total = len(masks)
    kept: list[tuple[str, float]] = []
    other_count = 0
    for mask, cnt in counts.most_common():
        if cnt >= k:
            kept.append((mask, round(cnt / total, 4)))
        else:
            other_count += cnt
    if other_count:
        kept.append(("other", round(other_count / total, 4)))
    return kept[:10]


def _percentile(series: pd.Series, q: float) -> int:
    if series.empty:
        return 0
    return int(series.quantile(q))


@dataclass
class ColumnFingerprint:
    """Value-free structural fingerprint for a single column."""

    column: str
    dtype: str
    # length (chars)
    len_min: int = 0
    len_max: int = 0
    len_mean: float = 0.0
    len_std: float = 0.0
    len_p50: int = 0
    len_p90: int = 0
    # character composition (fractions 0–1)
    frac_digit: float = 0.0
    frac_alpha: float = 0.0
    frac_upper: float = 0.0
    frac_space: float = 0.0
    frac_punct: float = 0.0
    frac_arabic: float = 0.0
    # structure
    distinct_ratio: float = 0.0
    is_unique: bool = False
    is_constant: bool = False
    null_rate: float = 0.0
    has_nulls: bool = False
    empty_rate: float = 0.0
    token_count_mean: float = 0.0
    len_entropy: float = 0.0
    # pattern signature — top-k value MASKS, k-anonymized
    pattern_masks: list[tuple[str, float]] = field(default_factory=list)
    # numeric shape — ONLY when dtype is numeric AND column is NOT pii
    num_min: Optional[float] = None
    num_max: Optional[float] = None
    num_mean: Optional[float] = None
    num_std: Optional[float] = None
    # provenance
    feature_spec_version: str = FEATURE_SPEC_VERSION
    is_pii_reduced: bool = False
    stats_source: str = "sample"
    sample_size: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pattern_masks"] = [list(m) for m in self.pattern_masks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ColumnFingerprint:
        masks = d.get("pattern_masks") or []
        parsed_masks = [(m[0], float(m[1])) for m in masks if isinstance(m, (list, tuple))]
        return cls(
            column=d["column"],
            dtype=d.get("dtype", "object"),
            len_min=int(d.get("len_min", 0)),
            len_max=int(d.get("len_max", 0)),
            len_mean=float(d.get("len_mean", 0)),
            len_std=float(d.get("len_std", 0)),
            len_p50=int(d.get("len_p50", 0)),
            len_p90=int(d.get("len_p90", 0)),
            frac_digit=float(d.get("frac_digit", 0)),
            frac_alpha=float(d.get("frac_alpha", 0)),
            frac_upper=float(d.get("frac_upper", 0)),
            frac_space=float(d.get("frac_space", 0)),
            frac_punct=float(d.get("frac_punct", 0)),
            frac_arabic=float(d.get("frac_arabic", 0)),
            distinct_ratio=float(d.get("distinct_ratio", 0)),
            is_unique=bool(d.get("is_unique", False)),
            is_constant=bool(d.get("is_constant", False)),
            null_rate=float(d.get("null_rate", 0)),
            has_nulls=bool(d.get("has_nulls", False)),
            empty_rate=float(d.get("empty_rate", 0)),
            token_count_mean=float(d.get("token_count_mean", 0)),
            len_entropy=float(d.get("len_entropy", 0)),
            pattern_masks=parsed_masks,
            num_min=d.get("num_min"),
            num_max=d.get("num_max"),
            num_mean=d.get("num_mean"),
            num_std=d.get("num_std"),
            feature_spec_version=d.get("feature_spec_version", FEATURE_SPEC_VERSION),
            is_pii_reduced=bool(d.get("is_pii_reduced", False)),
            stats_source=d.get("stats_source", "sample"),
            sample_size=int(d.get("sample_size", 0)),
        )


def compute_fingerprint(
    series: pd.Series,
    *,
    column: str,
    is_pii: bool = False,
    full_table_stats: dict | None = None,
    spec_version: str = FEATURE_SPEC_VERSION,
    arabic_fraction: float = 0.0,
) -> ColumnFingerprint:
    """Derive a PII-safe fingerprint from a column sample.

    Shape/distribution features come from ``series`` (the pandas sample).
    Count / distinct / min-max slots are filled from ``full_table_stats`` when
    supplied (stats_source="full"); otherwise from the sample.
    """
    n_rows = max(len(series), 1)
    null_rate = float(series.isna().mean())
    has_nulls = null_rate > 0

    non_null = series.dropna()
    str_vals = non_null.astype(str) if not non_null.empty else pd.Series(dtype=str)
    empty_rate = float((str_vals == "").mean()) if not str_vals.empty else 0.0

    # Distinct / count — prefer full-table stats when available
    fts = full_table_stats or {}
    stats_source = "full" if fts else "sample"
    if fts.get("count") is not None:
        row_count = max(int(fts["count"]), 1)
    else:
        row_count = n_rows

    if fts.get("approx_distinct") is not None:
        unique_count = int(fts["approx_distinct"])
    else:
        unique_count = int(series.nunique(dropna=True))

    distinct_ratio = min(unique_count / row_count, 1.0)
    is_unique = unique_count >= row_count and row_count > 0 and distinct_ratio >= 0.999
    is_constant = unique_count <= 1

    # Length stats (string representation)
    lengths = str_vals.str.len() if not str_vals.empty else pd.Series(dtype=int)
    len_min = int(lengths.min()) if not lengths.empty else 0
    len_max = int(lengths.max()) if not lengths.empty else 0
    len_mean = float(lengths.mean()) if not lengths.empty else 0.0
    len_std = float(lengths.std()) if len(lengths) > 1 else 0.0
    len_p50 = _percentile(lengths, 0.5)
    len_p90 = _percentile(lengths, 0.9)

    char_fracs = _char_fractions(str_vals)
    if arabic_fraction > 0 and char_fracs["frac_arabic"] == 0:
        char_fracs["frac_arabic"] = arabic_fraction

    # Token count (whitespace-split words)
    if not str_vals.empty:
        token_counts = str_vals.str.split().map(len)
        token_count_mean = float(token_counts.mean())
    else:
        token_count_mean = 0.0

    len_entropy = _length_entropy(lengths)
    masks = _pattern_masks(str_vals)

    # Numeric shape — only for numeric non-PII columns
    num_min = num_max = num_mean = num_std = None
    is_numeric = pd.api.types.is_numeric_dtype(series)
    if is_numeric and not is_pii:
        numeric = pd.to_numeric(non_null, errors="coerce").dropna()
        if not numeric.empty:
            if fts.get("min") is not None:
                num_min = float(fts["min"])
            else:
                num_min = float(numeric.min())
            if fts.get("max") is not None:
                num_max = float(fts["max"])
            else:
                num_max = float(numeric.max())
            num_mean = float(numeric.mean())
            num_std = float(numeric.std()) if len(numeric) > 1 else 0.0

    is_pii_reduced = bool(is_pii)
    if is_pii_reduced:
        num_min = num_max = num_mean = num_std = None

    return ColumnFingerprint(
        column=column,
        dtype=str(series.dtype),
        len_min=len_min,
        len_max=len_max,
        len_mean=round(len_mean, 4),
        len_std=round(len_std, 4),
        len_p50=len_p50,
        len_p90=len_p90,
        frac_digit=round(char_fracs["frac_digit"], 6),
        frac_alpha=round(char_fracs["frac_alpha"], 6),
        frac_upper=round(char_fracs["frac_upper"], 6),
        frac_space=round(char_fracs["frac_space"], 6),
        frac_punct=round(char_fracs["frac_punct"], 6),
        frac_arabic=round(char_fracs["frac_arabic"], 6),
        distinct_ratio=round(distinct_ratio, 6),
        is_unique=is_unique,
        is_constant=is_constant,
        null_rate=round(null_rate, 6),
        has_nulls=has_nulls,
        empty_rate=round(empty_rate, 6),
        token_count_mean=round(token_count_mean, 4),
        len_entropy=len_entropy,
        pattern_masks=masks,
        num_min=num_min,
        num_max=num_max,
        num_mean=num_mean,
        num_std=num_std,
        feature_spec_version=spec_version,
        is_pii_reduced=is_pii_reduced,
        stats_source=stats_source,
        sample_size=len(series),
    )


def compute_table_fingerprints(
    df: pd.DataFrame,
    *,
    pii_columns: Optional[set[str]] = None,
    arabic_columns: Optional[dict[str, float]] = None,
    full_table_stats: Optional[dict[str, dict]] = None,
    columns: Optional[list[str]] = None,
    spec_version: str = FEATURE_SPEC_VERSION,
) -> list[ColumnFingerprint]:
    """Compute structural fingerprints for all columns in ``df``."""
    if df is None or df.empty:
        return []
    pii_columns = pii_columns or set()
    arabic_columns = arabic_columns or {}
    full_table_stats = full_table_stats or {}
    target_cols = columns or list(df.columns)
    fps: list[ColumnFingerprint] = []
    for col in target_cols:
        if col not in df.columns:
            continue
        fts = full_table_stats.get(col)
        fps.append(
            compute_fingerprint(
                df[col],
                column=col,
                is_pii=col in pii_columns,
                full_table_stats=fts,
                spec_version=spec_version,
                arabic_fraction=arabic_columns.get(col, 0.0),
            )
        )
    return fps
