"""
redibis.services.browse_service
================================
BrowseService — read-only browsing of contracts, scan runs, and artifacts.

Pure business logic. No REST/CLI knowledge. Thin adapters call this service.

Capabilities
------------
  - List all tables with active contracts
  - Get the active contract for a table
  - Get contract history (audit trail) for a table
  - List scan runs for a table
  - Get a specific run manifest
  - Get run artifacts (links to reports, contracts, detections)
  - Get a specific artifact content
  - Compare two contract versions (diff)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from redibis.contracts.privacy import column_is_pii
from redibis.store.contract_store import ContractStore, TableHistoryEntry
from redibis.store.storage_backend import StorageBackend


def _column_is_pii_summary(prop: dict) -> bool:
    return column_is_pii(prop)
from redibis.store.run_output_writer import RunOutputWriter

log = logging.getLogger(__name__)


@dataclass
class ContractSummary:
    """Lightweight summary of a contract for listing views."""
    table:               str
    name:                str   = ""
    version:             str   = ""
    status:              str   = ""
    total_columns:       int   = 0
    pii_columns:         int   = 0
    quality_rules:       int   = 0
    last_updated:        str   = ""
    highest_sensitivity: str   = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class RunSummary:
    """Lightweight summary of a scan run."""
    run_id:              str
    table:               str   = ""
    workflow:            str   = ""
    status:              str   = ""
    timestamp:           str   = ""
    quality_passed:      int   = 0
    quality_failed:      int   = 0
    pii_detected:        int   = 0
    artifacts:           dict  = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


class BrowseService:
    """
    Read-only service for browsing contracts and scan results.

    Usage:
        service = BrowseService(backend=backend, store=store)

        # List all tables
        tables = service.list_tables()

        # Get active contract
        contract = service.get_contract("telecom.customers")

        # Get history
        history = service.get_history("telecom.customers")

        # List runs
        runs = service.list_runs("telecom.customers")

        # Get specific run
        manifest = service.get_run("telecom.customers", "2026-05-13_10-00-00")
    """

    def __init__(
        self,
        backend:        StorageBackend,
        store:          ContractStore,
        runs_bucket:    str = "pii-reports",
    ) -> None:
        self.backend      = backend
        self.store         = store
        self.runs_bucket   = runs_bucket

    # ── Contract browsing ─────────────────────────────────────────────────

    def list_tables(self) -> list[ContractSummary]:
        """List all tables with active contracts, with summary metadata."""
        tables = self.store.list_tables()
        summaries = []
        for table in tables:
            contract = self.store.get_active(table)
            if contract:
                summaries.append(self._contract_to_summary(table, contract))
            else:
                summaries.append(ContractSummary(table=table))
        return summaries

    def get_contract(self, table: str) -> Optional[dict]:
        """Get the full active contract for a table."""
        return self.store.get_active(table)

    def get_contract_yaml(self, table: str) -> Optional[str]:
        """Get the active contract as a YAML string."""
        contract = self.store.get_active(table)
        if contract is None:
            return None
        return yaml.safe_dump(
            contract, default_flow_style=False,
            sort_keys=False, allow_unicode=True,
        )

    def get_contract_summary(self, table: str) -> Optional[ContractSummary]:
        """Get a lightweight summary of the active contract."""
        contract = self.store.get_active(table)
        if contract is None:
            return None
        return self._contract_to_summary(table, contract)

    def get_history(
        self,
        table: str,
        limit: int = 50,
    ) -> list[TableHistoryEntry]:
        """Get the audit history (all upserts) for a table."""
        return self.store.get_history(table, limit=limit)

    def get_contract_at_version(
        self,
        table:    str,
        run_uuid: str,
    ) -> Optional[dict]:
        """Get the contract as it was at a specific audit snapshot."""
        return self.store.get_audit_snapshot(table, run_uuid)

    def diff_contracts(
        self,
        table:   str,
        uuid_a:  str,
        uuid_b:  str,
    ) -> dict:
        """
        Compare two contract versions and return the diff.

        Returns a dict with:
            added    : fields/columns present in B but not A
            removed  : fields/columns present in A but not B
            changed  : fields with different values between A and B
        """
        contract_a = self.store.get_audit_snapshot(table, uuid_a)
        contract_b = self.store.get_audit_snapshot(table, uuid_b)

        if not contract_a or not contract_b:
            return {"error": "One or both snapshots not found"}

        return self._compute_diff(contract_a, contract_b)

    # ── Scan run browsing ─────────────────────────────────────────────────

    def list_runs(
        self,
        table:    Optional[str] = None,
        limit:    int           = 50,
        workflow: Optional[str] = None,
    ) -> list[RunSummary]:
        """
        List scan runs, optionally filtered by table and/or workflow.

        Reads from the run index in the contracts audit trail.
        """
        if table:
            history = self.store.get_history(table, limit=limit)
            runs = []
            for entry in history:
                if workflow and entry.workflow != workflow:
                    continue
                runs.append(RunSummary(
                    run_id    = entry.run_id,
                    table     = table,
                    workflow  = entry.workflow,
                    timestamp = entry.timestamp,
                ))
            return runs

        # No table filter — list all tables and aggregate
        tables = self.store.list_tables()
        all_runs = []
        for t in tables:
            history = self.store.get_history(t, limit=limit)
            for entry in history:
                if workflow and entry.workflow != workflow:
                    continue
                all_runs.append(RunSummary(
                    run_id    = entry.run_id,
                    table     = t,
                    workflow  = entry.workflow,
                    timestamp = entry.timestamp,
                ))

        all_runs.sort(key=lambda r: r.timestamp, reverse=True)
        return all_runs[:limit]

    def get_run(self, table: str, run_id: str) -> Optional[dict]:
        """Get the run manifest for a specific scan run."""
        key = f"scan/{table}/{run_id}/run_manifest.json"
        try:
            content = self.backend.get_text(self.runs_bucket, key)
            return json.loads(content) if content else None
        except Exception:
            return None

    def get_run_artifact(
        self,
        table:         str,
        run_id:        str,
        artifact_name: str,
    ) -> Optional[str]:
        """Get the content of a specific run artifact."""
        key = f"scan/{table}/{run_id}/{artifact_name}"
        try:
            return self.backend.get_text(self.runs_bucket, key)
        except Exception:
            return None

    # ── Export ─────────────────────────────────────────────────────────────

    def export_contract(
        self,
        table:  str,
        format: str,
    ) -> Optional[str]:
        """
        Export the active contract for a table to any supported format.
        Delegates to redibis.contracts.exporter.
        """
        contract = self.store.get_active(table)
        if contract is None:
            return None

        from redibis.contracts.exporter import export_contract
        return export_contract(contract, format)

    # ── Private helpers ───────────────────────────────────────────────────

    def _contract_to_summary(self, table: str, contract: dict) -> ContractSummary:
        """Extract a lightweight summary from a full contract dict."""
        schema = contract.get("schema", [{}])
        props = schema[0].get("properties", []) if schema else []

        pii_count = sum(
            1 for p in props
            if _column_is_pii_summary(p)
        )

        quality_count = sum(
            len(p.get("quality", []))
            for p in props
        )
        # Also count table-level quality rules
        if schema:
            quality_count += len(schema[0].get("quality", []))

        from redibis.contracts.privacy import col_privacy_classification
        classifications = [col_privacy_classification(p) for p in props if column_is_pii(p)]
        if any(cl == "pii_sensitive" for cl in classifications):
            highest = "pii_sensitive"
        elif any(cl == "pii_personal" for cl in classifications):
            highest = "pii_personal"
        elif any(cl == "pii_indirect" for cl in classifications):
            highest = "pii_indirect"
        elif classifications:
            highest = "pii_personal"
        else:
            highest = ""

        return ContractSummary(
            table               = table,
            name                = contract.get("name", ""),
            version             = contract.get("version", ""),
            status              = contract.get("status", ""),
            total_columns       = len(props),
            pii_columns         = pii_count,
            quality_rules       = quality_count,
            last_updated        = contract.get("contractCreatedTs", ""),
            highest_sensitivity = highest,
        )

    @staticmethod
    def _compute_diff(a: dict, b: dict) -> dict:
        """Simple structural diff between two contract dicts."""
        props_a = {}
        props_b = {}

        for p in a.get("schema", [{}])[0].get("properties", []):
            props_a[p["name"]] = p
        for p in b.get("schema", [{}])[0].get("properties", []):
            props_b[p["name"]] = p

        added   = [name for name in props_b if name not in props_a]
        removed = [name for name in props_a if name not in props_b]
        changed = {}

        for name in set(props_a.keys()) & set(props_b.keys()):
            pa, pb = props_a[name], props_b[name]
            col_changes = {}
            all_keys = set(pa.keys()) | set(pb.keys())
            for key in all_keys:
                va, vb = pa.get(key), pb.get(key)
                if va != vb:
                    col_changes[key] = {"before": va, "after": vb}
            if col_changes:
                changed[name] = col_changes

        return {
            "added":   added,
            "removed": removed,
            "changed": changed,
            "version_a": a.get("version"),
            "version_b": b.get("version"),
        }