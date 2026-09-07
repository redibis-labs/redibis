"""
redibis.services.evidence_review_service
========================================
Unified evidence review read model — combines ``evidence_bundle.json`` columns,
contract overlays, review checkpoints, and optional run-scoped supplied verdicts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from redibis.review.drift import LIFECYCLE_ACTIVE, LIFECYCLE_STALE, evaluate_column_drift
from redibis.review.fingerprint import (
    evidence_digest,
    fingerprint_from_contract_prop,
    fingerprint_from_evidence_column,
)
from redibis.review.verdict_package import VerdictEntry, VerdictPackage, export_verdict_package
from redibis.review.verdict_resolver import resolve_effective_verdict
from redibis.services.review_service import ReviewService, _iter_props, _pending_review
from redibis.store.contract_store import ContractStore
from redibis.store.pii_decisions import PiiDecision, PiiDecisionStore
from redibis.store.review_store import ReviewStore

#: Version of *this* normalized review DTO — bump when the shape of
#: ``EvidenceReviewService.from_bundle`` output changes in an incompatible way.
#: Independent of ``evidence_bundle.json``'s own ``schema_version`` (2.0).
REVIEW_SCHEMA_VERSION = "1.0"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _review_progress(columns: list[dict]) -> dict[str, Any]:
    total = len(columns)
    if not total:
        return {
            "total_columns": 0,
            "reviewed_count": 0,
            "stale_count": 0,
            "unreviewed_count": 0,
            "status": "empty",
        }
    reviewed = sum(1 for c in columns if c.get("review_status") in ("approved", "edited"))
    stale = sum(1 for c in columns if c.get("drift", {}).get("state") == LIFECYCLE_STALE)
    unreviewed = total - reviewed
    if reviewed >= total and stale == 0:
        status = "reviewed"
    elif stale > 0:
        status = "stale"
    elif reviewed > 0:
        status = "partially_reviewed"
    else:
        status = "unreviewed"
    return {
        "total_columns": total,
        "reviewed_count": reviewed,
        "stale_count": stale,
        "unreviewed_count": unreviewed,
        "status": status,
    }


def _header_summary(bundle: dict) -> dict[str, Any]:
    """Trim ``evidence_bundle.header`` to what the run explorer needs to render.

    Keeps engine registry, rule/equation catalogue (so ``rules_fired`` IDs and
    the per-column engine matrix resolve to something), sampling, and
    timestamps — drops nothing sensitive (the header never carries raw values).
    """
    header = bundle.get("header") if isinstance(bundle.get("header"), dict) else {}
    provenance = header.get("provenance") if isinstance(header.get("provenance"), dict) else {}
    pack_stack = provenance.get("pack_stack") if isinstance(provenance.get("pack_stack"), dict) else None
    return {
        "provenance": {
            "redibis_version": provenance.get("redibis_version"),
            "redibis_git_sha": provenance.get("redibis_git_sha"),
            "redibis_build": provenance.get("redibis_build"),
            "config_sha256": provenance.get("config_sha256"),
            "pack_stack": pack_stack,
        },
        "timestamps": header.get("timestamps") or {},
        "engines": header.get("engines") or [],
        "rules": header.get("rules") or {},
        "sampling": header.get("sampling") or {},
    }


def _engine_matrix_for_column(col_block: dict) -> dict[str, dict]:
    """``engine_id -> EngineEvidenceRecord.to_dict()`` for the drill-down grid.

    Sourced from the plugin-neutral ``pii_evidence.engine_evidence`` map (see
    ``redibis.evidence.engines.merge_compat_blocks``), which is the canonical,
    uniformly-shaped record (``ran``/``reason``/``score``/``entity``/``label``/
    ``match_rate``/``hits``/``rates``/``duration_ms``/``error``/``version``/
    ``config_hash``/``extra``) for *every* registered engine. ``pii_evidence``
    also carries legacy per-engine "compat" blocks (``presidio``, ``phone``,
    ...) with ad-hoc field names for backward-compatible replay — those are
    not plugin-neutral and must not be surfaced as engine-matrix rows.
    """
    pii_evidence = col_block.get("pii_evidence") if isinstance(col_block.get("pii_evidence"), dict) else {}
    ev = pii_evidence.get("engine_evidence") if isinstance(pii_evidence.get("engine_evidence"), dict) else {}
    return {k: v for k, v in ev.items() if isinstance(v, dict)}


def _redacted_samples(samples: Any) -> Optional[dict]:
    """Sample shape/count for the review DTO — never literal source values.

    ``evidence_bundle.json`` (the "full"/restricted copy) may embed real
    sampled cell values when ``report.evidence_bundle.sample_mode: raw``
    (the default). The review API is a general-audience surface, not the
    governed restricted-evidence spool, so literal values never leave this
    read model — only the sampling shape (mode/n/seed/count) is exposed.
    """
    if not isinstance(samples, dict):
        return None
    values = samples.get("values")
    return {
        "mode": samples.get("mode"),
        "n": samples.get("n"),
        "seed": samples.get("seed"),
        "count": len(values) if isinstance(values, list) else 0,
    }


def _column_coverage(col_block: dict) -> dict:
    cov = col_block.get("coverage")
    return dict(cov) if isinstance(cov, dict) else {}


class EvidenceReviewService:
    """Framework-neutral evidence reviewer for UI, batch reports, and CLI."""

    def __init__(
        self,
        store: Optional[ContractStore] = None,
        *,
        review_store: Optional[ReviewStore] = None,
        pii_store: Optional[PiiDecisionStore] = None,
    ):
        self.store = store
        self.reviews = review_store
        self.pii_store = pii_store
        self._review_svc = ReviewService(store, review_store) if store else None

    def from_bundle(
        self,
        bundle: dict,
        *,
        table: Optional[str] = None,
        mode: str = "artifact",
        supplied_verdicts: Optional[VerdictPackage] = None,
        session_id: str = "",
        manifest: Optional[dict] = None,
    ) -> dict:
        """Build normalized review payload from an evidence bundle."""
        table_name = table or str((bundle.get("table") or {}).get("name") or "")
        run_id = str((bundle.get("table") or {}).get("run_id") or "")
        columns_block = bundle.get("columns") if isinstance(bundle.get("columns"), dict) else {}

        pii_decisions: dict[str, dict] = {}
        review_state = None
        contract_props: dict[str, dict] = {}
        if self.store and table_name:
            try:
                pii_decisions = self.store.get_pii_decisions(table_name)
            except Exception:
                pii_decisions = {}
            if self.reviews:
                review_state = self.reviews.get(table_name)
            active = self.store.get_active(table_name)
            if active:
                contract_props = {name: prop for name, prop in _iter_props(active)}

        supplied_by_col = supplied_verdicts.by_column(table_name) if supplied_verdicts else {}
        replay_digest = ""
        if supplied_verdicts:
            from redibis.review.verdict_package import verdict_package_digest

            replay_digest = verdict_package_digest(supplied_verdicts)

        items: list[dict] = []
        for column in sorted(columns_block.keys()):
            col_block = columns_block[column] or {}
            fp = fingerprint_from_evidence_column(column, col_block)
            steward_raw = pii_decisions.get(column)
            supplied_entry = supplied_by_col.get(column)
            supplied_dict = supplied_entry.to_dict() if supplied_entry else None

            effective = resolve_effective_verdict(
                column=column,
                col_block=col_block,
                current_fingerprint=fp,
                steward_decision=steward_raw,
                supplied_decision=supplied_dict,
            )

            review_rec = _pending_review()
            review_status = "pending"
            if review_state and column in review_state.columns:
                cr = review_state.columns[column]
                review_rec = cr.to_dict()
                review_status = cr.status

            drift = effective.drift
            if drift is None and steward_raw:
                drift = evaluate_column_drift(
                    fingerprint_from_contract_prop(column, contract_props.get(column, {}))
                    if column in contract_props
                    else None,
                    fp,
                    lifecycle_state=str(steward_raw.get("lifecycle_state") or LIFECYCLE_ACTIVE),
                )

            items.append({
                "column": column,
                "position": col_block.get("position"),
                "logical_type": col_block.get("logical_type"),
                "physical_type": col_block.get("physical_type"),
                "inferred_class": col_block.get("inferred_class"),
                "fingerprint": fp.to_dict(),
                "evidence_digest": evidence_digest(col_block),
                "engine_proposal": effective.engine_proposal,
                "effective_verdict": {
                    "detected": effective.detected,
                    "entity_type": effective.entity_type,
                    "confidence": effective.confidence,
                    "source": effective.source,
                    "authority_reason": effective.authority_reason,
                },
                "steward_decision": effective.steward_decision,
                "supplied_decision": effective.supplied_decision,
                "drift": {
                    "state": drift.state if drift else "none",
                    "reasons": list(drift.reasons) if drift else [],
                    "fingerprint_match": drift.fingerprint_match if drift else False,
                },
                "review": review_rec,
                "review_status": review_status,
                "coverage": _column_coverage(col_block),
                "pii_evidence": col_block.get("pii_evidence"),
                "engine_evidence": _engine_matrix_for_column(col_block),
                "rules_fired": col_block.get("rules_fired") or [],
                "negative_signals_fired": col_block.get("negative_signals_fired") or [],
                "validator_results": col_block.get("validator_results") or {},
                "quality": col_block.get("quality"),
                "profile": col_block.get("profile"),
                "profile_summary": _profile_summary(col_block.get("profile")),
                "samples": _redacted_samples(col_block.get("samples")),
            })

        progress = _review_progress(items)
        out: dict[str, Any] = {
            "review_schema_version": REVIEW_SCHEMA_VERSION,
            "table": table_name,
            "run_id": run_id,
            "mode": mode,
            "session_id": session_id,
            "columns": items,
            "progress": progress,
            "schema_version": bundle.get("schema_version"),
            "header": _header_summary(bundle),
            "table_summary": bundle.get("table_summary") or {},
        }
        if manifest:
            out["artifacts"] = manifest.get("artifacts") or {}
            out["llm_calls"] = manifest.get("llm_calls") or []
            out["coverage"] = manifest.get("coverage") or {}
            out["errors"] = manifest.get("errors") or []
            out["warnings"] = manifest.get("warnings") or []
            out["run_status"] = manifest.get("run_status") or ""
        if replay_digest:
            out["supplied_verdict_digest"] = replay_digest
        if self._review_svc and table_name:
            try:
                contract_review = self._review_svc.status(table_name)
                out["contract_review"] = contract_review
            except Exception:
                pass
        return out

    def export_verdicts(
        self,
        *,
        tables: Optional[list[str]] = None,
        exporter: str = "redibis",
    ) -> dict:
        """Export portable steward verdicts from PII decision overlays."""
        if self.store is None or self.pii_store is None:
            raise ValueError("ContractStore required for verdict export")
        target_tables = tables
        if not target_tables:
            target_tables = self._list_tables_with_decisions()
        entries: list[VerdictEntry] = []
        for table in target_tables:
            decisions = self.pii_store.get(table)
            for column, raw in decisions.items():
                lifecycle = str(raw.get("lifecycle_state") or LIFECYCLE_ACTIVE)
                if lifecycle == "superseded":
                    continue
                entries.append(VerdictEntry(
                    table=table,
                    column=column,
                    status=str(raw.get("status") or "not_pii"),
                    entity_type=raw.get("entity_type"),
                    fingerprint_key=str(raw.get("fingerprint_key") or ""),
                    logical_type=str(raw.get("logical_type") or ""),
                    physical_type=str(raw.get("physical_type") or ""),
                    format_signature=str(raw.get("format_signature") or ""),
                    name_normalized=str(raw.get("name_normalized") or ""),
                    evidence_digest=str(raw.get("evidence_digest") or ""),
                    lifecycle_state=lifecycle,
                    decision_version=int(raw.get("decision_version") or 1),
                    decided_by=str(raw.get("decided_by") or ""),
                    reason=str(raw.get("reason") or ""),
                    ts=str(raw.get("ts") or ""),
                    source_run_id=str(raw.get("run_id") or ""),
                ))
        package = export_verdict_package(entries, exporter=exporter, exported_at=_utc_now_iso())
        return package.to_dict()

    def preview_verdicts(self, table: str, bundle: dict, package: VerdictPackage) -> dict:
        """Classify every package entry for *table* before any write.

        Categories (plan §6): ``matching`` (fingerprint-clean, safe to
        promote), ``stale`` (fingerprint mismatch vs. the current column),
        ``missing`` (column absent from this run's evidence bundle),
        ``conflicting`` (an active local steward decision already disagrees),
        ``invalid`` (malformed entry — no column, or wrong table). Never
        writes to any store.
        """
        columns_block = bundle.get("columns") if isinstance(bundle.get("columns"), dict) else {}
        local_decisions: dict[str, dict] = {}
        if self.store:
            try:
                local_decisions = self.store.get_pii_decisions(table)
            except Exception:
                local_decisions = {}

        buckets: dict[str, list[dict]] = {
            "matching": [], "stale": [], "missing": [], "conflicting": [], "invalid": [],
        }
        for entry in package.entries_for_table(table):
            row = {
                "column": entry.column,
                "status": entry.status,
                "entity_type": entry.entity_type,
                "reason": entry.reason,
                "decided_by": entry.decided_by,
                "source_run_id": entry.source_run_id,
            }
            if not entry.column or entry.table != table:
                buckets["invalid"].append({**row, "detail": "missing column or table mismatch"})
                continue
            if not entry.fingerprint_key:
                buckets["invalid"].append({**row, "detail": "no fingerprint — cannot verify safely"})
                continue
            col_block = columns_block.get(entry.column)
            if col_block is None:
                buckets["missing"].append({**row, "detail": "column not present in this run's evidence"})
                continue
            current_fp = fingerprint_from_evidence_column(entry.column, col_block)
            if entry.fingerprint_key != current_fp.fingerprint_key:
                buckets["stale"].append({
                    **row, "detail": "fingerprint mismatch vs. current column",
                    "expected": entry.fingerprint_key, "actual": current_fp.fingerprint_key,
                })
                continue
            local = local_decisions.get(entry.column)
            if (
                local
                and str(local.get("lifecycle_state") or LIFECYCLE_ACTIVE) == LIFECYCLE_ACTIVE
                and str(local.get("status") or "") != entry.status
            ):
                buckets["conflicting"].append({
                    **row, "detail": "disagrees with an active local steward decision",
                    "local_status": local.get("status"), "local_decided_by": local.get("decided_by"),
                })
                continue
            buckets["matching"].append(row)
        buckets["summary"] = {k: len(v) for k, v in buckets.items() if k != "summary"}
        buckets["table"] = table
        buckets["run_id"] = str((bundle.get("table") or {}).get("run_id") or "")
        return buckets

    def import_verdicts(
        self,
        table: str,
        bundle: dict,
        package: VerdictPackage,
        *,
        actor: str,
        reason: str,
        merge_policy: str = "matching_only",
    ) -> dict:
        """Durably promote package entries into the PII decision overlay.

        ``merge_policy``: ``matching_only`` (default — only fingerprint-clean,
        non-conflicting entries) or ``overwrite_conflicts`` (also promote
        entries that disagree with an existing active local decision, the
        steward's explicit override). ``stale``/``missing``/``invalid``
        entries are never promoted regardless of policy. Records one audit
        event in ``ContractStore.verdict_import_log`` and writes through
        ``PiiDecisionStore``/``ContractStore.set_pii_decision`` — no new
        contract-bucket writer.
        """
        if self.store is None:
            raise ValueError("ContractStore required for verdict import")
        actor = (actor or "").strip()
        reason = (reason or "").strip()
        if not actor or not reason:
            raise ValueError("actor and reason are required to import verdicts")
        if merge_policy not in ("matching_only", "overwrite_conflicts"):
            raise ValueError(f"unknown merge_policy: {merge_policy!r}")

        preview = self.preview_verdicts(table, bundle, package)
        to_apply = list(preview["matching"])
        if merge_policy == "overwrite_conflicts":
            to_apply += list(preview["conflicting"])

        by_column = package.by_column(table)
        applied: list[str] = []
        skipped: list[dict] = []
        for row in to_apply:
            entry = by_column.get(row["column"])
            if entry is None:
                continue
            try:
                self.store.set_pii_decision(
                    table, entry.column, entry.status,
                    entity_type=entry.entity_type,
                    payload=None,
                    decided_by=f"import:{actor}",
                    run_id=entry.source_run_id,
                    fingerprint_key=entry.fingerprint_key,
                    name_normalized=entry.name_normalized,
                    logical_type=entry.logical_type,
                    physical_type=entry.physical_type,
                    format_signature=entry.format_signature,
                    evidence_digest=entry.evidence_digest,
                    reason=reason or entry.reason,
                    lifecycle_state="active",
                    decision_version=entry.decision_version,
                )
                applied.append(entry.column)
            except Exception as exc:
                skipped.append({"column": entry.column, "error": str(exc)})

        result = {
            "table": table,
            "actor": actor,
            "reason": reason,
            "merge_policy": merge_policy,
            "applied": applied,
            "skipped": skipped,
            "stale_skipped": [r["column"] for r in preview["stale"]],
            "missing_skipped": [r["column"] for r in preview["missing"]],
            "invalid_skipped": [r["column"] for r in preview["invalid"]],
            "conflicting_skipped": (
                [] if merge_policy == "overwrite_conflicts"
                else [r["column"] for r in preview["conflicting"]]
            ),
            "package_digest": _package_digest(package),
        }
        if hasattr(self.store, "verdict_import_log"):
            self.store.verdict_import_log.append(table, result)
        return result

    def mark_stale_on_drift(self, table: str, bundle: dict) -> list[str]:
        """Persist stale lifecycle for drifted steward decisions; returns column names."""
        if self.store is None or self.pii_store is None:
            return []
        decisions = self.pii_store.get(table)
        if not decisions:
            return []
        columns_block = bundle.get("columns") if isinstance(bundle.get("columns"), dict) else {}
        stale_cols: list[str] = []
        for column, raw in decisions.items():
            if str(raw.get("lifecycle_state") or LIFECYCLE_ACTIVE) != LIFECYCLE_ACTIVE:
                continue
            col_block = columns_block.get(column)
            if col_block is None:
                drift = evaluate_column_drift(
                    _decision_fingerprint(raw),
                    None,
                )
            else:
                current = fingerprint_from_evidence_column(column, col_block)
                drift = evaluate_column_drift(_decision_fingerprint(raw), current)
            if drift.state == LIFECYCLE_STALE:
                updated = PiiDecision.from_dict({**raw, "lifecycle_state": LIFECYCLE_STALE})
                self.pii_store.set(table, updated)
                stale_cols.append(column)
        return stale_cols

    def _list_tables_with_decisions(self) -> list[str]:
        if self.pii_store is None:
            return []
        prefix = f"{PiiDecisionStore.PREFIX}/"
        keys = self.pii_store.backend.list_keys(self.pii_store.bucket, prefix=prefix)
        tables = []
        for key in keys:
            if key.endswith(".json"):
                tables.append(key[len(prefix): -len(".json")])
        return sorted(tables)


def _decision_fingerprint(raw: dict):
    from redibis.review.fingerprint import ColumnFingerprintSnapshot

    if not raw.get("fingerprint_key") and not raw.get("logical_type"):
        return None
    return ColumnFingerprintSnapshot.from_dict({
        "column": raw.get("column", ""),
        "name_normalized": raw.get("name_normalized", ""),
        "logical_type": raw.get("logical_type", "string"),
        "physical_type": raw.get("physical_type", "string"),
        "format_signature": raw.get("format_signature", "unknown"),
        "fingerprint_key": raw.get("fingerprint_key", ""),
    })


def _package_digest(package: VerdictPackage) -> str:
    from redibis.review.verdict_package import verdict_package_digest

    return verdict_package_digest(package)


def _profile_summary(profile: Optional[dict]) -> dict:
    if not isinstance(profile, dict):
        return {}
    counts = profile.get("counts") if isinstance(profile.get("counts"), dict) else {}
    return {
        "inferred_class": profile.get("inferred_class"),
        "null_rate": counts.get("null_rate"),
        "distinct_rate": counts.get("distinct_rate"),
    }
