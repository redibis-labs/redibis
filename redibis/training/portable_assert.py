"""Fail-closed value-leak assert for portable (Track A) training rows."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from redibis.telemetry.pii_scope import payload_may_contain_raw_pii

# Keys that must never appear on a portable training row (codegen egress parity).
FORBIDDEN_RAW_KEYS = frozenset({
    "sample",
    "samples",
    "values",
    "example_values",
    "text_context",
    "description",
    "definition",
})


class PortableLeakError(ValueError):
    """Raised when a portable training row carries (or looks like) raw values."""

    def __init__(self, message: str, *, path: str = ""):
        self.path = path
        detail = f"{path}: {message}" if path else message
        super().__init__(detail)


def _walk(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, val in obj.items():
            child = f"{path}.{key}" if path else str(key)
            yield child, val
            yield from _walk(val, child)
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            child = f"{path}[{i}]"
            yield child, val
            yield from _walk(val, child)


# Leaf path suffixes that are never cell values (timestamps, hashes, ids, shapes).
_SAFE_PATH_SUFFIXES = (
    ".context_hash",
    ".format_signatures",
    ".decision_rule",
    ".decided_at",
    ".reviewed_at",
    ".created_at",
    ".ts",
    ".run_id",
    ".checksum",
    ".version",
)


def assert_portable_row(row: dict[str, Any], *, path: str = "row") -> None:
    """Error if ``row`` is not safe to leave the trust boundary.

    Reuses egress/memory heuristics (forbidden keys + ``payload_may_contain_raw_pii``).
    Does **not** call ``scrub_pii_enrichment_output`` (contract example scrubber).
    """
    if not isinstance(row, dict):
        raise PortableLeakError("training row must be a mapping", path=path)

    residency = str(row.get("residency") or "portable").lower()
    if residency == "local":
        raise PortableLeakError("residency=local is not portable", path=f"{path}.residency")
    if row.get("raw_trained") is True:
        raise PortableLeakError("raw_trained marker is not portable", path=f"{path}.raw_trained")
    if row.get("contains_raw_values") is True:
        raise PortableLeakError(
            "contains_raw_values=true is not portable",
            path=f"{path}.contains_raw_values",
        )

    for key in FORBIDDEN_RAW_KEYS:
        if key in row and row[key] not in (None, "", [], {}):
            raise PortableLeakError(
                f"forbidden raw-bearing field {key!r}",
                path=f"{path}.{key}",
            )

    features = row.get("features")
    if isinstance(features, dict):
        for key in FORBIDDEN_RAW_KEYS:
            if key in features and features[key] not in (None, "", [], {}):
                raise PortableLeakError(
                    f"forbidden raw-bearing feature {key!r}",
                    path=f"{path}.features.{key}",
                )

    for child_path, val in _walk(row, path):
        leaf = child_path.rsplit(".", 1)[-1].split("[", 1)[0]
        if leaf in FORBIDDEN_RAW_KEYS and val not in (None, "", [], {}):
            raise PortableLeakError(
                f"forbidden raw-bearing field {leaf!r}",
                path=child_path,
            )
        if isinstance(val, str) and len(val) >= 8:
            if any(child_path.endswith(sfx) for sfx in _SAFE_PATH_SUFFIXES):
                continue
            if "/format_signatures[" in child_path or child_path.endswith("format_signatures"):
                continue
            # ISO-8601 timestamps falsely trip the phone heuristic.
            if "T" in val and val[:4].isdigit() and ("-" in val or ":" in val):
                continue
            if payload_may_contain_raw_pii(val):
                raise PortableLeakError(
                    "string looks like raw PII (email/phone/SSN heuristic)",
                    path=child_path,
                )


def assert_portable_dataset_rows(rows: Iterable[dict[str, Any]]) -> None:
    for i, row in enumerate(rows):
        assert_portable_row(row, path=f"row[{i}]")
