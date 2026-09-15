"""Portable value-free table-column evaluation datasets."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Optional, Sequence

from redibis.pii.eval.span_metrics import current_redibis_version

DATASET_KIND = "redibis.table_column_eval_dataset"
REPORT_KIND = "redibis.table_column_eval_report"
SCHEMA_VERSION = "1.0"
# 1.1 adds the optional per-column ``samples`` / ``samples_withheld`` fields.
# A 1.1 dataset with no samples is byte-identical in meaning to a 1.0 one, so
# both versions stay loadable forever — never drop "1.0" from this set.
SCHEMA_VERSION_SAMPLES = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION, SCHEMA_VERSION_SAMPLES})
TARGETS = ("engine", "contract", "llm")
MAX_SAMPLES_PER_COLUMN = 50


class TableEvalError(ValueError):
    """Raised when a structured evaluation dataset cannot be used."""


def structural_fingerprint(column_names: Sequence[str], dtypes: Sequence[str] | None = None) -> str:
    types = list(dtypes or ["*"] * len(column_names))
    payload = "|".join(
        f"{name}:{types[i] if i < len(types) else '*'}"
        for i, name in enumerate(column_names)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _norm_tags(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _norm_samples(raw: Any) -> list[str]:
    """Sample records as strings, capped. Never fails a dataset over samples."""
    if not isinstance(raw, list):
        return []
    return [str(v) for v in raw[:MAX_SAMPLES_PER_COLUMN] if v is not None]


def _column_entry(raw: Mapping[str, Any], *, index: int, require_entity: bool) -> dict[str, Any]:
    name = str(raw.get("name") or raw.get("column") or "").strip()
    if not name:
        raise TableEvalError(f"columns[{index}].name is required")
    is_pii = bool(raw.get("is_pii"))
    entity_type = str(raw.get("entity_type") or "").strip().upper()
    if require_entity and is_pii and not entity_type:
        raise TableEvalError(f"column {name!r}: entity_type is required when is_pii is true")
    return {
        "name": name,
        "is_pii": is_pii,
        "entity_type": entity_type,
        "logicalType": str(raw.get("logicalType") or raw.get("logical_type") or "").strip(),
        "privacy_classification": str(
            raw.get("privacy_classification")
            or raw.get("classification")
            or ((raw.get("privacy") or {}) if isinstance(raw.get("privacy"), Mapping) else {}).get("classification")
            or ""
        ).strip(),
        "businessName": str(raw.get("businessName") or raw.get("business_name") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "business_definition": str(
            raw.get("business_definition")
            or ((raw.get("business") or {}) if isinstance(raw.get("business"), Mapping) else {}).get("definition")
            or ""
        ).strip(),
        "tags": _norm_tags(raw.get("tags")),
        # Optional, schema 1.1+. Evaluation never scores these; they are carried
        # so a reviewer can see what the column actually holds.
        "samples": _norm_samples(raw.get("samples")),
        "samples_withheld": str(raw.get("samples_withheld") or "").strip(),
    }


def classify_table_eval_payload(raw: Any) -> tuple[str, str]:
    if not isinstance(raw, Mapping):
        return "failed", "not a JSON object"
    if raw.get("kind") != DATASET_KIND:
        return "failed", f"not a table evaluation dataset (kind={raw.get('kind')!r})"
    if str(raw.get("schema_version") or "") not in SUPPORTED_SCHEMA_VERSIONS:
        return "failed", f"unsupported schema_version {raw.get('schema_version')!r}"
    return "ok", ""


def validate_table_dataset(
    dataset: Mapping[str, Any],
    *,
    table_columns: Iterable[str] | None = None,
    table_fingerprint: str = "",
    require_entity: bool = True,
) -> dict[str, Any]:
    status, reason = classify_table_eval_payload(dataset)
    if status != "ok":
        raise TableEvalError(reason)
    columns_raw = dataset.get("columns") or dataset.get("column_evaluations")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise TableEvalError("columns must be a non-empty array")
    seen: set[str] = set()
    columns: list[dict[str, Any]] = []
    for index, raw in enumerate(columns_raw):
        if not isinstance(raw, Mapping):
            raise TableEvalError(f"columns[{index}] must be an object")
        if isinstance(raw.get("golden_truth"), Mapping):
            classification = raw.get("golden_truth", {}).get("column_classification") or {}
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
            input_data = raw.get("input_data") if isinstance(raw.get("input_data"), Mapping) else {}
            raw = {
                "name": input_data.get("column_name") or raw.get("name") or metadata.get("column_name"),
                "is_pii": classification.get("is_pii"),
                "entity_type": classification.get("expected_semantic_type"),
                "logicalType": metadata.get("logical_type"),
                "businessName": metadata.get("column_business_name"),
                "description": (
                    metadata.get("column_description")
                    or metadata.get("description")
                    or raw.get("description")
                ),
                "privacy_classification": classification.get("pii_category") or classification.get("sensitivity_level"),
                "tags": raw.get("tags"),
            }
        entry = _column_entry(raw, index=index, require_entity=require_entity)
        if entry["name"] in seen:
            raise TableEvalError(f"duplicate column {entry['name']!r}")
        seen.add(entry["name"])
        columns.append(entry)

    live = [str(c) for c in (table_columns or [])]
    missing = sorted(set(live) - seen) if live else []
    extra = sorted(seen - set(live)) if live else []
    expected_fp = str(dataset.get("fingerprint") or "")
    stale = bool(table_fingerprint and expected_fp and expected_fp != table_fingerprint)
    return {
        "kind": DATASET_KIND,
        # Echo the input version — a 1.1 dataset stays 1.1 through validation.
        "schema_version": str(dataset.get("schema_version") or SCHEMA_VERSION),
        "redibis_version": str(dataset.get("redibis_version") or current_redibis_version()),
        "table_name": str(dataset.get("table_name") or dataset.get("table") or ""),
        "fingerprint": expected_fp or table_fingerprint,
        "residency": str(dataset.get("residency") or "portable"),
        "source": str(dataset.get("source") or ""),
        "warning": str(dataset.get("warning") or ""),
        "notes": dict(dataset.get("notes") or {}) if isinstance(dataset.get("notes"), Mapping) else {},
        "columns": columns,
        "missing_columns": missing,
        "extra_columns": extra,
        "stale_fingerprint": stale,
    }


def empty_actual(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "is_pii": False,
        "entity_type": "",
        "logicalType": "",
        "privacy_classification": "",
        "businessName": "",
        "description": "",
        "business_definition": "",
        "tags": [],
        "samples": [],
        "samples_withheld": "",
        "is_proposal": False,
        "source": "",
    }
