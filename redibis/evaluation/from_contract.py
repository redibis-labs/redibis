"""Build a table-column evaluation dataset from an active data contract.

The contract is treated as **ground truth**: each column's ``is_pii``,
``entity_type``, ``privacy_classification`` and agreed ``tags`` become the
expected values a later ``redibis eval run --target engine`` is scored against.

Scoring the *contract* against a dataset generated from that same contract is
circular and always yields F1 = 1.0 — use ``--target engine`` (or ``llm``).

Sample records are **opt-in**. A dataset with no samples is value-free and
carries ``residency: "portable"``; the moment samples are attached the dataset
stops being portable and says so.

Two sample modes, with different consent rules:

``shape``  Length + token-shape masks via ``redibis.memory.redaction.redact_samples``.
           These contain no characters or digits, so they need no consent by
           default — set ``require_consent=True`` to gate them anyway.
``raw``    Real values. Always requires recorded steward sampling consent for
           that specific column, and is refused outright when no consent store
           is available (for example when reading a contract from a file).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from redibis.evaluation.schema import (
    DATASET_KIND,
    SCHEMA_VERSION,
    SCHEMA_VERSION_SAMPLES,
    TableEvalError,
    structural_fingerprint,
)
from redibis.evaluation.targets import from_contract

DEFAULT_SAMPLE_COUNT = 10
SAMPLE_MODES = ("shape", "raw", "none")
CONSENT_WITHHELD = "no steward sampling consent"
CONSENT_UNAVAILABLE = "raw samples need a contract store with sampling consent"
NO_VALUES = "no non-empty values in the supplied data"
NO_DATA = "no data file supplied"

RESIDENCY_PORTABLE = "portable"
RESIDENCY_VALUES = "contains_values"
RESIDENCY_SHAPES = "contains_shapes"

_VALUES_WARNING = (
    "This dataset carries raw sample values and is NOT value-free. Do not "
    "commit it to a shared corpus, attach it to a ticket, or send it outside "
    "the data's residency boundary."
)
_SHAPES_WARNING = (
    "This dataset carries shape masks derived from real values. They contain "
    "no characters or digits, but they do reveal value lengths and formats."
)


def normalize_tags(
    tags: Iterable[Any],
    *,
    allowlist: Optional[Iterable[str]] = None,
) -> list[str]:
    """De-duplicated, order-preserving tags, optionally filtered to an allowlist.

    Matching is case-insensitive and whitespace-trimmed, but the emitted tag
    keeps the spelling from the allowlist when one is supplied, so a contract
    written as ``PII`` and an allowlist written as ``pii`` agree on one form.
    """
    canonical: dict[str, str] = {}
    if allowlist is not None:
        for entry in allowlist:
            key = str(entry or "").strip()
            if key:
                canonical.setdefault(key.casefold(), key)

    out: list[str] = []
    seen: set[str] = set()
    for item in tags or []:
        tag = str(item or "").strip()
        if not tag:
            continue
        folded = tag.casefold()
        if allowlist is not None and folded not in canonical:
            continue
        if folded in seen:
            continue
        seen.add(folded)
        out.append(canonical.get(folded, tag))
    return out


def sample_values_from_frame(df, column: str, count: int) -> list[str]:
    """First ``count`` non-empty values of ``column`` as strings."""
    if df is None or column not in getattr(df, "columns", []):
        return []
    out: list[str] = []
    for value in df[column].tolist():
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() in ("nan", "none", "null", "n/a"):
            continue
        out.append(text)
        if len(out) >= count:
            break
    return out


def _column_samples(
    *,
    table: str,
    name: str,
    values: Sequence[str],
    mode: str,
    consent: Any,
    require_consent: bool,
) -> tuple[list[str], str]:
    """Return ``(samples, withheld_reason)`` for one column."""
    if mode == "none":
        return [], ""
    if not values:
        return [], NO_VALUES

    approved: Optional[bool] = None
    if consent is not None:
        try:
            approved = bool(consent.is_approved(table, name))
        except Exception:
            approved = None

    if mode == "raw":
        # Raw values never leave without an explicit, recorded approval. No
        # consent store means there is nothing to approve them, so refuse.
        if consent is None:
            return [], CONSENT_UNAVAILABLE
        if approved is not True:
            return [], CONSENT_WITHHELD
        return [str(v) for v in values], ""

    # mode == "shape" — masks carry no characters or digits, so consent is not
    # required unless the caller asks for it.
    if require_consent and approved is not True:
        return [], CONSENT_WITHHELD

    from redibis.memory.redaction import redact_samples

    shapes = redact_samples(list(values), table=table, column=name, approved=True)
    if not shapes:
        return [], NO_VALUES
    return list(shapes), ""


def dataset_from_contract(
    contract: Mapping[str, Any] | None,
    *,
    table_name: str = "",
    tags_allowlist: Optional[Iterable[str]] = None,
    pii_only: bool = False,
    sample_values: Optional[Mapping[str, Sequence[str]]] = None,
    # "none" by default: supplying values must never be enough to publish them.
    # The caller opts in by naming a mode, at every layer.
    sample_mode: str = "none",
    sample_count: int = DEFAULT_SAMPLE_COUNT,
    consent: Any = None,
    require_consent: bool = False,
    dtypes: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Convert an ODCS contract into a ``redibis.table_column_eval_dataset``.

    ``tags_allowlist=None`` keeps every tag the contract carries. Pass an
    iterable to keep only the agreed vocabulary; pass an empty iterable to
    strip tags entirely.
    """
    from redibis.pii.eval.span_metrics import current_redibis_version

    if sample_mode not in SAMPLE_MODES:
        raise TableEvalError(
            f"sample_mode must be one of {', '.join(SAMPLE_MODES)}, got {sample_mode!r}"
        )
    rows = from_contract(contract)
    if not rows:
        raise TableEvalError(
            "contract has no schema properties — nothing to evaluate"
        )

    table = str(
        table_name
        or (contract or {}).get("physicalName")
        or _physical_name(contract)
        or (contract or {}).get("table_name")
        or ""
    )
    allow = None if tags_allowlist is None else list(tags_allowlist)
    values_by_column = dict(sample_values or {})
    dtype_by_column = dict(dtypes or {})

    columns: list[dict[str, Any]] = []
    dropped_tags: set[str] = set()
    withheld: list[str] = []
    any_samples = False

    for row in rows:
        name = row["name"]
        if pii_only and not row.get("is_pii"):
            continue
        raw_tags = list(row.get("tags") or [])
        kept_tags = normalize_tags(raw_tags, allowlist=allow)
        if allow is not None:
            dropped_tags.update(
                t for t in normalize_tags(raw_tags)
                if t.casefold() not in {k.casefold() for k in kept_tags}
            )

        entry: dict[str, Any] = {
            "name": name,
            "is_pii": bool(row.get("is_pii")),
            "entity_type": str(row.get("entity_type") or "").upper(),
            "logicalType": str(row.get("logicalType") or dtype_by_column.get(name) or ""),
            "privacy_classification": str(row.get("privacy_classification") or ""),
            "businessName": str(row.get("businessName") or ""),
            "description": str(row.get("description") or ""),
            "business_definition": str(row.get("business_definition") or ""),
            "tags": kept_tags,
        }

        samples, reason = _column_samples(
            table=table,
            name=name,
            values=list(values_by_column.get(name) or [])[:sample_count],
            mode=sample_mode,
            consent=consent,
            require_consent=require_consent,
        )
        if sample_mode != "none":
            entry["samples"] = samples
            if samples:
                any_samples = True
            elif reason:
                entry["samples_withheld"] = reason
                if reason in (CONSENT_WITHHELD, CONSENT_UNAVAILABLE):
                    withheld.append(name)
        columns.append(entry)

    if not columns:
        raise TableEvalError(
            "no columns selected — the contract has no PII columns and --pii-only was set"
            if pii_only else "no columns selected"
        )

    names = [c["name"] for c in columns]
    types = [c["logicalType"] or "*" for c in columns]

    if not any_samples:
        residency = RESIDENCY_PORTABLE
    elif sample_mode == "raw":
        residency = RESIDENCY_VALUES
    else:
        residency = RESIDENCY_SHAPES

    dataset: dict[str, Any] = {
        "kind": DATASET_KIND,
        "schema_version": SCHEMA_VERSION_SAMPLES if any_samples else SCHEMA_VERSION,
        "redibis_version": current_redibis_version(),
        "table_name": table,
        "fingerprint": structural_fingerprint(names, types),
        "residency": residency,
        "source": "contract",
        "columns": columns,
    }
    if residency == RESIDENCY_VALUES:
        dataset["warning"] = _VALUES_WARNING
    elif residency == RESIDENCY_SHAPES:
        dataset["warning"] = _SHAPES_WARNING

    notes: dict[str, Any] = {}
    if allow is not None:
        notes["tags_allowlist"] = sorted({t for t in normalize_tags(allow)})
        if dropped_tags:
            notes["tags_dropped"] = sorted(dropped_tags)
    if withheld:
        notes["samples_withheld_columns"] = sorted(withheld)
    if notes:
        dataset["notes"] = notes
    return dataset


def _physical_name(contract: Mapping[str, Any] | None) -> str:
    for schema_obj in (contract or {}).get("schema") or []:
        if isinstance(schema_obj, Mapping) and schema_obj.get("physicalName"):
            return str(schema_obj["physicalName"])
    return ""
