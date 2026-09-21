"""Deep Enrich service — governed synthesis candidates with selective merge.

Deep Enrich reuses Contract Synthesis analyzers/stages but:
  * resolves the Enrich page provider via ``contract.enrichment``
  * persists a separate ``deep_enrich`` subcontract (never auto-upserts active)
  * exposes path-level review + explicit merge into the active contract
  * appends ``llm_synthesis`` generations so Steward Review can pick verdicts
"""

from __future__ import annotations

import copy
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.contract_store import ContractStore
from redibis.store.generation_ledger import (
    Generation,
    generations_from_contract_column,
)
from redibis.store.subcontract_store import (
    KIND_DEEP_ENRICH,
    STATUS_DISCARDED,
    STATUS_DRAFT,
    STATUS_MERGED,
    STATUS_REVIEWED,
    Subcontract,
    SubcontractStore,
)
from redibis.synthesis.merge import (
    PathVerdict,
    REVIEW_DECISIONS,
    build_partial_from_verdicts,
    merge_accepted,
)
from redibis.synthesis.path_diff import diff_contracts, summarize_diff
from redibis.synthesis.runner import ContractSynthesisRunner, SynthesisResult

log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iter_props(contract: dict):
    for schema_obj in contract.get("schema") or []:
        if not isinstance(schema_obj, dict):
            continue
        for prop in schema_obj.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name"):
                yield str(prop["name"]), prop


