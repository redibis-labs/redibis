"""
redibis.memory.fingerprint
==========================
Engine-neutral column fingerprints for retrieval and memory storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from redibis.metadata.base import CatalogColumnStats, ColumnCatalogInfo, SourceMetadata
from redibis.models import ColumnProfile, PIIDetection
from redibis.profiling.om_metrics import compute_column_metrics

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class FingerprintField:
    value: Any
    source: str  # hms | pushdown | sample | pandas
    generalizes: str  # stable | approximate | sample_only


@dataclass
class ColumnFingerprint:
    table: str
    column: str
    domain: str
    name_normalized: str
    logical_type: FingerprintField
    physical_type: FingerprintField
    format_signature: FingerprintField
    cardinality_class: FingerprintField
    nullable: FingerprintField
    stats: dict[str, FingerprintField] = field(default_factory=dict)
    redacted_samples: list[str] = field(default_factory=list)
    entity_type: Optional[str] = None

    def to_column_card(self) -> str:
        """English embedding text — no raw sample values or sample-derived extrema."""
        parts = [
            f"column: {self.name_normalized}",
            f"logical_type: {self.logical_type.value}",
            f"format: {self.format_signature.value}",
        ]
        if self.entity_type:
            parts.append(f"entity_hint: {self.entity_type}")
        if self.cardinality_class.value:
            parts.append(f"cardinality: {self.cardinality_class.value}")
        if self.domain:
            parts.append(f"domain: {self.domain}")
        for key, fp in sorted(self.stats.items()):
            if fp.generalizes == "sample_only":
                continue
            if key in ("min", "max"):
                continue
            parts.append(f"{key}({fp.generalizes}): {fp.value}")
        if self.redacted_samples:
            parts.append(f"sample_shapes: {', '.join(self.redacted_samples[:3])}")
        return " | ".join(parts)


def normalize_column_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def value_to_shape(value: Any) -> str:
    """Map a value to a token-shape mask (digits→D, ASCII letters→L)."""
    text = str(value)
    out: list[str] = []
    for ch in text:
        if ch.isdigit():
            out.append("D")
        elif ch.isascii() and ch.isalpha():
            out.append("L")
        elif ch.isspace():
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def compute_format_signature(values: list[Any]) -> tuple[str, str]:
    """
    Return ``(signature, source_tag)`` for non-empty sample values.

    Tries regex-catalog pattern names, then well-known shapes, then token masks.
    """
    clean = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not clean:
        return "empty", "sample"

    from redibis.pii.regex_catalog import compiled_structured

    for name, pattern in compiled_structured.items():
        try:
            if all(pattern.fullmatch(v) for v in clean[:8]):
                return name, "sample"
        except Exception:
            continue

    if all(_EMAIL_RE.match(v) for v in clean[:8]):
        return "email", "sample"
    if all(_UUID_RE.match(v) for v in clean[:8]):
        return "uuid", "sample"
    if all(_ISO_DATE_RE.match(v) for v in clean[:8]):
        return "iso_date", "sample"

    shapes = {value_to_shape(v) for v in clean[:8]}
    if len(shapes) == 1:
        return next(iter(shapes)), "sample"
    return "mixed", "sample"


def _cardinality_class(
    *,
    unique_ratio: Optional[float],
    unique_count: Optional[int],
    row_count: Optional[int],
) -> str:
    if unique_ratio is not None and unique_ratio >= 0.95:
        return "unique"
    if row_count and unique_count is not None and unique_count >= max(1, row_count - 1):
        return "unique"
    if unique_ratio is not None and unique_ratio <= 0.05:
        return "categorical"
    if unique_count is not None and row_count and unique_count <= max(20, int(row_count * 0.05)):
        return "categorical"
    return "continuous"


def _field(
    value: Any,
    *,
    source: str,
    generalizes: str,
) -> FingerprintField:
    return FingerprintField(value=value, source=source, generalizes=generalizes)


def build_fingerprints(
    df: pd.DataFrame,
    source_metadata: SourceMetadata,
    column_profiles: list[ColumnProfile],
    *,
    table: str,
    domain: str = "",
    gap_stats: Optional[dict[str, CatalogColumnStats]] = None,
    pii_detections: Optional[list[PIIDetection]] = None,
) -> list[ColumnFingerprint]:
    """Compose Tier A/B catalog facts with Tier C sample profiles."""
    profile_by_col = {p.column: p for p in column_profiles}
    catalog_by_col = {c.name: c for c in source_metadata.columns}
    pii_by_col = {d.column: d for d in (pii_detections or [])}
    gap_stats = gap_stats or {}
    row_count = len(df)

    fingerprints: list[ColumnFingerprint] = []
    for col in df.columns:
        col_name = str(col)
        catalog = catalog_by_col.get(col_name)
        profile = profile_by_col.get(col_name)
        catalog_stats = source_metadata.column_stats.get(col_name)
        push_stats = gap_stats.get(col_name)

        samples = []
        if col_name in df.columns:
            samples = (
                df[col_name].dropna().astype(str).head(8).tolist()
            )
        sig, _ = compute_format_signature(samples)
        shape_samples = list({value_to_shape(v) for v in samples[:5]})

        logical = (
            profile.logical_type if profile else "string"
        )
        physical = (
            profile.physical_type if profile else (
                catalog.native_type if catalog else str(df[col_name].dtype)
            )
        )

        entity_type = None
        det = pii_by_col.get(col_name)
        if det and det.presidio_entity:
            entity_type = det.presidio_entity
        elif det and det.gliner_label:
            entity_type = det.gliner_label

        metric = None
        if col_name in df.columns:
            metric = compute_column_metrics(df[[col_name]]).get(col_name)
        stats: dict[str, FingerprintField] = {}

        if catalog_stats and catalog_stats.ndv is not None:
            stats["distinct_count"] = _field(
                catalog_stats.ndv, source="hms", generalizes="approximate",
            )
        elif push_stats and push_stats.ndv is not None:
            stats["distinct_count"] = _field(
                push_stats.ndv, source="pushdown", generalizes="approximate",
            )
        elif metric:
            stats["distinct_count"] = _field(
                metric.unique_count, source="sample", generalizes="sample_only",
            )

        null_rate = None
        null_source = "sample"
        null_generalizes = "sample_only"
        if catalog_stats and catalog_stats.num_nulls is not None and row_count:
            null_rate = catalog_stats.num_nulls / row_count
            null_source = "hms"
            null_generalizes = "approximate"
        elif push_stats and push_stats.num_nulls is not None and row_count:
            null_rate = push_stats.num_nulls / row_count
            null_source = "pushdown"
            null_generalizes = "approximate"
        elif profile and profile.null_rate is not None:
            null_rate = profile.null_rate
        elif metric:
            null_rate = metric.null_proportion
        if null_rate is not None:
            stats["null_rate"] = _field(
                round(null_rate, 4), source=null_source, generalizes=null_generalizes,
            )

        if metric and metric.min_value is not None:
            stats["min"] = _field(metric.min_value, source="sample", generalizes="sample_only")
        if metric and metric.max_value is not None:
            stats["max"] = _field(metric.max_value, source="sample", generalizes="sample_only")

        unique_ratio = None
        unique_count = None
        card_source = "sample"
        if catalog_stats and catalog_stats.ndv is not None:
            unique_count = catalog_stats.ndv
            unique_ratio = (catalog_stats.ndv / row_count) if row_count else None
            card_source = "hms"
        elif push_stats and push_stats.ndv is not None:
            unique_count = push_stats.ndv
            unique_ratio = (push_stats.ndv / row_count) if row_count else None
            card_source = "pushdown"
        elif profile:
            unique_ratio = profile.cardinality_ratio
        elif metric:
            unique_ratio = metric.unique_proportion
            unique_count = metric.unique_count

        card = _cardinality_class(
            unique_ratio=unique_ratio,
            unique_count=unique_count,
            row_count=row_count,
        )

        fingerprints.append(
            ColumnFingerprint(
                table=table,
                column=col_name,
                domain=domain,
                name_normalized=normalize_column_name(col_name),
                logical_type=_field(
                    logical,
                    source="hms" if catalog else "pandas",
                    generalizes="stable",
                ),
                physical_type=_field(
                    physical,
                    source="hms" if catalog else "pandas",
                    generalizes="stable",
                ),
                format_signature=_field(sig, source="sample", generalizes="stable"),
                cardinality_class=_field(
                    card,
                    source=card_source,
                    generalizes="approximate",
                ),
                nullable=_field(
                    catalog.nullable if catalog else bool(metric and metric.null_count > 0),
                    source="hms" if catalog else "sample",
                    generalizes="stable" if catalog else "approximate",
                ),
                stats=stats,
                redacted_samples=shape_samples,
                entity_type=entity_type,
            )
        )

    return fingerprints
