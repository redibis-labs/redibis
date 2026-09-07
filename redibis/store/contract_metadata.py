"""
redibis.store.contract_metadata — operational telemetry OUTSIDE the contract spec.

The active ODCS contract is a declarative *desired state* (version, schema, privacy,
quality, business). Run provenance, ``pii_summary``, and last-updated telemetry
live here instead — either under ``_meta/telemetry/{table}/`` in the contracts
bucket (default) or in a dedicated metadata bucket (``S3_METADATA_BUCKET``).

Exportable as JSON for OpenMetadata, Elasticsearch, or other catalog integrations.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend


# Top-level contract keys that are operational, not spec.
OPERATIONAL_CONTRACT_FIELDS: frozenset[str] = frozenset({
    "provenance",
    "pii_summary",
    "last_updated",
    "last_updated_by_workflow",
    "_scan_metadata",       # transient scan hints from writers; never persisted in spec
    "_column_telemetry",    # per-column discovery evidence; lives in metadata sidecar
})


def slim_contract(contract: dict) -> dict:
    """Return a copy of ``contract`` with operational telemetry removed.

    Also normalises column privacy blocks and drops legacy ``pii``/``maskingPolicy``
    duplicates.
    """
    from redibis.contracts.privacy import normalize_column_privacy

    out = copy.deepcopy(contract)
    for key in OPERATIONAL_CONTRACT_FIELDS:
        out.pop(key, None)

    cps = out.get("customProperties")
    if isinstance(cps, list):
        kept = [cp for cp in cps if not (
            isinstance(cp, dict) and cp.get("property") == "pii_summary"
        )]
        if kept:
            out["customProperties"] = kept
        else:
            out.pop("customProperties", None)

    for col in _iter_columns(out):
        normalize_column_privacy(col)
    return out


def extract_operational_metadata(contract: dict) -> dict:
    """Pull operational fields that may still be embedded in an old contract."""
    meta: dict[str, Any] = {}
    if contract.get("provenance"):
        meta["provenance"] = copy.deepcopy(contract["provenance"])
    if contract.get("pii_summary"):
        meta["pii_summary"] = copy.deepcopy(contract["pii_summary"])
    if contract.get("last_updated"):
        meta["last_updated"] = contract["last_updated"]
    if contract.get("last_updated_by_workflow"):
        meta["last_updated_by_workflow"] = contract["last_updated_by_workflow"]
    return meta


def _iter_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict):
                yield prop


class ContractMetadataStore:
    """Per-table operational metadata sidecar (provenance, pii_summary, telemetry)."""

    DEFAULT_PREFIX = "_meta/telemetry"

    def __init__(
        self,
        backend: StorageBackend,
        bucket: str,
        prefix: str = DEFAULT_PREFIX,
    ):
        self.backend = backend
        self.bucket = bucket
        self.prefix = prefix.strip("/") if prefix else ""

    def _base(self, table: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{table}"
        return table

    def _provenance_key(self, table: str) -> str:
        return f"{self._base(table)}/provenance.jsonl"

    def _pii_summary_key(self, table: str) -> str:
        return f"{self._base(table)}/pii_summary.json"

    def _telemetry_key(self, table: str) -> str:
        return f"{self._base(table)}/telemetry.json"

    def _columns_key(self, table: str) -> str:
        return f"{self._base(table)}/columns.json"

    # ── Provenance (append-only) ───────────────────────────────────────────

    def append_provenance(self, table: str, entry: dict) -> None:
        line = json.dumps(entry, default=str) + "\n"
        key = self._provenance_key(table)
        try:
            existing = self.backend.get_text(self.bucket, key) or ""
        except Exception:
            existing = ""
        self.backend.put_text(self.bucket, key, existing + line)

    def get_provenance(self, table: str, limit: int = 200) -> list[dict]:
        key = self._provenance_key(table)
        try:
            text = self.backend.get_text(self.bucket, key)
        except Exception:
            return []
        if not text:
            return []
        entries = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries[-limit:]

    # ── PII summary ────────────────────────────────────────────────────────

    def set_pii_summary(self, table: str, summary: dict) -> None:
        self.backend.put_json(self.bucket, self._pii_summary_key(table), summary)

    def get_pii_summary(self, table: str) -> Optional[dict]:
        try:
            return self.backend.get_json(self.bucket, self._pii_summary_key(table))
        except Exception:
            return None

    # ── Per-column discovery telemetry ───────────────────────────────────

    def merge_column_telemetry(self, table: str, columns: dict) -> None:
        """Upsert per-column discovery evidence (confidence, engines, …)."""
        if not columns:
            return
        current = self.get_column_telemetry(table)
        current.update(copy.deepcopy(columns))
        self.backend.put_json(self.bucket, self._columns_key(table), current)

    def get_column_telemetry(self, table: str) -> dict:
        try:
            data = self.backend.get_json(self.bucket, self._columns_key(table))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_column_evidence(self, table: str, column: str) -> dict:
        return dict(self.get_column_telemetry(table).get(column) or {})

    def record_final_review_column(
        self,
        table: str,
        column: str,
        *,
        status: str,
        reviewed_by: str,
        reviewed_at: str,
        note: str = "",
        approved: Optional[dict] = None,
    ) -> None:
        """Persist per-column Final Review audit (reviewer, timestamp, status) in MinIO."""
        if not reviewed_by:
            return
        current = self.get_column_telemetry(table)
        col_entry = dict(current.get(column) or {})
        col_entry["final_review"] = {
            "status": status,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
            "note": note or "",
            "approved": dict(approved or {}),
        }
        current[column] = col_entry
        self.backend.put_json(self.bucket, self._columns_key(table), current)

    def clear_final_review_column(self, table: str, column: str) -> None:
        """Remove Final Review audit for one column (e.g. after reset)."""
        current = self.get_column_telemetry(table)
        col_entry = dict(current.get(column) or {})
        col_entry.pop("final_review", None)
        if col_entry:
            current[column] = col_entry
        elif column in current:
            del current[column]
        self.backend.put_json(self.bucket, self._columns_key(table), current)

    def patch_final_review_summary(self, table: str, patch: dict) -> None:
        """Contract-level Final Review summary in telemetry (finalize stamp)."""
        current = self.get_telemetry(table)
        summary = dict(current.get("final_review") or {})
        summary.update(patch)
        current["final_review"] = summary
        self.backend.put_json(self.bucket, self._telemetry_key(table), current)

    # ── Telemetry (last updated) ─────────────────────────────────────────

    def update_telemetry(
        self,
        table: str,
        *,
        last_updated: str,
        last_updated_by_workflow: str,
        version: str,
        contract_uuid: str = "",
    ) -> None:
        payload = {
            "last_updated": last_updated,
            "last_updated_by_workflow": last_updated_by_workflow,
            "version": version,
            "contract_uuid": contract_uuid,
        }
        self.backend.put_json(self.bucket, self._telemetry_key(table), payload)

    def get_telemetry(self, table: str) -> dict:
        try:
            return self.backend.get_json(self.bucket, self._telemetry_key(table)) or {}
        except Exception:
            return {}

    def record_catalog_push(
        self,
        table: str,
        *,
        backend: str,
        entity_fqn: str,
        contract_version: str,
        pushed_at: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        """Record a catalog push in telemetry (backend-neutral; not in contract spec)."""
        from datetime import datetime, timezone

        ts = pushed_at or (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        current = self.get_telemetry(table)
        by_backend = dict(current.get("catalog") or {})
        entry: dict[str, Any] = {
            "pushed_at": ts,
            "entity_fqn": entity_fqn,
            "contract_version": contract_version,
        }
        if details:
            entry.update(details)
        by_backend[backend] = entry
        current["catalog"] = by_backend
        current["last_catalog_push"] = {"backend": backend, **entry}
        self.backend.put_json(self.bucket, self._telemetry_key(table), current)

    # ── Full export package ────────────────────────────────────────────────

    def export_package(
        self,
        table: str,
        *,
        contract: Optional[dict] = None,
        pii_decisions: Optional[dict] = None,
    ) -> dict:
        """JSON bundle for catalog integration (OpenMetadata, etc.)."""
        telemetry = self.get_telemetry(table)
        return {
            "table": table,
            "contract_uuid": (contract or {}).get("contract_uuid")
                             or telemetry.get("contract_uuid", ""),
            "version": (contract or {}).get("version") or telemetry.get("version", ""),
            "spec": contract,
            "telemetry": telemetry,
            "provenance": self.get_provenance(table),
            "pii_summary": self.get_pii_summary(table) or {},
            "column_telemetry": self.get_column_telemetry(table),
            "pii_decisions": pii_decisions or {},
        }

    def clear(self, table: str) -> list[str]:
        """Delete all telemetry sidecar files for a table."""
        prefix = f"{self._base(table)}/"
        return self.backend.delete_prefix(self.bucket, prefix)

    def migrate_embedded_metadata(self, table: str, contract: dict) -> bool:
        """One-time migration: move embedded operational fields out of a legacy contract."""
        embedded = extract_operational_metadata(contract)
        if not embedded:
            return False
        if embedded.get("provenance"):
            for entry in embedded["provenance"]:
                self.append_provenance(table, entry)
        if embedded.get("pii_summary"):
            self.set_pii_summary(table, embedded["pii_summary"])
        if embedded.get("last_updated"):
            self.update_telemetry(
                table,
                last_updated=embedded["last_updated"],
                last_updated_by_workflow=embedded.get("last_updated_by_workflow", ""),
                version=contract.get("version", ""),
                contract_uuid=contract.get("contract_uuid", ""),
            )
        return True