class DeepEnrichService:
    """Run / list / review / merge Deep Enrich candidates for one ContractStore."""

    def __init__(
        self,
        store: ContractStore,
        subcontracts: Optional[SubcontractStore] = None,
        *,
        redibis_config: Any = None,
    ):
        self.store = store
        if subcontracts is not None:
            self.subs = subcontracts
        else:
            backend = store.backend
            self.subs = SubcontractStore(backend)
        self.redibis_config = redibis_config
        self.ledger = getattr(store, "generation_ledger", None)

    # ── Run ───────────────────────────────────────────────────────────────

    def run(
        self,
        table: str,
        *,
        provider: Any = None,
        analysis_mode: str = "deterministic",
        odcs_version: str = "v3.1.0",
        uploaded: Optional[dict[str, bytes]] = None,
        requirement_paths: Optional[list] = None,
        source_paths: Optional[list] = None,
        system_prompt: str = "",
        extra_context: str = "",
        output_dir: Optional[str] = None,
        created_by: str = "deep-enrich",
        also_run_assisted_compare: bool = False,
    ) -> dict[str, Any]:
        """Run Deep Enrich and persist a governed candidate subcontract.

        Never writes the active contract.
        """
        base = self.store.get_active(table)
        if not base:
            raise ValueError(f"no active contract for {table}")

        runner = ContractSynthesisRunner(
            analysis_mode=analysis_mode or "deterministic",
            odcs_version=odcs_version or "v3.1.0",
            provider=provider,
            redibis_config=self.redibis_config,
        )
        # Inject prompt/context into assisted path via runner context by
        # temporarily wrapping the provider call if prompts were supplied.
        if system_prompt or extra_context:
            provider = self._wrap_provider_prompts(provider, system_prompt, extra_context)
            runner.provider = provider

        result: SynthesisResult = runner.run(
            base_contract=base,
            requirement_paths=requirement_paths,
            source_paths=source_paths,
            uploaded=uploaded,
            output_dir=output_dir,
            also_run_assisted_compare=also_run_assisted_compare,
        )

        path_diff = diff_contracts(base, result.candidate)
        run_id = uuid.uuid4().hex[:12]
        payload = {
            "candidate": result.candidate,
            "base_ref": {
                "table": table,
                "contract_uuid": base.get("contract_uuid"),
                "version": base.get("version"),
                "apiVersion": base.get("apiVersion"),
                "status": base.get("status"),
            },
            "path_diff": path_diff,
            "diff_summary": summarize_diff(path_diff),
            "evidence": result.evidence,
            "lineage": result.lineage,
            "lineage_react_flow": result.lineage_react_flow,
            "traceability": result.traceability,
            "stage_results": result.stage_results,
            "meta": {
                **(result.meta or {}),
                "tool": "redibis.deep_enrich",
                "writes_active_contract": False,
                "system_prompt_chars": len(system_prompt or ""),
                "extra_context_chars": len(extra_context or ""),
            },
            "comparison": result.comparison,
            "artifacts": result.artifacts,
            "path_verdicts": {},
            "valid": result.valid,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
        }
        sub = self.subs.create_from_payload(
            kind=KIND_DEEP_ENRICH,
            schema_table=table,
            run_id=run_id,
            payload=payload,
            contract_uuid=str(base.get("contract_uuid") or "") or None,
            created_by=created_by,
            summary_stats={
                "valid": result.valid,
                "diff_total": len(path_diff),
                "analysis_mode": analysis_mode,
                "property_count": sum(
                    len(s.get("properties") or [])
                    for s in ((result.candidate or {}).get("schema") or [])
                    if isinstance(s, dict)
                ),
            },
        )
        self._append_generations(table, result.candidate, run_id)
        return self._public_view(sub)

    def _wrap_provider_prompts(self, provider: Any, system_prompt: str, extra_context: str):
        """Prepend steward system/context text to assisted synthesis prompts."""
        if provider is None:
            return None
        sys_extra = (system_prompt or "").strip()
        ctx_extra = (extra_context or "").strip()
        if not sys_extra and not ctx_extra:
            return provider

        original_complete = getattr(provider, "complete", None)
        if not callable(original_complete):
            return provider

        def complete(system: str = "", user: str = "", **kwargs):
            sys_parts = [p for p in (sys_extra, system) if p]
            user_parts = [p for p in (ctx_extra and f"Additional context:\n{ctx_extra}", user) if p]
            return original_complete(
                system="\n\n".join(sys_parts),
                user="\n\n".join(user_parts),
                **kwargs,
            )

        try:
            provider.complete = complete  # type: ignore[attr-defined]
        except Exception:
            # Immutable provider — ignore prompt injection
            pass
        return provider

    def _append_generations(self, table: str, candidate: dict, run_id: str) -> None:
        if self.ledger is None or not candidate:
            return
        for col, prop in _iter_props(candidate):
            gens = generations_from_contract_column(
                col, prop, source="llm_synthesis", run_id=run_id,
            )
            # Emit SLA/custom facets as table-scoped generations under a sentinel column
            if gens:
                try:
                    self.ledger.append(table, col, gens)
                except Exception:
                    log.debug("ledger append failed for %s.%s", table, col, exc_info=True)

        # Table-level facets (freshness / cost) stored under "__table__"
        facet_gens: list[Generation] = []
        for entry in candidate.get("slaProperties") or []:
            if not isinstance(entry, dict) or not entry.get("property"):
                continue
            prop_name = str(entry["property"])
            field = prop_name
            lower = prop_name.lower()
            if lower in ("frequency", "timeofavailability", "freshness", "latency"):
                field = "freshness"
            elif lower == "retention":
                field = "retention"
            facet_gens.append(Generation(
                field=field,
                source="llm_synthesis",
                value=entry,
                confidence=None,
                run_id=run_id,
                ts=_utc_now_iso(),
                detail={"sla_property": prop_name, "scope": "table"},
            ))
        for entry in candidate.get("customProperties") or []:
            if not isinstance(entry, dict) or not entry.get("property"):
                continue
            prop_name = str(entry["property"])
            if prop_name == "pii_summary":
                continue
            field = "cost" if prop_name.lower() in (
                "cost", "business_cost", "businesscost", "data_product_cost",
            ) else prop_name
            facet_gens.append(Generation(
                field=field,
                source="llm_synthesis",
                value=entry,
                confidence=None,
                run_id=run_id,
                ts=_utc_now_iso(),
                detail={"custom_property": prop_name, "scope": "table"},
            ))
        if facet_gens and self.ledger is not None:
            try:
                self.ledger.append(table, "__table__", facet_gens)
            except Exception:
                log.debug("ledger append failed for %s.__table__", table, exc_info=True)

    # ── Read / list ───────────────────────────────────────────────────────

    def list_runs(self, table: str) -> list[dict]:
        return [
            {
                **s.summary(),
                "diff_total": (s.summary_stats or {}).get("diff_total"),
                "valid": (s.summary_stats or {}).get("valid"),
                "analysis_mode": (s.summary_stats or {}).get("analysis_mode"),
            }
            for s in self.subs.list_runs(KIND_DEEP_ENRICH, table)
            if s.status != STATUS_DISCARDED
        ]

    def get(self, table: str, run_id: str) -> dict[str, Any]:
        sub = self.subs.get(KIND_DEEP_ENRICH, table, run_id)
        if sub is None:
            raise ValueError(f"no Deep Enrich run {run_id!r} for {table}")
        return self._public_view(sub)

    def latest(self, table: str) -> Optional[dict[str, Any]]:
        runs = self.subs.list_runs(KIND_DEEP_ENRICH, table)
        for sub in runs:
            if sub.status != STATUS_DISCARDED:
                return self._public_view(sub)
        return None

    def discard(self, table: str, run_id: str) -> dict[str, Any]:
        sub = self.subs.discard(KIND_DEEP_ENRICH, table, run_id)
        if sub is None:
            raise ValueError(f"no Deep Enrich run {run_id!r} for {table}")
        return self._public_view(sub)

    # ── Path verdicts ─────────────────────────────────────────────────────

    def set_path_verdicts(
        self,
        table: str,
        run_id: str,
        verdicts: list[dict],
        *,
        actor: str = "",
    ) -> dict[str, Any]:
        sub = self.subs.get(KIND_DEEP_ENRICH, table, run_id)
        if sub is None:
            raise ValueError(f"no Deep Enrich run {run_id!r} for {table}")
        payload = dict(sub.payload or {})
        existing = dict(payload.get("path_verdicts") or {})
        for raw in verdicts:
            fv = PathVerdict.from_dict(raw or {})
            if not fv.path:
                raise ValueError("path verdict missing path")
            if fv.decision not in REVIEW_DECISIONS:
                raise ValueError(f"invalid decision {fv.decision!r}")
            entry = fv.to_dict()
            entry["at"] = _utc_now_iso()
            entry["by"] = actor
            existing[fv.path] = entry
        payload["path_verdicts"] = existing
        sub.payload = payload
        if sub.status == STATUS_DRAFT:
            sub.status = STATUS_REVIEWED
            sub.reviewed_at = _utc_now_iso()
        self.subs.write(sub)
        return self._public_view(sub)

    # ── Preview / merge ───────────────────────────────────────────────────

    def _assert_base_fresh(self, table: str, payload: dict) -> dict:
        active = self.store.get_active(table)
        if not active:
            raise ValueError(f"no active contract for {table}")
        base_ref = payload.get("base_ref") or {}
        if base_ref.get("contract_uuid") and active.get("contract_uuid"):
            if str(base_ref["contract_uuid"]) != str(active["contract_uuid"]):
                raise ValueError(
                    "Deep Enrich candidate targets a different contract_uuid — "
                    "re-run Deep Enrich against the current active contract"
                )
        if base_ref.get("version") and active.get("version"):
            if str(base_ref["version"]) != str(active["version"]):
                raise ValueError(
                    f"Deep Enrich candidate is stale "
                    f"(base version {base_ref['version']} ≠ active {active['version']}). "
                    "Re-run Deep Enrich or discard this candidate."
                )
        return active

    def preview_merge(self, table: str, run_id: str, verdicts: Optional[list[dict]] = None) -> dict:
        sub = self.subs.get(KIND_DEEP_ENRICH, table, run_id)
        if sub is None:
            raise ValueError(f"no Deep Enrich run {run_id!r} for {table}")
        payload = sub.payload or {}
        active = self._assert_base_fresh(table, payload)
        candidate = payload.get("candidate") or {}
        if verdicts is None:
            verdicts = list((payload.get("path_verdicts") or {}).values())
        preview = build_partial_from_verdicts(
            active=active, candidate=candidate, verdicts=verdicts,
        )
        return {
            "run_id": run_id,
            "table": table,
            "base_ref": payload.get("base_ref"),
            **preview.to_dict(),
        }

    def merge(
        self,
        table: str,
        run_id: str,
        *,
        actor: str,
        verdicts: Optional[list[dict]] = None,
        validate: bool = True,
    ) -> dict[str, Any]:
        who = (actor or "").strip()
        if not who:
            raise ValueError("actor is required for Deep Enrich merge")
        sub = self.subs.get(KIND_DEEP_ENRICH, table, run_id)
        if sub is None:
            raise ValueError(f"no Deep Enrich run {run_id!r} for {table}")
        if sub.status == STATUS_DISCARDED:
            raise ValueError("cannot merge a discarded Deep Enrich candidate")
        payload = sub.payload or {}
        active = self._assert_base_fresh(table, payload)
        candidate = payload.get("candidate") or {}
        if verdicts is None:
            verdicts = list((payload.get("path_verdicts") or {}).values())
        if not verdicts:
            raise ValueError("no path verdicts to merge — accept/edit paths first")

        result = merge_accepted(
            self.store,
            table,
            active=active,
            candidate=candidate,
            verdicts=verdicts,
            decided_by=who,
            run_id=run_id,
            validate=validate,
        )
        if result.get("ok"):
            self.subs.set_status(
                KIND_DEEP_ENRICH, table, run_id, STATUS_MERGED,
                contract_uuid=str(active.get("contract_uuid") or "") or None,
            )
            # Refresh path_verdicts with merge stamp
            sub2 = self.subs.get(KIND_DEEP_ENRICH, table, run_id)
            if sub2 is not None:
                pl = dict(sub2.payload or {})
                pl["merged_at"] = _utc_now_iso()
                pl["merged_by"] = who
                pl["merge_result"] = {
                    "accepted_paths": result.get("accepted_paths"),
                    "version_after": result.get("version_after"),
                    "run_uuid": result.get("run_uuid"),
                }
                sub2.payload = pl
                self.subs.write(sub2)
        result["run_id"] = run_id
        result["table"] = table
        return result

    def facets_for_steward(self, table: str) -> dict[str, Any]:
        """Table-level Deep Enrich facets (freshness/cost/…) for Steward Review."""
        latest = self.latest(table)
        if not latest:
            return {"run_id": None, "facets": [], "merge_status": None, "stale": False}
        payload = latest.get("payload") or {}
        path_diff = payload.get("path_diff") or latest.get("path_diff") or []
        facets = []
        for item in path_diff:
            if item.get("scope") not in ("sla", "custom", "table"):
                continue
            if item.get("scope") == "table" and item.get("field") in ("name", "description", "owner"):
                # Already covered by TABLE_ITEMS
                continue
            facets.append(item)
        # Also surface ledger table facets
        ledger_facets = []
        if self.ledger is not None:
            try:
                for field in ("freshness", "retention", "cost"):
                    latest_by = self.ledger.latest_by_source(table, "__table__", field)
                    for source, gen in latest_by.items():
                        ledger_facets.append({
                            "field": field,
                            "source": source,
                            "value": gen.value,
                            "run_id": gen.run_id,
                            "ts": gen.ts,
                            "label": "Deep Enrich" if source == "llm_synthesis" else source,
                        })
            except Exception:
                pass
        base_ref = payload.get("base_ref") or latest.get("base_ref") or {}
        stale = False
        try:
            active = self.store.get_active(table) or {}
            if base_ref.get("version") and active.get("version"):
                stale = str(base_ref["version"]) != str(active["version"])
        except Exception:
            pass
        return {
            "run_id": latest.get("run_id"),
            "status": latest.get("status"),
            "stale": stale,
            "base_ref": base_ref,
            "facets": facets,
            "ledger_facets": ledger_facets,
            "path_verdicts": payload.get("path_verdicts") or latest.get("path_verdicts") or {},
            "diff_summary": payload.get("diff_summary") or latest.get("diff_summary"),
            "merge_status": latest.get("status"),
        }

    def unresolved_required_paths(self, table: str) -> list[str]:
        """Paths that still need a steward decision before finalize (blocking).

        Only paths currently marked ``needs_review`` in the candidate block
        finalization. Unreviewed paths are informational (steward may ignore).
        """
        latest = self.latest(table)
        if not latest:
            return []
        payload = latest.get("payload") or {}
        verdicts = payload.get("path_verdicts") or latest.get("path_verdicts") or {}
        blocking = []
        for path, v in verdicts.items():
            if isinstance(v, dict) and v.get("decision") == "needs_review":
                blocking.append(path)
        return blocking

    def _public_view(self, sub: Subcontract) -> dict[str, Any]:
        payload = sub.payload or {}
        candidate = payload.get("candidate") or {}
        return {
            "run_id": sub.run_id,
            "subcontract_id": sub.subcontract_id,
            "table": sub.schema_table,
            "kind": sub.kind,
            "status": sub.status,
            "created_at": sub.created_at,
            "reviewed_at": sub.reviewed_at,
            "merged_at": sub.merged_at,
            "created_by": sub.created_by,
            "contract_uuid": sub.contract_uuid,
            "summary_stats": sub.summary_stats,
            "valid": payload.get("valid"),
            "errors": payload.get("errors") or [],
            "warnings": payload.get("warnings") or [],
            "base_ref": payload.get("base_ref"),
            "path_diff": payload.get("path_diff") or [],
            "diff_summary": payload.get("diff_summary") or {},
            "path_verdicts": payload.get("path_verdicts") or {},
            "traceability": payload.get("traceability") or {},
            "lineage_react_flow": payload.get("lineage_react_flow") or {},
            "artifacts": payload.get("artifacts") or {},
            "meta": payload.get("meta") or {},
            "candidate_preview": {
                "apiVersion": candidate.get("apiVersion"),
                "name": candidate.get("name"),
                "version": candidate.get("version"),
                "status": candidate.get("status"),
                "property_count": sum(
                    len(s.get("properties") or [])
                    for s in (candidate.get("schema") or [])
                    if isinstance(s, dict)
                ),
            },
            # Full payload for detail views / merge (large)
            "payload": payload,
            "writes_active_contract": False,
        }


def deep_enrich_service_for_store(
    store: ContractStore,
    *,
    subcontracts: Optional[SubcontractStore] = None,
    redibis_config: Any = None,
) -> DeepEnrichService:
    return DeepEnrichService(store, subcontracts, redibis_config=redibis_config)
