"""
ContractStore — top-layer service for ODCS contract storage.

Single rule: every contract write goes through ContractStore.upsert().
There is no other writer to the contracts bucket. This guarantees:
  - All identity validation happens in one place
  - Auto-merge on existing contracts is automatic, not opt-in
  - Audit trail is consistent across all workflows
  - The webapp / CLI / batch import all use the same code path

Storage layout (in contracts bucket):
    active/{db}.{table}.yaml           ← latest merged state (overwritten on upsert)
    audit/{db}.{table}/{uuid}.yaml     ← immutable per-upsert snapshot
    audit/{db}.{table}/_run_index.jsonl ← append-only run index for fast listing
    business/{db}.{table}.yaml         ← (optional) human-curated business contracts
    merged/{db}.{table}.yaml           ← (optional) explicit merge outputs

The "fetch + merge before write" pattern (locked architecture decision):
    upsert(partial, table, workflow):
        existing = fetch active/{table}.yaml if exists
        merged   = merge_two_contracts(existing, partial, workflow=workflow)
        write    active/{table}.yaml = merged       (always latest)
        write    audit/{table}/{uuid}.yaml = merged (immutable)
        append   audit/{table}/_run_index.jsonl

Optional Tier-2 (Iceberg + Nessie) extension is mentioned in the plan but
not implemented here — current implementation uses S3-only audit. The
upsert flow is identical; only the audit storage changes.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import os

log = logging.getLogger(__name__)

from redibis.store.merger import (
    merge_two_contracts,
    IdentityConflictError,
    make_provenance_entry,
)
from redibis.store.pii_decisions import (
    PiiDecision,
    PiiDecisionStore,
    evaluate_and_mark_drift,
    reconcile_pii_columns,
    compute_pii_summary,
)
from redibis.store.verdict_import_log import VerdictImportLog
from redibis.store.contract_metadata import (
    ContractMetadataStore,
    slim_contract,
    extract_operational_metadata,
)
from redibis.store.quality_decisions import (
    QualityDecision,
    QualityDecisionStore,
    reconcile_quality_rules,
)
from redibis.store.definition_decisions import (
    DefinitionDecisionStore,
    reconcile_definition_decisions,
)
from redibis.store.storage_backend import StorageBackend


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UpsertResult:
    """Result of a contract upsert operation."""
    table:               str          # 'telecom.customers'
    contract_uuid:       str          # locked identity UUID
    run_uuid:            str          # this upsert's audit UUID
    version_before:      Optional[str] # version before merge, None if first creation
    version_after:       str          # version after merge
    is_new:              bool         # True if first creation
    active_key:          str          # S3 key of the active contract
    audit_key:           str          # S3 key of the audit snapshot
    workflow:            str          # workflow that triggered the upsert
    timestamp:           str          # ISO 8601 UTC

    def to_dict(self) -> dict:
        return {
            "table":          self.table,
            "contract_uuid":  self.contract_uuid,
            "run_uuid":       self.run_uuid,
            "version_before": self.version_before,
            "version_after":  self.version_after,
            "is_new":         self.is_new,
            "active_key":     self.active_key,
            "audit_key":      self.audit_key,
            "workflow":       self.workflow,
            "timestamp":      self.timestamp,
        }


@dataclass
class TableHistoryEntry:
    """One row from the audit run index."""
    run_uuid:        str
    run_id:          str
    workflow:        str
    timestamp:       str
    version:         str
    audit_key:       str
    contributed_fields: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _table_safe(table: str) -> str:
    """Convert 'db.table' into a key-safe form. Keeps the dot as-is for clarity."""
    # Active and business prefixes use 'db.table.yaml' verbatim.
    return table


def _split_db_table(table: str) -> tuple[str, str]:
    """'telecom.customers' → ('telecom', 'customers'). Empty db if no dot."""
    if "." in table:
        db, tbl = table.split(".", 1)
        return db, tbl
    return "", table


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# ContractStore
# ─────────────────────────────────────────────────────────────────────────────

class ContractStore:
    """
    Top-layer service for ODCS contract storage.

    Every write is a smart upsert: fetches the current active contract for
    the same {db}.{table}, merges it with the incoming partial, then writes
    the merged result back. Identity (contract_uuid, database_name,
    table_name) is locked after first write.

    This service has 4 public methods:
        upsert(partial, table, workflow, run_id)  → UpsertResult
        get_active(table)                          → dict | None
        get_history(table, limit=50)              → list[TableHistoryEntry]
        get_audit_snapshot(table, run_uuid)       → dict | None

    Plus 1 admin helper:
        list_tables()                              → list[str]
    """

    # Bucket prefixes
    ACTIVE_PREFIX:     str = "active"
    AUDIT_PREFIX:      str = "audit"
    BUSINESS_PREFIX:   str = "business"
    MERGED_PREFIX:     str = "merged"
    ENRICHMENT_PREFIX: str = "enrichment"

    def __init__(
        self,
        backend: StorageBackend,
        bucket:  str,
        metadata_bucket: Optional[str] = None,
        *,
        memory_config: Optional["MemoryConfig"] = None,
    ):
        from redibis.config import MemoryConfig

        self.backend = backend
        self.bucket  = bucket
        self.memory_config = memory_config or MemoryConfig()
        from redibis.memory.consent import SamplingConsentStore
        self.sampling_consent = SamplingConsentStore(backend, bucket)
        self._memory_store = None
        if self.memory_config.enabled:
            try:
                from redibis.memory.store import get_memory_store

                self._memory_store = get_memory_store(self.memory_config)
            except Exception:
                log.warning(
                    "memory store unavailable — similarity will be skipped",
                    exc_info=True,
                )
        # Operational telemetry (provenance, pii_summary, last_updated) — separate
        # from the declarative contract spec. Uses a dedicated metadata bucket when
        # S3_METADATA_BUCKET is set; otherwise _meta/telemetry/ in the contracts bucket.
        meta_bucket = metadata_bucket or os.getenv("S3_METADATA_BUCKET") or bucket
        meta_prefix = "" if os.getenv("S3_METADATA_BUCKET") else ContractMetadataStore.DEFAULT_PREFIX
        self.metadata = ContractMetadataStore(backend, meta_bucket, prefix=meta_prefix)
        # Per-table PII decision overlay (single source of truth for which
        # columns are PII). Enforced on every upsert so it always wins over the
        # union-merge — the only way to REMOVE PII from a column.
        self.pii_decisions = PiiDecisionStore(backend, bucket)
        self.quality_decisions = QualityDecisionStore(backend, bucket)
        self.definition_decisions = DefinitionDecisionStore(backend, bucket)
        self.verdict_import_log = VerdictImportLog(backend, bucket)

    # ── Key path helpers ──────────────────────────────────────────────────

    def _active_key(self, table: str) -> str:
        return f"{self.ACTIVE_PREFIX}/{_table_safe(table)}.yaml"

    def _audit_key(self, table: str, run_uuid: str) -> str:
        return f"{self.AUDIT_PREFIX}/{_table_safe(table)}/{run_uuid}.yaml"

    def _audit_index_key(self, table: str) -> str:
        return f"{self.AUDIT_PREFIX}/{_table_safe(table)}/_run_index.jsonl"

    def _business_key(self, table: str) -> str:
        return f"{self.BUSINESS_PREFIX}/{_table_safe(table)}.yaml"

    def _merged_key(self, table: str) -> str:
        return f"{self.MERGED_PREFIX}/{_table_safe(table)}.yaml"

    def _enrichment_candidate_key(self, table: str) -> str:
        return f"{self.ENRICHMENT_PREFIX}/{_table_safe(table)}/candidate.yaml"

    def _enrichment_context_prefix(self, table: str) -> str:
        return f"{self.ENRICHMENT_PREFIX}/{_table_safe(table)}/context/"

    def _enrichment_example_prefix(self, table: str) -> str:
        return f"{self.ENRICHMENT_PREFIX}/{_table_safe(table)}/examples/"

    def _enrichment_sample_prefix(self, table: str) -> str:
        return f"{self.ENRICHMENT_PREFIX}/{_table_safe(table)}/samples/"

    def _enrichment_draft_context_key(self, table: str) -> str:
        return f"{self.ENRICHMENT_PREFIX}/{_table_safe(table)}/context_draft.json"

    # ── Read operations ───────────────────────────────────────────────────

    def get_active(self, table: str) -> Optional[dict]:
        """
        Fetch the slim active contract spec (no operational telemetry).
        Legacy contracts with embedded provenance/pii_summary are migrated on read.
        """
        key = self._active_key(table)
        if not self.backend.exists(self.bucket, key):
            return None
        try:
            raw = self.backend.get_yaml(self.bucket, key)
        except (KeyError, ValueError) as exc:
            log.warning(
                "Ignoring unreadable active contract for %s (%s): %s",
                table, key, exc,
            )
            return None
        if not raw or not isinstance(raw, dict):
            return None
        return self._normalize_active_contract(table, raw)

    def _normalize_active_contract(self, table: str, contract: dict) -> dict:
        """Migrate embedded telemetry to metadata store; return slim spec."""
        if extract_operational_metadata(contract):
            self.metadata.migrate_embedded_metadata(table, contract)
        return slim_contract(contract)

    def get_metadata(self, table: str) -> dict:
        """Operational telemetry for a table (provenance, pii_summary, last_updated)."""
        active = self.backend.get_yaml(self.bucket, self._active_key(table)) if (
            self.backend.exists(self.bucket, self._active_key(table))
        ) else None
        if active and extract_operational_metadata(active):
            self.metadata.migrate_embedded_metadata(table, active)
        return {
            "table": table,
            "telemetry": self.metadata.get_telemetry(table),
            "provenance": self.metadata.get_provenance(table),
            "pii_summary": self.metadata.get_pii_summary(table) or {},
            "pii_decisions": {
                k: v for k, v in (self.pii_decisions.get(table) or {}).items()
            },
        }

    def export_integration_package(self, table: str) -> dict:
        """Full JSON bundle: slim spec + telemetry + decisions (OpenMetadata, etc.)."""
        spec = self.get_active(table)
        if spec is None:
            raise ValueError(f"No active contract for {table!r}")
        return self.metadata.export_package(
            table,
            contract=spec,
            pii_decisions=self.pii_decisions.get(table),
        )

    def get_business(self, table: str) -> Optional[dict]:
        """Fetch a manually-stored business contract (if uploaded separately)."""
        key = self._business_key(table)
        if not self.backend.exists(self.bucket, key):
            return None
        return self.backend.get_yaml(self.bucket, key)

    def get_audit_snapshot(self, table: str, run_uuid: str) -> Optional[dict]:
        """Fetch a specific historical snapshot by run_uuid."""
        key = self._audit_key(table, run_uuid)
        if not self.backend.exists(self.bucket, key):
            return None
        return self.backend.get_yaml(self.bucket, key)

    def get_history(self, table: str, limit: int = 50) -> list[TableHistoryEntry]:
        """
        Read the run index for a table; returns most recent first.
        Reads {audit}/{table}/_run_index.jsonl line by line.
        """
        key = self._audit_index_key(table)
        if not self.backend.exists(self.bucket, key):
            return []

        text = self.backend.get_text(self.bucket, key)
        entries = []
        for line in text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                entries.append(TableHistoryEntry(
                    run_uuid           = row["run_uuid"],
                    run_id             = row.get("run_id", ""),
                    workflow           = row.get("workflow", ""),
                    timestamp          = row.get("timestamp", ""),
                    version            = row.get("version", ""),
                    audit_key          = row.get("audit_key", ""),
                    contributed_fields = row.get("contributed_fields", []),
                ))
            except (json.JSONDecodeError, KeyError):
                continue

        # Most recent first
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]

    def list_tables(self) -> list[str]:
        """List all tables that have an active contract."""
        keys = self.backend.list_keys(
            self.bucket, prefix=f"{self.ACTIVE_PREFIX}/"
        )
        tables = []
        for key in keys:
            # active/{db}.{table}.yaml
            stripped = key.replace(f"{self.ACTIVE_PREFIX}/", "", 1)
            if stripped.endswith(".yaml"):
                tables.append(stripped[:-5])
        return sorted(tables)

    # ── Contract validation ─────────────────────────────────────────────

    @staticmethod
    def validate(
        contract: dict,
        strict: bool = False,
    ) -> dict:
        """
        Validate an ODCS contract using the official Pydantic models and
        datacontract-cli linter.

        Two layers of validation:
          1. Pydantic model validation — catches structural errors (wrong types,
             unknown fields, missing required fields in nested objects)
          2. datacontract lint — validates against the ODCS JSON Schema

        Args:
            contract : The contract dict to validate.
            strict   : If True, raise ValueError on any validation failure.
                       If False (default), return the result dict silently.

        Returns:
            dict with keys:
                valid    : bool — True if the contract passes all checks
                errors   : list[str] — validation error messages
                warnings : list[str] — non-fatal warnings

        Usage:
            result = ContractStore.validate(contract)
            if not result["valid"]:
                print("Errors:", result["errors"])

            # Strict — raises on failure
            ContractStore.validate(contract, strict=True)

            # Validate on upsert
            store.upsert(contract, table="t.t", validate=True)
        """
        errors = []
        warnings = []

        # ── Layer 1: Pydantic model validation ────────────────────────────
        try:
            from redibis.contracts.odcs_compat import build_odcs_model
            odcs = build_odcs_model(contract)
        except ImportError:
            warnings.append(
                "open-data-contract-standard not installed — "
                "Pydantic validation skipped. "
                "Install with: pip install datacontract-cli"
            )
            odcs = None
        except Exception as e:
            errors.append(f"ODCS model validation: {e}")
            odcs = None

        # ── Layer 2: datacontract lint ────────────────────────────────────
        if odcs is not None:
            try:
                from datacontract.data_contract import DataContract
                dc = DataContract(data_contract=odcs)
                run = dc.lint()
                for check in run.checks:
                    if check.result in ("error", "failed"):
                        errors.append(
                            f"lint: {check.name}: {check.reason or check.result}"
                        )
                    elif check.result in ("warning", "warn"):
                        warnings.append(
                            f"lint: {check.name}: {check.reason or check.result}"
                        )
            except ImportError:
                warnings.append(
                    "datacontract-cli not installed — lint skipped."
                )
            except Exception as e:
                warnings.append(f"lint error: {e}")

        valid = len(errors) == 0

        if strict and not valid:
            raise ValueError(
                "ODCS contract validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

        return {"valid": valid, "errors": errors, "warnings": warnings}

    # ── Write operations (the smart upsert) ───────────────────────────────

    def upsert(
        self,
        partial:   dict,
        table:     str,
        workflow:  str = "manual",
        run_id:    str = "",
        validate:  bool = False,
        strip_pii_quality: bool = False,
    ) -> UpsertResult:
        """
        Smart upsert: fetch existing active contract, merge with partial,
        write merged result to active and audit prefixes.

        Args:
            partial  : Incoming partial contract dict (from a workflow).
                       Should contain {database_name, table_name} for identity.
            table    : 'db.table' identifier. Used as the storage key.
            workflow : 'ge' | 'pii' | 'business' | 'merge' | 'manual'
            run_id   : Human-readable run identifier (e.g. timestamp).
            validate : If True, validate the merged contract against the ODCS
                       schema before writing. Raises ValueError if invalid.

        Returns:
            UpsertResult with full audit metadata.

        Raises:
            IdentityConflictError if partial identity differs from existing.
            ValueError if validate=True and the merged contract is invalid.
        """
        run_uuid  = str(uuid.uuid4())
        timestamp = _utc_now_iso()

        # Auto-populate database_name / table_name on partial if missing
        db_name, tbl_name = _split_db_table(table)
        partial.setdefault("database_name", db_name)
        partial.setdefault("table_name",    tbl_name)

        # ── Fetch existing active ────────────────────────────────────────
        existing = self.get_active(table)
        version_before = existing.get("version") if existing else None
        is_new         = existing is None

        # ── Merge ────────────────────────────────────────────────────────
        merged = merge_two_contracts(
            existing = existing,
            incoming = partial,
            workflow = workflow,
            run_id   = run_id or timestamp,
            run_uuid = run_uuid,
        )

        # ── Reconcile against the PII decision overlay ────────────────────
        # The merge unions tags and never deletes omitted fields, so it can
        # never REMOVE PII from a column. The overlay is the single source of
        # truth: enforce it here so a "not_pii" decision strips PII (and a
        # "pii" decision ensures it) on every write, no matter the workflow.
        decisions = self.pii_decisions.get(table)
        if decisions:
            # Evaluate/persist steward-decision drift here — the one choke point
            # every scan and merge workflow passes through — so lifecycle state
            # changes at write time, never as a side effect of a reviewer GET.
            evaluate_and_mark_drift(table, merged, self.pii_decisions)
            decisions = self.pii_decisions.get(table)
            reconcile_pii_columns(merged, decisions)

        qd = self.quality_decisions.get(table)
        if qd:
            reconcile_quality_rules(merged, qd)

        dd = self.definition_decisions.get(table)
        if dd.get("table") or dd.get("columns"):
            reconcile_definition_decisions(merged, dd)

        # ── PII value-leak guard: strip quality rules from PII columns ────
        # min/max/quantile/validValues/length expectations embed REAL PII
        # values (e.g. a phone number's min/max, a name's validValues). If the
        # contract is shared with an external LLM those values leak.
        #
        # Scope: automated writers opt in (strip_pii_quality=True) — RunMerger.merge_run
        # callers, scan automerge, legacy Workflow A/B run_outputs, `redibis runs merge`,
        # and enrich auto-write (`EnrichmentService.enrich` / `merge_candidate`).
        # MANUAL paths (web UI merge, approved basket, patch_* edits, memory review)
        # pass the default False, so a human can deliberately keep quality rules on a PII column.
        #
        # Env overrides (apply only when strip_pii_quality is in play):
        #   REDIBIS_KEEP_QUALITY_ON_PII=1  → never strip (disable globally)
        #   REDIBIS_STRIP_PII_QUALITY_ALWAYS=1 → strip on EVERY write (incl. manual)
        #   REDIBIS_PII_QUALITY_KEEP_SAFE=1 → keep value-free count rules
        # Table-level quality (rowCount / column set) is never touched.
        import os as _os
        _env = lambda k: _os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on")
        _do_strip = strip_pii_quality or _env("REDIBIS_STRIP_PII_QUALITY_ALWAYS")
        if _do_strip and not _env("REDIBIS_KEEP_QUALITY_ON_PII"):
            from redibis.contracts.privacy import strip_quality_from_pii_columns
            strip_quality_from_pii_columns(
                merged, keep_safe_rules=_env("REDIBIS_PII_QUALITY_KEEP_SAFE"),
            )

        # ── Operational metadata (outside the contract spec) ───────────
        provenance_entry = make_provenance_entry(
            partial, workflow, run_id or timestamp, run_uuid,
        )
        prev_summary = self.metadata.get_pii_summary(table) or {}
        for src in (merged.get("pii_summary"), merged.get("_scan_metadata"), partial.get("_scan_metadata")):
            if isinstance(src, dict):
                prev_summary = {**prev_summary, **src}
        pii_summary = compute_pii_summary(merged, preserve=prev_summary)

        column_telemetry = partial.get("_column_telemetry")
        if isinstance(column_telemetry, dict) and column_telemetry:
            self.metadata.merge_column_telemetry(table, column_telemetry)

        slim = slim_contract(merged)

        # ── Validate (optional) ──────────────────────────────────────────
        if validate:
            self.validate(slim, strict=True)

        # ── Write slim spec to active + audit ────────────────────────────
        active_key = self._active_key(table)
        self.backend.put_yaml(self.bucket, active_key, slim)

        audit_key = self._audit_key(table, run_uuid)
        self.backend.put_yaml(self.bucket, audit_key, slim)

        # ── Write telemetry sidecar ──────────────────────────────────────
        self.metadata.append_provenance(table, provenance_entry)
        self.metadata.set_pii_summary(table, pii_summary)
        self.metadata.update_telemetry(
            table,
            last_updated=timestamp,
            last_updated_by_workflow=workflow,
            version=slim.get("version", "1.0.0"),
            contract_uuid=slim.get("contract_uuid", ""),
        )

        # ── Append to run index ─────────────────────────────────────────
        self._append_run_index(
            table              = table,
            run_uuid           = run_uuid,
            run_id             = run_id or timestamp,
            workflow           = workflow,
            timestamp          = timestamp,
            version            = slim.get("version", "1.0.0"),
            audit_key          = audit_key,
            contributed_fields = provenance_entry.get("contributed_fields", []),
        )

        return UpsertResult(
            table          = table,
            contract_uuid  = slim.get("contract_uuid", ""),
            run_uuid       = run_uuid,
            version_before = version_before,
            version_after  = slim.get("version", "1.0.0"),
            is_new         = is_new,
            active_key     = active_key,
            audit_key      = audit_key,
            workflow       = workflow,
            timestamp      = timestamp,
        )

    def upsert_business(
        self,
        contract:  dict,
        table:     str,
        run_id:    str = "",
    ) -> UpsertResult:
        """
        Convenience method for business glossary import.
        Same as upsert(workflow='business') but also writes to /business/ prefix
        as a separate immutable copy of the business-only definition.
        """
        # Store immutable business-only copy
        business_key = self._business_key(table)
        self.backend.put_yaml(self.bucket, business_key, contract)

        # Standard upsert
        return self.upsert(
            partial  = contract,
            table    = table,
            workflow = "business",
            run_id   = run_id,
        )

    def write_merged(
        self,
        merged:   dict,
        table:    str,
    ) -> str:
        """
        Write a merged contract to the /merged/ prefix.
        Used by the webapp's "Generate Merged Contract" button — returns
        the merge result without going through the smart upsert flow
        (since it doesn't carry new information, just combines existing).
        """
        key = self._merged_key(table)
        self.backend.put_yaml(self.bucket, key, merged)
        return key

    # ── PII decisions (strip / add PII on a column) ──────────────────────

    def get_pii_decisions(self, table: str) -> dict[str, dict]:
        """Return the per-column PII decision overlay for a table."""
        return self.pii_decisions.get(table)

    def column_names(self, table: str) -> list[str]:
        """List the column names present in the active contract's schema."""
        active = self.get_active(table)
        if active is None:
            return []
        names: list[str] = []
        for schema_obj in active.get("schema", []) or []:
            for prop in schema_obj.get("properties", []) or []:
                name = prop.get("name") if isinstance(prop, dict) else None
                if name is not None and name not in names:
                    names.append(name)
        return names

    def set_pii_decision(
        self,
        table:       str,
        column:      str,
        status:      str,
        *,
        entity_type: Optional[str] = None,
        payload:     Optional[dict] = None,
        decided_by:  str = "",
        run_id:      str = "",
        engine_is_pii: Optional[bool] = None,
        engine_entity_type: Optional[str] = None,
        engine_confidence: Optional[float] = None,
        engine_decision_rule: Optional[str] = None,
        capture_engine_baseline: bool = True,
        fingerprint_key: str = "",
        name_normalized: str = "",
        logical_type: str = "",
        physical_type: str = "",
        format_signature: str = "",
        evidence_digest: str = "",
        reason: str = "",
        lifecycle_state: str = "active",
        decision_version: Optional[int] = None,
    ) -> UpsertResult:
        """Record a PII decision for a column and apply it to the contract.

        ``status="not_pii"`` strips every PII signal from the column (demote to a
        normal column). ``status="pii"`` ensures the column carries the PII signal
        in ``payload`` (used to add a PII column the scan missed).

        When ``capture_engine_baseline`` is True (default), the current column
        telemetry snapshot is stored on the decision so training export can mark
        ``corrected_engine`` without a join-time guess. Explicit ``engine_*``
        kwargs override the telemetry snapshot.

        The decision is persisted to the overlay, then the active contract is
        re-upserted (a no-op merge of the contract with itself) so the standard
        upsert path reconciles and writes it — keeping ContractStore the single
        writer and producing a normal audited version bump.

        Raises:
            ValueError if no active contract exists or the column is absent.
        """
        if status not in ("pii", "not_pii"):
            raise ValueError("status must be 'pii' or 'not_pii'")

        active = self.get_active(table)
        if active is None:
            raise ValueError(
                f"No active contract for {table!r} — create it (scan/import) first."
            )
        if column not in self.column_names(table):
            raise ValueError(
                f"Column {column!r} is not in the {table!r} contract schema."
            )

        from redibis.store.pii_decisions import infer_engine_baseline_from_telemetry

        baseline = {
            "engine_is_pii": None,
            "engine_entity_type": None,
            "engine_confidence": None,
            "engine_decision_rule": None,
        }
        if capture_engine_baseline:
            tel = (self.metadata.get_column_telemetry(table) or {}).get(column) or {}
            baseline = infer_engine_baseline_from_telemetry(tel)
        if engine_is_pii is not None:
            baseline["engine_is_pii"] = engine_is_pii
        if engine_entity_type is not None:
            baseline["engine_entity_type"] = engine_entity_type
        if engine_confidence is not None:
            baseline["engine_confidence"] = float(engine_confidence)
        if engine_decision_rule is not None:
            baseline["engine_decision_rule"] = engine_decision_rule

        # Every steward decision is fingerprint-bound by default — callers that
        # don't supply one (strip/add PII buttons, share-strip, CLI, lifecycle
        # purge) get it derived from the active contract's column property so
        # no new "legacy" un-fingerprinted authority is created (plan §5).
        if not fingerprint_key:
            from redibis.review.fingerprint import fingerprint_metadata_from_prop

            col_prop: dict = {}
            for schema_obj in active.get("schema", []) or []:
                for prop in schema_obj.get("properties", []) or []:
                    if isinstance(prop, dict) and prop.get("name") == column:
                        col_prop = prop
                        break
            fp_meta = fingerprint_metadata_from_prop(column, col_prop)
            fingerprint_key = fp_meta["fingerprint_key"]
            name_normalized = name_normalized or fp_meta["name_normalized"]
            logical_type = logical_type or fp_meta["logical_type"]
            physical_type = physical_type or fp_meta["physical_type"]
            format_signature = format_signature or fp_meta["format_signature"]

        decision = PiiDecision(
            column=column, status=status, entity_type=entity_type,
            payload=payload or {}, decided_by=decided_by, run_id=run_id,
            engine_is_pii=baseline["engine_is_pii"],
            engine_entity_type=baseline["engine_entity_type"],
            engine_confidence=baseline["engine_confidence"],
            engine_decision_rule=baseline["engine_decision_rule"],
            fingerprint_key=fingerprint_key,
            name_normalized=name_normalized,
            logical_type=logical_type,
            physical_type=physical_type,
            format_signature=format_signature,
            evidence_digest=evidence_digest,
            lifecycle_state=lifecycle_state or "active",
            decision_version=int(decision_version or 1),
            reason=reason,
        )
        self.pii_decisions.set(table, decision)

        # Re-upsert the active contract: the merge is a self no-op, then the
        # reconcile step (in upsert) enforces the freshly-saved decision.
        import copy
        partial = copy.deepcopy(active)
        workflow = "demote-pii" if status == "not_pii" else "add-pii"
        result = self.upsert(
            partial  = partial,
            table    = table,
            workflow = workflow,
            run_id   = run_id or f"{workflow}:{column}",
        )
        from redibis.memory.writer import record_contract_pii_decision
        record_contract_pii_decision(
            self,
            table,
            column,
            status=status,
            payload=payload,
            upsert_result=result,
            memory_config=self.memory_config,
            decided_by=decided_by,
            run_id=run_id,
        )
        return result

    def set_sampling_consent(
        self,
        table: str,
        column: str,
        *,
        approved: bool,
        approved_by: str = "",
    ) -> dict:
        """Record steward consent for persisting masked/hashed column samples."""
        return self.sampling_consent.set_approved(
            table, column, approved=approved, approved_by=approved_by,
        )

    def get_sampling_consent(self, table: str) -> list[dict]:
        return self.sampling_consent.list(table)

    def clear_pii_decision(self, table: str, column: str) -> bool:
        """Stop enforcing a column's PII decision (does not restore old metadata)."""
        return self.pii_decisions.remove(table, column)

    def _reapply_overlays(self, table: str, *, workflow: str, run_id: str) -> UpsertResult:
        """Re-upsert the active contract so overlay reconciles run (no merge delta)."""
        import copy
        active = self.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        return self.upsert(
            partial=copy.deepcopy(active),
            table=table,
            workflow=workflow,
            run_id=run_id,
        )

    def get_pii_view(self, table: str) -> dict:
        from redibis.services.contract_views import build_pii_view
        active = self.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        return build_pii_view(active, self.pii_decisions.get(table))

    def patch_column_privacy(
        self,
        table: str,
        column: str,
        *,
        entity_type: Optional[str] = None,
        classification: Optional[str] = None,
        tags: Optional[list] = None,
        masking_policy: Optional[dict] = None,
        strip: bool = False,
        decided_by: str = "",
        run_id: str = "",
    ) -> UpsertResult:
        """Full PII column edit: privacy block, classification, tags (replace), masking."""
        if column not in self.column_names(table):
            raise ValueError(f"Column {column!r} is not in the {table!r} contract schema.")
        if strip:
            return self.set_pii_decision(
                table, column, "not_pii", decided_by=decided_by, run_id=run_id,
            )

        from redibis.contracts.privacy import build_privacy_block

        pii_block: dict = {"detected": True}
        if entity_type:
            pii_block["entity_type"] = entity_type
        legacy_mask = None
        if masking_policy:
            legacy_mask = {
                "default": masking_policy.get("default_strategy", "mask"),
                "reversible": bool(masking_policy.get("reversible", False)),
            }
            if masking_policy.get("role_overrides"):
                legacy_mask["roles"] = masking_policy["role_overrides"]
        privacy = build_privacy_block(pii_block, legacy_mask) or {}
        if entity_type and privacy:
            ce = privacy.setdefault("classification_engine", {})
            ce["entity_type"] = entity_type
            ce["detected"] = True

        payload: dict = {"privacy": privacy}
        if classification is not None:
            payload["classification"] = classification
        if tags is not None:
            payload["tags"] = list(tags)
            payload["_replace_tags"] = True

        return self.set_pii_decision(
            table, column, "pii",
            entity_type=entity_type,
            payload=payload,
            decided_by=decided_by,
            run_id=run_id or f"patch-pii:{column}",
        )

    def get_quality_view(self, table: str) -> dict:
        from redibis.services.contract_views import build_quality_view
        active = self.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        return build_quality_view(active, self.quality_decisions.get(table))

    def suppress_quality_rule(
        self,
        table: str,
        rule_id: str,
        *,
        column: Optional[str] = None,
        decided_by: str = "",
        run_id: str = "",
    ) -> UpsertResult:
        decision = QualityDecision(
            rule_id=rule_id, status="suppressed", column=column,
            decided_by=decided_by, run_id=run_id,
        )
        self.quality_decisions.set(table, decision)
        return self._reapply_overlays(
            table, workflow="quality-suppress", run_id=run_id or f"suppress:{rule_id}",
        )

    def suppress_all_quality_rules(
        self,
        table: str,
        *,
        decided_by: str = "",
        run_id: str = "",
    ) -> UpsertResult:
        view = self.get_quality_view(table)
        for r in view.get("rules", []):
            self.quality_decisions.set(table, QualityDecision(
                rule_id=r["rule_id"],
                status="suppressed",
                column=r.get("column"),
                decided_by=decided_by,
                run_id=run_id,
            ))
        return self._reapply_overlays(
            table,
            workflow="quality-suppress-all",
            run_id=run_id or "suppress-all",
        )

    def restore_quality_rule(self, table: str, rule_id: str) -> UpsertResult:
        self.quality_decisions.remove(table, rule_id)
        return self._reapply_overlays(
            table, workflow="quality-restore", run_id=f"restore:{rule_id}",
        )

    def add_manual_quality_rule(
        self,
        table: str,
        rule_payload: dict,
        *,
        rule_id: Optional[str] = None,
        column: Optional[str] = None,
        source_rule_id: Optional[str] = None,
        suppress_source: bool = False,
        decided_by: str = "",
        run_id: str = "",
    ) -> UpsertResult:
        from redibis.contracts.rules import stable_rule_id
        rid = rule_id or stable_rule_id(column, rule_payload)
        if suppress_source and source_rule_id:
            self.quality_decisions.set(table, QualityDecision(
                rule_id=source_rule_id, status="suppressed", column=column,
                decided_by=decided_by, run_id=run_id,
            ))
        self.quality_decisions.set(table, QualityDecision(
            rule_id=rid, status="manual", column=column, payload=rule_payload,
            source_rule_id=source_rule_id, decided_by=decided_by, run_id=run_id,
        ))
        return self._reapply_overlays(
            table, workflow="quality-manual", run_id=run_id or f"manual:{rid}",
        )

    def get_definitions_view(self, table: str) -> dict:
        from redibis.services.contract_views import build_definitions_view
        active = self.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        return build_definitions_view(active, self.definition_decisions.get(table))

    def patch_definitions(
        self,
        table: str,
        *,
        table_patch: Optional[dict] = None,
        column_patches: Optional[dict[str, dict]] = None,
        decided_by: str = "",
        run_id: str = "",
    ) -> UpsertResult:
        if table_patch:
            self.definition_decisions.patch_table(table, table_patch, decided_by=decided_by)
        for col, patch in (column_patches or {}).items():
            if col not in self.column_names(table):
                raise ValueError(f"Column {col!r} is not in the {table!r} contract schema.")
            self.definition_decisions.patch_column(table, col, patch, decided_by=decided_by)
        result = self._reapply_overlays(
            table, workflow="definitions-patch",
            run_id=run_id or "definitions-patch",
        )
        from redibis.memory.writer import record_contract_definitions_patch
        record_contract_definitions_patch(
            self,
            table,
            column_patches,
            upsert_result=result,
            memory_config=self.memory_config,
            decided_by=decided_by,
            run_id=run_id,
        )
        return result

    # ── Retention (table-level TTL) ──────────────────────────────────────

    def get_retention(self, table: str) -> dict:
        """
        Return the table's retention policy, defaulting to ``value="NA"`` when
        the contract is missing or declares no retention.
        """
        from redibis.contracts.retention import get_retention, NA
        active = self.get_active(table)
        if active is None:
            return {"value": NA, "unit": NA, "driver": None,
                    "element": None, "set": False}
        return get_retention(active)

    def set_retention(
        self,
        table:    str,
        value:    Any = "NA",
        unit:     str = "NA",
        driver:   Optional[str] = None,
        element:  Optional[str] = None,
        run_id:   str = "",
    ) -> UpsertResult:
        """
        Set/replace the table-level retention (TTL) and persist it.

        Goes through the standard smart upsert (the only writer), so the change
        is versioned and audited like any other contract edit. The active
        contract must already exist.

        Args:
            value   : retention duration (int) or ``"NA"`` to mark it unset.
            unit    : duration unit (``d``, ``m``, ``y`` …); ignored when value is NA.
            driver  : optional reason (e.g. ``"regulatory"``, ``"legal_hold"``).
            element : optional column the TTL is measured from.
            run_id  : human-readable run identifier for the audit trail.

        Raises:
            ValueError if no active contract exists for ``table``.
        """
        from redibis.contracts.retention import build_retention_partial

        active = self.get_active(table)
        if active is None:
            raise ValueError(
                f"No active contract for {table!r} — create it (scan/import) "
                "before setting retention."
            )
        physical = None
        for schema_obj in active.get("schema", []) or []:
            if schema_obj.get("physicalName"):
                physical = schema_obj["physicalName"]
                break

        partial = build_retention_partial(
            table, value, unit=unit, driver=driver, element=element,
            physical_name=physical,
        )
        return self.upsert(
            partial  = partial,
            table    = table,
            workflow = "manual",
            run_id   = run_id or "retention-update",
        )

    # ── Purge (fresh start) ────────────────────────────────────────────────

    def purge(self, table: str) -> dict:
        """
        Delete the active contract + ALL its support data (audit snapshots,
        run index, business layer, merged output, enrichment candidate +
        context docs, decision overlays, operational telemetry) for a table.

        This is the v2 "fresh start" delete. After a purge, the next scan/merge
        creates a brand-new contract with a NEW contract_uuid (because the merger
        generates a fresh UUID when no active contract exists).

        Run-bucket subcontracts (pii-contracts / quality-contracts) are NOT
        touched here — callers decide whether to keep them (SubcontractStore
        .delete_table_runs handles that), so good runs can be re-merged after
        a purge.

        Returns a summary of what was deleted.
        """
        deleted: list[str] = []

        # Single-object keys
        for key in (
            self._active_key(table),
            self._business_key(table),
            self._merged_key(table),
            self._enrichment_candidate_key(table),
        ):
            if self.backend.exists(self.bucket, key):
                self.backend.delete(self.bucket, key)
                deleted.append(key)

        # Prefix-scoped data (audit snapshots + index, enrichment context docs)
        for prefix in (
            f"{self.AUDIT_PREFIX}/{_table_safe(table)}/",
            f"{self.ENRICHMENT_PREFIX}/{_table_safe(table)}/",
        ):
            deleted.extend(self.backend.delete_prefix(self.bucket, prefix))

        # Decision overlays + telemetry (so a re-scan is a true fresh start)
        self.pii_decisions.clear(table)
        self.quality_decisions.clear(table)
        self.definition_decisions.clear(table)
        deleted.extend(self.metadata.clear(table))

        return {
            "table": table,
            "deleted_keys": deleted,
            "deleted_count": len(deleted),
            "purged": len(deleted) > 0,
            "cleared_overlays": [
                "pii_decisions", "quality_decisions",
                "definition_decisions", "telemetry",
            ],
        }

    # ── Internal: append-only run index ──────────────────────────────────

    def _append_run_index(
        self,
        table:              str,
        run_uuid:           str,
        run_id:             str,
        workflow:           str,
        timestamp:          str,
        version:            str,
        audit_key:          str,
        contributed_fields: list[str],
    ) -> None:
        """Append one JSONL row to the table's run index."""
        index_key = self._audit_index_key(table)
        row = {
            "run_uuid":            run_uuid,
            "run_id":              run_id,
            "workflow":            workflow,
            "timestamp":           timestamp,
            "version":             version,
            "audit_key":           audit_key,
            "contributed_fields":  contributed_fields,
        }
        new_line = json.dumps(row, ensure_ascii=False) + "\n"

        # Read existing + append (S3 has no append; full read-write is acceptable
        # since per-table run index stays small)
        existing_text = ""
        if self.backend.exists(self.bucket, index_key):
            existing_text = self.backend.get_text(self.bucket, index_key)

        self.backend.put_text(
            self.bucket, index_key,
            existing_text + new_line,
            content_type="application/x-ndjson",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    from redibis.store.storage_backend import LocalBackend

    print("── ContractStore smoke test ────────────────────────")

    with tempfile.TemporaryDirectory() as td:
        backend = LocalBackend(td)
        store   = ContractStore(backend=backend, bucket="contracts")

        # 1. First insert
        ge_partial = {
            "apiVersion": "v3.0.1",
            "kind":       "DataContract",
            "name":       "telecom_customers_contract",
            "schema": [{
                "name": "telecom_customers",
                "physicalName": "telecom.customers",
                "properties": [
                    {
                        "name": "phone",
                        "logicalType": "string",
                        "quality": [{
                            "rule": "expect_column_values_to_not_be_null",
                            "engine": "greatExpectations",
                        }],
                    },
                ],
            }],
        }
        result1 = store.upsert(
            partial  = ge_partial,
            table    = "telecom.customers",
            workflow = "ge",
            run_id   = "2026-05-09_10-00-00",
        )
        assert result1.is_new is True
        assert result1.contract_uuid
        assert result1.version_after == "1.0.0"
        print(f"✓ First insert created contract uuid={result1.contract_uuid[:8]}...")

        # 2. Verify active exists
        active = store.get_active("telecom.customers")
        assert active is not None
        assert active["database_name"] == "telecom"
        assert active["table_name"]    == "customers"
        print("✓ get_active returns the active contract")

        # 3. PII workflow upsert — adds pii block, tags
        pii_partial = {
            "schema": [{
                "name": "telecom_customers",
                "properties": [
                    {
                        "name": "phone",
                        "tags": ["pii", "gdpr_personal_data"],
                        "classification": "pii_personal",
                        "pii": {
                            "detected": True,
                            "entity_type": "PHONE_NUMBER",
                            "confidence": 0.92,
                        },
                    },
                ],
            }],
        }
        result2 = store.upsert(
            partial  = pii_partial,
            table    = "telecom.customers",
            workflow = "pii",
            run_id   = "2026-05-09_14-32-18",
        )
        assert result2.is_new is False
        assert result2.contract_uuid == result1.contract_uuid  # identity preserved
        assert result2.version_after == "1.0.1"
        print(f"✓ PII upsert preserved identity, version → {result2.version_after}")

        # 4. Verify merge — quality from GE preserved, pii added
        active2 = store.get_active("telecom.customers")
        phone   = active2["schema"][0]["properties"][0]
        assert phone["quality"]                  # preserved from GE
        assert phone["pii"]["detected"] is True  # added by PII
        assert "pii" in phone["tags"]            # added by PII
        print("✓ Active contract has merged GE quality + PII metadata")

        # 5. Business import — adds description, businessName, more tags
        business_partial = {
            "schema": [{
                "name": "telecom_customers",
                "properties": [
                    {
                        "name": "phone",
                        "description": "Customer mobile phone number, E.164 format",
                        "businessName": "Mobile Phone Number",
                        "tags": ["regulated", "billing"],
                    },
                ],
            }],
        }
        result3 = store.upsert_business(
            contract = business_partial,
            table    = "telecom.customers",
            run_id   = "2026-05-09_16-00-00",
        )
        assert result3.version_after == "1.0.2"
        print(f"✓ Business upsert version → {result3.version_after}")

        # 6. Verify all three workflows merged correctly
        active3 = store.get_active("telecom.customers")
        phone3  = active3["schema"][0]["properties"][0]
        assert phone3["description"]  == "Customer mobile phone number, E.164 format"
        assert phone3["businessName"] == "Mobile Phone Number"
        # Tags should contain ALL: pii, gdpr, regulated, billing
        assert set(phone3["tags"]) >= {"pii", "gdpr_personal_data", "regulated", "billing"}
        # PII still present
        assert phone3["pii"]["detected"] is True
        # Quality still present
        assert phone3["quality"]
        # Classification preserved
        assert phone3["classification"] == "pii_personal"
        print("✓ Three workflows merged: tags unioned, all blocks present")

        # 7. History
        history = store.get_history("telecom.customers")
        assert len(history) == 3
        assert history[0].workflow == "business"  # most recent first
        assert history[1].workflow == "pii"
        assert history[2].workflow == "ge"
        print("✓ History returns 3 entries, most-recent-first")

        # 8. Identity conflict
        evil = {
            "contract_uuid": "00000000-0000-0000-0000-000000000000",
            "schema": [{"name": "telecom_customers", "properties": []}],
        }
        try:
            store.upsert(partial=evil, table="telecom.customers", workflow="manual")
            raise AssertionError("Expected IdentityConflictError")
        except IdentityConflictError as e:
            assert "contract_uuid" in str(e)
        print("✓ Identity conflict rejected by ContractStore")

        # 9. List tables
        store.upsert(
            partial  = {"schema": [{"name": "eshop_orders", "properties": []}]},
            table    = "eshop.orders",
            workflow = "ge",
        )
        tables = store.list_tables()
        assert "telecom.customers" in tables
        assert "eshop.orders"      in tables
        print(f"✓ list_tables: {tables}")

        # 10. Audit snapshot retrieval
        snapshot = store.get_audit_snapshot("telecom.customers", result2.run_uuid)
        assert snapshot is not None
        assert snapshot["version"] == "1.0.1"  # PII upsert version
        print("✓ get_audit_snapshot returns historical version")

    print("\n✅ All ContractStore smoke tests passed")


# Backward-compat aliases (deprecated — will be removed in v2.0)
ContractStoreService = ContractStore