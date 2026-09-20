"""Portable steward verdict package — privacy-safe export and replay input."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from redibis.review.drift import LIFECYCLE_ACTIVE

VERDICT_PACKAGE_VERSION = "1.0"
VERDICT_PACKAGE_KIND = "redibis.verdict_package"
STEWARD_VERDICTS_KIND = "redibis.steward_verdicts"


class VerdictPackageError(ValueError):
    """Invalid or incompatible verdict package."""


@dataclass
class VerdictEntry:
    table: str
    column: str
    status: str  # pii | not_pii
    entity_type: Optional[str] = None
    fingerprint_key: str = ""
    logical_type: str = ""
    physical_type: str = ""
    format_signature: str = ""
    name_normalized: str = ""
    evidence_digest: str = ""
    lifecycle_state: str = LIFECYCLE_ACTIVE
    decision_version: int = 1
    decided_by: str = ""
    reason: str = ""
    ts: str = ""
    source_run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "status": self.status,
            "entity_type": self.entity_type,
            "fingerprint_key": self.fingerprint_key,
            "logical_type": self.logical_type,
            "physical_type": self.physical_type,
            "format_signature": self.format_signature,
            "name_normalized": self.name_normalized,
            "evidence_digest": self.evidence_digest,
            "lifecycle_state": self.lifecycle_state,
            "decision_version": self.decision_version,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "ts": self.ts,
            "source_run_id": self.source_run_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VerdictEntry":
        if str(data.get("status") or "") not in ("pii", "not_pii"):
            raise VerdictPackageError(f"invalid status for {data.get('column')!r}")
        return cls(
            table=str(data.get("table") or ""),
            column=str(data.get("column") or ""),
            status=str(data.get("status") or ""),
            entity_type=data.get("entity_type"),
            fingerprint_key=str(data.get("fingerprint_key") or ""),
            logical_type=str(data.get("logical_type") or ""),
            physical_type=str(data.get("physical_type") or ""),
            format_signature=str(data.get("format_signature") or ""),
            name_normalized=str(data.get("name_normalized") or ""),
            evidence_digest=str(data.get("evidence_digest") or ""),
            lifecycle_state=str(data.get("lifecycle_state") or LIFECYCLE_ACTIVE),
            decision_version=int(data.get("decision_version") or 1),
            decided_by=str(data.get("decided_by") or ""),
            reason=str(data.get("reason") or ""),
            ts=str(data.get("ts") or ""),
            source_run_id=str(data.get("source_run_id") or ""),
        )


@dataclass
class VerdictPackage:
    schema_version: str = VERDICT_PACKAGE_VERSION
    kind: str = VERDICT_PACKAGE_KIND
    exported_at: str = ""
    exporter: str = ""
    tables: list[str] = field(default_factory=list)
    entries: list[VerdictEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "exported_at": self.exported_at,
            "exporter": self.exporter,
            "tables": list(self.tables),
            "entries": [e.to_dict() for e in self.entries],
        }

    def entries_for_table(self, table: str) -> list[VerdictEntry]:
        return [e for e in self.entries if e.table == table]

    def by_column(self, table: str) -> dict[str, VerdictEntry]:
        return {e.column: e for e in self.entries if e.table == table}


def verdict_package_digest(package: VerdictPackage | dict) -> str:
    payload = package.to_dict() if isinstance(package, VerdictPackage) else package
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_jsonl(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_verdict_package(path: Path | str) -> VerdictPackage:
    """Load a verdict package from JSON or JSONL."""
    p = Path(path)
    if not p.is_file():
        raise VerdictPackageError(f"verdict package not found: {p}")
    raw = p.read_text(encoding="utf-8")
    try:
        if p.suffix.lower() == ".jsonl":
            rows = _parse_jsonl(raw)
            if not rows:
                raise VerdictPackageError("empty JSONL verdict package")
            header = rows[0]
            if header.get("kind") == VERDICT_PACKAGE_KIND:
                entries = [VerdictEntry.from_dict(r) for r in rows[1:]]
                return VerdictPackage(
                    schema_version=str(header.get("schema_version") or VERDICT_PACKAGE_VERSION),
                    kind=VERDICT_PACKAGE_KIND,
                    exported_at=str(header.get("exported_at") or ""),
                    exporter=str(header.get("exporter") or ""),
                    tables=list(header.get("tables") or []),
                    entries=entries,
                )
            return VerdictPackage(
                entries=[VerdictEntry.from_dict(r) for r in rows],
            )
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerdictPackageError(f"invalid verdict package JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise VerdictPackageError("verdict package must be a JSON object")
    if data.get("kind") == STEWARD_VERDICTS_KIND:
        return steward_verdicts_to_package(data)
    if data.get("kind") and data.get("kind") != VERDICT_PACKAGE_KIND:
        raise VerdictPackageError(f"unsupported kind: {data.get('kind')!r}")
    version = str(data.get("schema_version") or VERDICT_PACKAGE_VERSION)
    if version.split(".")[0] != VERDICT_PACKAGE_VERSION.split(".")[0]:
        raise VerdictPackageError(f"incompatible schema_version: {version}")

    entries_raw = data.get("entries") or []
    if not isinstance(entries_raw, list):
        raise VerdictPackageError("entries must be a list")
    entries = [VerdictEntry.from_dict(e) for e in entries_raw if isinstance(e, dict)]
    return VerdictPackage(
        schema_version=version,
        kind=str(data.get("kind") or VERDICT_PACKAGE_KIND),
        exported_at=str(data.get("exported_at") or ""),
        exporter=str(data.get("exporter") or ""),
        tables=[str(t) for t in (data.get("tables") or [])],
        entries=entries,
    )


def _pii_status_from_steward_entry(entry: dict) -> Optional[str]:
    """Map one A1 steward entry onto ``pii`` / ``not_pii``, or None to skip."""
    field = entry.get("field")
    if field not in (None, "", "pii", "column"):
        return None
    if entry.get("decision") in ("reject", "needs_review"):
        return None
    value = entry.get("value")
    if value is True:
        return "pii"
    if value is False:
        return "not_pii"
    if isinstance(value, dict):
        if "is_pii" in value:
            return "pii" if value.get("is_pii") else "not_pii"
        if value.get("status") in ("pii", "not_pii"):
            return str(value.get("status"))
        if value.get("detected") is True:
            return "pii"
        if value.get("detected") is False:
            return "not_pii"
    col_status = str(entry.get("status") or "")
    if col_status in ("pii", "not_pii"):
        return col_status
    return None


def steward_verdicts_to_package(data: dict) -> VerdictPackage:
    """Project A1 ``redibis.steward_verdicts`` onto a scan-ready ``VerdictPackage``.

    Only PII field entries become scan/import memory. Definitions, tags, and
    table items stay in the A1 file for audit; they are not replayed by
    ``redibis scan decide --verdicts`` or ``redibis verdict import``.
    """
    table_hint = str(data.get("table") or "")
    entries: list[VerdictEntry] = []
    for raw in data.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        column = raw.get("column")
        if not column:
            continue
        status = _pii_status_from_steward_entry(raw)
        if status is None:
            continue
        value = raw.get("value") if isinstance(raw.get("value"), dict) else {}
        entity = value.get("entity_type") if isinstance(value, dict) else None
        table = str(raw.get("table") or table_hint)
        entries.append(VerdictEntry(
            table=table,
            column=str(column),
            status=status,
            entity_type=entity,
            fingerprint_key=str(raw.get("fingerprint_key") or ""),
            lifecycle_state=str(raw.get("lifecycle_state") or LIFECYCLE_ACTIVE),
            decision_version=int(raw.get("decision_version") or 1),
            decided_by=str(raw.get("by") or raw.get("decided_by") or data.get("exporter") or ""),
            reason=str(raw.get("rationale_code") or raw.get("reason") or ""),
            ts=str(raw.get("at") or data.get("exported_at") or ""),
            source_run_id=str(raw.get("chosen_run_id") or ""),
        ))
    tables = sorted({e.table for e in entries if e.table} | ({table_hint} if table_hint else set()))
    return VerdictPackage(
        schema_version=str(data.get("schema_version") or VERDICT_PACKAGE_VERSION),
        kind=VERDICT_PACKAGE_KIND,
        exported_at=str(data.get("exported_at") or ""),
        exporter=str(data.get("exporter") or ""),
        tables=tables,
        entries=entries,
    )


def export_verdict_package(
    entries: Iterable[VerdictEntry],
    *,
    exporter: str = "",
    exported_at: str = "",
) -> VerdictPackage:
    entry_list = list(entries)
    tables = sorted({e.table for e in entry_list if e.table})
    return VerdictPackage(
        exported_at=exported_at,
        exporter=exporter,
        tables=tables,
        entries=entry_list,
    )


def write_verdict_package(package: VerdictPackage, path: Path | str, *, as_jsonl: bool = False) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if as_jsonl:
        lines = [json.dumps(package.to_dict(), sort_keys=True)]
        for entry in package.entries:
            lines.append(json.dumps(entry.to_dict(), sort_keys=True))
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        out.write_text(json.dumps(package.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
