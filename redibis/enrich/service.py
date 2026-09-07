"""
redibis.enrich.service — full-contract enrichment (always-active governance).

Flow (contract lifecycle):
    1. Read active contract (C_det).
    2. LLM returns enrichment delta; apply to produce C_llm.
    3. Validate C_llm (advisory — never blocks the write).
    4. Always auto-upsert C_llm to active (PII safety strip on write).
    5. Write per-run artifacts + agent_report + enrichment_status telemetry.
    6. Route classification changes through set_pii_decision overlay.

The service accepts an injected provider so it is testable without network.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from redibis.config import MemoryConfig, RAIConfig, RedibisConfig
from redibis.enrich.prompt_store import (
    builtin_enrichment_system_prompt,
    resolve_enrichment_system_prompt,
)
from redibis.enrich.providers import EnrichmentProvider, get_provider
from redibis.store.contract_store import ContractStore

if TYPE_CHECKING:
    from redibis.store.run_output_writer import RunOutputWriter

log = logging.getLogger(__name__)


# Caps for uploaded masked sample files included in the LLM context.
_MAX_SAMPLE_ROWS = 25
_MAX_SAMPLE_CHARS = 15000

# Builtin fallback / pack seed. Live Settings edits live in EnrichPromptStore.
DEFAULT_SYSTEM_PROMPT = builtin_enrichment_system_prompt()

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_context_rel(rel: str) -> str:
    """Reject keys that would escape the table's context prefix."""
    n = (rel or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in n.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"invalid context path: {rel!r}")
    return "/".join(parts)


@dataclass
class EnrichmentResult:
    """Outcome of an enrichment run."""
    table: str
    candidate: dict
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    enrichment_meta: dict = field(default_factory=dict)
    raw_response: str = ""
    diff_report: dict = field(default_factory=dict)
    auto_written: bool = False
    version_after: str = ""
    run_artifacts: dict = field(default_factory=dict)
    enrichment_status: str = "clean"
    deferred_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "enrichment_meta": self.enrichment_meta,
            "candidate": self.candidate,
            "diff_report": self.diff_report,
            "auto_written": self.auto_written,
            "version_after": self.version_after,
            "run_artifacts": self.run_artifacts,
            "enrichment_status": self.enrichment_status,
            "deferred_context": dict(self.deferred_context),
        }


@dataclass
class EnrichmentContext:
    """Shared enrichment prompt context — one path for UI, CLI, and agentic."""

    table: str
    run_id: str
    c_det: dict
    column_context: list[dict]
    contract_for_prompt: dict
    system_prompt: str
    user_prompt: str
    context_docs: list[tuple[str, str]] = field(default_factory=list)
    example_docs: list[tuple[str, str]] = field(default_factory=list)
    sample_data: list[tuple[str, str]] = field(default_factory=list)
    example_contracts: list[str] = field(default_factory=list)
    memory_context: Optional[str] = None
    pack_digest: dict = field(default_factory=dict)
    prompt_redacted: bool = False
    sample_policy: str = "raw"
    is_cloud: bool = False
    enrichment_pack: dict = field(default_factory=dict)
    pack_context_provenance: dict = field(default_factory=dict)


def enrichment_service_for_store(store) -> "EnrichmentService":
    """Build EnrichmentService with memory retriever wired (shared by CLI/agent/web)."""
    from redibis.memory.retriever import get_context_retriever

    retriever = get_context_retriever(
        store.memory_config,
        memory_store=getattr(store, "_memory_store", None),
    )
    return EnrichmentService(store, context_retriever=retriever)


class EnrichmentService:
    """Full-contract enrichment service over a ContractStore."""

    def __init__(
        self,
        store: ContractStore,
        *,
        context_retriever: Optional[Any] = None,
    ):
        self.store = store
        self._context_retriever = context_retriever

    @staticmethod
    def default_system_prompt() -> str:
        """Editable enrichment system prompt (Settings / pack-backed)."""
        return resolve_enrichment_system_prompt()

    # ── Context docs (design docs, marketing/company info, data dictionaries) ─

    def add_context_doc(
        self,
        table: str,
        filename: str,
        content: bytes,
        *,
        mode: str = "shared",
        stage: str = "",
    ) -> str:
        from redibis.enrich.context_profile import overlay_relpath

        if (mode or "shared") == "shared" and not stage:
            rel = _safe_context_rel(Path(filename).name)
        else:
            rel = overlay_relpath(mode, filename, stage=stage)
        key = f"{self.store._enrichment_context_prefix(table)}{rel}"
        self.store.backend.put_bytes(self.store.bucket, key, content)
        return key

    def list_context_docs(self, table: str) -> list[str]:
        prefix = self.store._enrichment_context_prefix(table)
        return [k[len(prefix):] for k in
                self.store.backend.list_keys(self.store.bucket, prefix=prefix)]

    def delete_context_doc(self, table: str, filename: str) -> bool:
        key = f"{self.store._enrichment_context_prefix(table)}{_safe_context_rel(filename)}"
        if not self.store.backend.exists(self.store.bucket, key):
            return False
        self.store.backend.delete(self.store.bucket, key)
        return True

    def _read_context_docs(
        self,
        table: str,
        *,
        mode: str = "normal",
        stage_kind: Optional[str] = None,
    ) -> list[tuple[str, str]]:
        prefix = self.store._enrichment_context_prefix(table)
        docs = self._read_docs(prefix)
        selected: list[tuple[str, str]] = []
        mode_n = (mode or "normal").strip().lower()
        kind = (stage_kind or "").strip().lower()
        for name, text in docs:
            rel = name.replace("\\", "/")
            if "/" not in rel or (rel.startswith("shared/") and rel.count("/") == 1):
                selected.append((name, text))
                continue
            if mode_n == "normal" and rel.startswith("normal/") and rel.count("/") == 1:
                selected.append((name, text))
            elif mode_n == "multistep":
                if rel.startswith("multistep/shared/") and rel.count("/") == 2:
                    selected.append((name, text))
                if kind and rel.startswith(f"multistep/steps/{kind}/"):
                    selected.append((name, text))
        return selected

    # ── Example docs (uploaded few-shot examples: sample contracts, write-ups) ─

    def add_example_doc(self, table: str, filename: str, content: bytes) -> str:
        key = f"{self.store._enrichment_example_prefix(table)}{filename}"
        self.store.backend.put_bytes(self.store.bucket, key, content)
        return key

    def list_example_docs(self, table: str) -> list[str]:
        prefix = self.store._enrichment_example_prefix(table)
        return [k[len(prefix):] for k in
                self.store.backend.list_keys(self.store.bucket, prefix=prefix)]

    def delete_example_doc(self, table: str, filename: str) -> bool:
        key = f"{self.store._enrichment_example_prefix(table)}{filename}"
        if not self.store.backend.exists(self.store.bucket, key):
            return False
        self.store.backend.delete(self.store.bucket, key)
        return True

    def _read_example_docs(self, table: str) -> list[tuple[str, str]]:
        return self._read_docs(self.store._enrichment_example_prefix(table))

    # ── Masked sample data (de-identified CSV/Parquet/JSON for example_values) ─

    def add_sample_data(self, table: str, filename: str, content: bytes) -> str:
        key = f"{self.store._enrichment_sample_prefix(table)}{filename}"
        self.store.backend.put_bytes(self.store.bucket, key, content)
        return key

    def list_sample_data(self, table: str) -> list[str]:
        prefix = self.store._enrichment_sample_prefix(table)
        return [k[len(prefix):] for k in
                self.store.backend.list_keys(self.store.bucket, prefix=prefix)]

    def delete_sample_data(self, table: str, filename: str) -> bool:
        key = f"{self.store._enrichment_sample_prefix(table)}{filename}"
        if not self.store.backend.exists(self.store.bucket, key):
            return False
        self.store.backend.delete(self.store.bucket, key)
        return True

    def _read_sample_data(self, table: str) -> list[tuple[str, str]]:
        prefix = self.store._enrichment_sample_prefix(table)
        out: list[tuple[str, str]] = []
        for key in self.store.backend.list_keys(self.store.bucket, prefix=prefix):
            try:
                raw = self.store.backend.get_bytes(self.store.bucket, key)
            except Exception:
                continue
            name = key[len(prefix):]
            formatted = _format_sample_file(raw, name)
            if formatted:
                out.append((name, formatted[:_MAX_SAMPLE_CHARS]))
        return out

    def _read_docs(self, prefix: str) -> list[tuple[str, str]]:
        out = []
        for key in self.store.backend.list_keys(self.store.bucket, prefix=prefix):
            try:
                text = self.store.backend.get_text(self.store.bucket, key)
            except Exception:
                continue
            out.append((key[len(prefix):], text[:20000]))  # cap per doc
        return out

    def _pii_evidence_for_enrich(self, cfg: RedibisConfig) -> dict:
        """Catalog + NER inventory summary for LLM tuning context (no cell values)."""
        from redibis.pii.export import export_ner_models, list_regex_catalog_summary

        return {
            "regex_catalog_summary": list_regex_catalog_summary(active_only=True)[:50],
            "ner_models": export_ner_models(cfg.pii.models_dir)[:10],
        }

    # ── Shared context + prompt building ─────────────────────────────────

    def build_context(
        self,
        table: str,
        provider: EnrichmentProvider,
        *,
        system_prompt: Optional[str] = None,
        extra_instructions: Optional[str] = None,
        example_contracts: Optional[list[str]] = None,
        memory_config: Optional[MemoryConfig] = None,
        redibis_config: Optional[RedibisConfig] = None,
        external_masked_acknowledged: bool = False,
        run_id: Optional[str] = None,
        context_bundle: Optional[dict] = None,
        input_contract: Optional[dict] = None,
        similar_context_enabled: Optional[bool] = None,
        similar_context_k: Optional[int] = None,
        sample_policy: Optional[str] = None,
        enrichment_pack: Optional[Any] = None,
        approve_context_reduction: Optional[str] = None,
        reduction_approval_source: str = "cli_flag",
        reduction_approved_by: str = "",
        enforce_pack_reduction_approval: bool = True,
        mode: str = "normal",
        stage_kind: Optional[str] = None,
    ) -> EnrichmentContext:
        """Assemble the shared enrichment context (UI = CLI = agentic)."""
        from redibis.enrich.context import contract_from_bundle
        from redibis.enrich.evidence import (
            build_full_column_context,
            load_run_detection_index,
            resolve_column_sample,
        )
        from redibis.classification.pack_digest import build_classification_pack_digest
        from redibis.classification.policy_pack import get_builtin_pack
        from redibis.telemetry.model_gateway import _CLOUD_PROVIDER_NAMES

        if context_bundle:
            c_det = contract_from_bundle(context_bundle)
            if not c_det:
                raise KeyError(f"No contract in context bundle for {table!r}")
            instructions = context_bundle.get("instructions") or {}
            if instructions.get("system_prompt"):
                system_prompt = instructions["system_prompt"]
            if instructions.get("extra_instructions"):
                extra_instructions = instructions["extra_instructions"]
            run_id = run_id or context_bundle.get("run_id")
            bundle_columns = {
                item.get("name"): item
                for item in (context_bundle.get("columns") or [])
                if item.get("name")
            }
        elif input_contract is not None:
            c_det = copy.deepcopy(input_contract)
            bundle_columns = {}
        else:
            active = self.store.get_active(table)
            if active is None:
                raise KeyError(f"No active contract for {table!r}")
            c_det = copy.deepcopy(active)
            bundle_columns = {}

        cfg = redibis_config or RedibisConfig()
        mem_cfg = memory_config or getattr(self.store, "memory_config", None) or MemoryConfig()
        sc_enabled = similar_context_enabled
        sc_k = similar_context_k
        if context_bundle:
            global_cfg = context_bundle.get("global") or {}
            sc_bundle = global_cfg.get("similar_context") or {}
            if sc_enabled is None and "enabled" in sc_bundle:
                sc_enabled = bool(sc_bundle["enabled"])
            if sc_k is None and sc_bundle.get("k"):
                sc_k = int(sc_bundle["k"])
            if sample_policy is None and global_cfg.get("sample_policy"):
                sample_policy = str(global_cfg["sample_policy"])
        if sc_enabled is None:
            sc_enabled = cfg.enrich.similar_context.enabled
        if sc_k is None:
            sc_k = cfg.enrich.similar_context.k

        is_cloud = (provider.name or "").lower() in _CLOUD_PROVIDER_NAMES
        if sample_policy is None:
            sample_policy = "masked" if is_cloud else "raw"
        if external_masked_acknowledged and sample_policy == "raw":
            sample_policy = "masked"

        context_docs = self._read_context_docs(table, mode=mode, stage_kind=stage_kind)
        custom_dir = getattr(getattr(cfg.enrich, "context", None), "custom_dir", "") or ""
        try:
            from redibis.enrich.context_profile import CustomContextStore

            store = CustomContextStore(custom_dir or None)
            for rel, text in store.read_for(mode=mode, stage_kind=stage_kind):
                context_docs.append((f"custom:{rel}", text[:20000]))
        except Exception:
            pass
        example_docs = self._read_example_docs(table)
        sample_data = self._read_sample_data(table)
        if external_masked_acknowledged and sample_policy == "masked" and not sample_data:
            raise ValueError(
                "Upload masked sample data before attesting external-safe enrichment "
                "(export from the Masking page, then upload under Masked sample data)."
            )

        memory_context = self._memory_context_for(table, c_det, mem_cfg)
        if context_bundle:
            for hint in context_bundle.get("memory_hints") or []:
                if hint.get("kind") == "memory_section" and hint.get("text"):
                    memory_context = hint["text"]
                    break

        pack_name = getattr(cfg.classification, "policy_pack", "telecom")
        if context_bundle:
            pack_name = (context_bundle.get("instructions") or {}).get("policy_pack") or pack_name
        pack_digest = build_classification_pack_digest(pack_name)
        try:
            policy_pack = get_builtin_pack(pack_name)
        except Exception:
            policy_pack = None

        runs_bucket = getattr(cfg.storage, "runs_bucket", "pii-reports")
        run_detections = load_run_detection_index(
            self.store.backend, runs_bucket, table,
        )
        telemetry_all = self.store.metadata.get_column_telemetry(table)
        masked_by_col = _masked_samples_from_files(sample_data) if sample_policy == "masked" else {}

        similar_by_col: dict[str, list] = {}
        use_similar = sc_enabled and mem_cfg.enabled and self._context_retriever is not None
        if use_similar:
            try:
                from redibis.memory.retriever import build_similar_columns_per_column

                similar_by_col = build_similar_columns_per_column(
                    table, c_det, self._context_retriever, top_k=sc_k,
                )
            except Exception:
                similar_by_col = {}

        column_context: list[dict] = []
        for prop in _iter_columns(c_det):
            name = prop.get("name")
            if not name:
                continue
            tel = dict(telemetry_all.get(name) or {})
            bundle_col = bundle_columns.get(name) or {}
            sample = resolve_column_sample(
                column=str(name),
                telemetry=tel,
                bundle_sample=bundle_col.get("sample"),
                masked_samples=masked_by_col,
                sample_policy=sample_policy,
            )
            column_context.append(
                build_full_column_context(
                    prop,
                    tel,
                    run_detection=run_detections.get(name),
                    similar_columns=similar_by_col.get(str(name), []),
                    sample=sample,
                    sample_policy=sample_policy,
                    policy=policy_pack,
                    table=table,
                )
            )

        table_meta = _table_prompt_meta(c_det)
        contract_for_prompt = {
            "table": table,
            "physicalName": _table_physical_name(c_det),
            "existing_description": table_meta["existing_description"],
            "existing_purpose": table_meta["existing_purpose"],
            "table_tags": table_meta["table_tags"],
            "columns": column_context,
        }
        prompt_redacted = is_cloud or (external_masked_acknowledged and sample_policy == "masked")

        pack_stage_addendum = ""
        if enrichment_pack is not None:
            from redibis.enrich.packs.context_compiler import (
                pack_profile_base,
                pack_stage_prompt,
            )

            if not system_prompt:
                # A pack that declares prompt.normal / prompt.shared owns the base
                # prompt for that mode; otherwise the prompt store still drives it.
                system_prompt = pack_profile_base(enrichment_pack, mode=mode) or None
            if (mode or "normal").strip().lower() == "multistep":
                pack_stage_addendum = pack_stage_prompt(
                    enrichment_pack, stage_kind=stage_kind,
                )
        system_prompt = system_prompt or resolve_enrichment_system_prompt(
            mode=mode, stage_kind=stage_kind,
        )
        if pack_stage_addendum:
            system_prompt = system_prompt.rstrip() + "\n\n" + pack_stage_addendum
        if extra_instructions and extra_instructions.strip():
            system_prompt = (
                system_prompt.rstrip()
                + "\n\n# ADDITIONAL INSTRUCTIONS\n"
                + extra_instructions.strip()
            )
        system_prompt = (
            system_prompt.rstrip()
            + "\n\n# CLASSIFICATION PACK DIGEST\n"
            + _yaml_dump(pack_digest)
        )

        pack_prov: dict = {}
        pack_user_addendum = ""
        if enrichment_pack is not None:
            from redibis.enrich.packs.context_compiler import (
                compile_pack_context,
                require_approved_context,
            )

            compiled = compile_pack_context(
                enrichment_pack,
                c_det,
                provider_name=getattr(provider, "name", "") or "demo",
                model=getattr(provider, "model", "") or "",
                sample_policy=sample_policy,
                extra_instructions=extra_instructions or "",
                approve_reduction_plan_id=approve_context_reduction,
                approval_source=reduction_approval_source,
                approved_by=reduction_approved_by,
                mode=mode,
                stage_kind=stage_kind,
            )
            if enforce_pack_reduction_approval:
                require_approved_context(compiled)
            system_prompt = system_prompt.rstrip() + "\n\n" + compiled.system_addendum
            pack_user_addendum = compiled.user_addendum
            pack_prov = compiled.to_provenance()

        examples = []
        user_example_tables = list(example_contracts or [])
        for ex_table in user_example_tables:
            ex = self.store.get_active(ex_table)
            if ex is not None:
                examples.append(ex)

        gold_example_included = False
        if sample_policy == "raw" and not examples and not example_docs and not user_example_tables:
            from redibis.enrich.gold import load_gold_example_contract

            gold_example = load_gold_example_contract()
            if gold_example:
                examples.append(gold_example)
                gold_example_included = True

        pii_evidence = (
            self._pii_evidence_for_enrich(cfg) if cfg.enrich.include_pii_evidence else None
        )
        user_prompt = self._build_user_prompt(
            contract_for_prompt,
            context_docs,
            examples,
            example_docs,
            sample_data,
            memory_context=memory_context,
            external_redacted=prompt_redacted,
            pii_evidence=pii_evidence,
            sample_policy=sample_policy,
        )
        if pack_user_addendum:
            user_prompt = pack_user_addendum.rstrip() + "\n\n" + user_prompt

        return EnrichmentContext(
            table=table,
            run_id=run_id or _utc_now_iso(),
            c_det=c_det,
            column_context=column_context,
            contract_for_prompt=contract_for_prompt,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_docs=context_docs,
            example_docs=example_docs,
            sample_data=sample_data,
            example_contracts=user_example_tables + (["__gold__"] if gold_example_included else []),
            memory_context=memory_context,
            pack_digest=pack_digest,
            prompt_redacted=prompt_redacted,
            sample_policy=sample_policy,
            is_cloud=is_cloud,
            enrichment_pack=(
                enrichment_pack.provenance()
                if enrichment_pack is not None and hasattr(enrichment_pack, "provenance")
                else {}
            ),
            pack_context_provenance=pack_prov,
        )

    def _build_user_prompt(
        self,
        contract_for_prompt: dict,
        context_docs: list[tuple[str, str]],
        example_contracts: list[dict],
        example_docs: Optional[list[tuple[str, str]]] = None,
        sample_data: Optional[list[tuple[str, str]]] = None,
        memory_context: Optional[str] = None,
        external_redacted: bool = False,
        pii_evidence: Optional[dict] = None,
        sample_policy: str = "raw",
    ) -> str:
        parts = [
            "# COLUMN EVIDENCE (deterministic verdict + reasoning + engines + profiling + similar)\n",
            "Below is how redibis classified each column and why. "
            "You may agree, refine, or override any of it — when you diverge, briefly say why.\n",
            "Also return table.description (purpose, grain, key entities, time scope) "
            "from this evidence; refine existing_description when it is already present. "
            "Never copy sample values into the table narrative.\n",
            _yaml_dump(contract_for_prompt),
        ]
        if external_redacted:
            parts.append(
                "\n# NOTE: External/residency policy — raw samples redacted; column `sample` "
                "values are masked or omitted. Output contract must still use masked skeletons only.\n"
            )
        elif sample_policy == "raw":
            parts.append(
                "\n# NOTE: Local LLM — raw sample values may appear under column `sample`. "
                "Output contract must NEVER copy them; use masked skeletons in example_values.\n"
            )
        if memory_context:
            parts.append("\n" + memory_context.rstrip() + "\n")
        if example_contracts:
            parts.append("\n# EXAMPLE CONTRACTS (few-shot shape reference — do not edit)\n")
            for ex in example_contracts:
                parts.append(_yaml_dump(_strip_internal(ex)))
        if example_docs:
            parts.append("\n# EXAMPLE DOCUMENTS (uploaded few-shot examples)\n")
            for name, text in example_docs:
                parts.append(f"\n## {name}\n{text}\n")
        if context_docs:
            parts.append(
                "\n# CONTEXT DOCUMENTS (design docs, company/marketing info)\n"
            )
            for name, text in context_docs:
                parts.append(f"\n## {name}\n{text}\n")
        if sample_data:
            label = (
                "MASKED SAMPLE DATA (de-identified rows — input context only)"
                if sample_policy == "masked"
                else "UPLOADED SAMPLE DATA (steward-provided — input context only)"
            )
            parts.append(f"\n# {label}\n")
            for name, text in sample_data:
                parts.append(f"\n## {name}\n{text}\n")
        if pii_evidence:
            parts.append(
                "\n# PII DETECTION CATALOG (pattern names, models — no cell values)\n"
            )
            parts.append(_yaml_dump(pii_evidence))
        return "\n".join(parts)

    # ── Enrich ────────────────────────────────────────────────────────────

    def _apply_active_write(
        self,
        *,
        table: str,
        candidate: dict,
        c_det: dict,
        delta: dict,
        pii_changes: list,
        pii_reasons: dict[str, str],
        enriched_by: str,
        effective_run_id: str,
        meta: dict,
        sc_enabled: bool,
        sc_k: int,
        context_bundle: Optional[dict],
        run_writer: Optional["RunOutputWriter"],
        result: dict,
        delta_errors: list[str],
        scrubbed_cols: list[str],
        provider: EnrichmentProvider,
        system_prompt: str,
        user_prompt: str,
        raw: str,
        diff_report: dict,
    ) -> tuple[bool, str, list[str], str, dict]:
        """Upsert active contract + lifecycle side effects (always-active path)."""
        import copy

        partial = copy.deepcopy(candidate)
        partial.pop("enrichment_meta", None)
        upsert = self.store.upsert(
            partial,
            table=table,
            workflow="business",
            run_id=effective_run_id,
            validate=False,
            strip_pii_quality=True,
        )
        version_after = upsert.version_after or ""

        from redibis.contracts.lifecycle import (
            apply_llm_classification_decisions,
            append_lifecycle_provenance,
            resolve_enrichment_overlay_changes,
        )

        overlay_changes = resolve_enrichment_overlay_changes(
            c_det,
            candidate,
            explicit_changes=pii_changes,
            delta=delta,
        )
        apply_llm_classification_decisions(
            self.store,
            table,
            overlay_changes,
            enriched_by=enriched_by,
            run_id=effective_run_id,
        )

        if pii_reasons:
            reviewed_at = _utc_now_iso()
            self.store.metadata.merge_column_telemetry(
                table,
                {
                    col: {
                        "llm_pii_reason": reason,
                        "llm_pii_review_at": reviewed_at,
                    }
                    for col, reason in pii_reasons.items()
                },
            )

        append_lifecycle_provenance(
            self.store.metadata,
            table,
            workflow="business",
            run_id=effective_run_id,
            run_uuid=upsert.run_uuid,
            active_source="llm",
            det_audit_version=c_det.get("version"),
            engines={"similar_context": f"on(k={sc_k})" if sc_enabled else "off"},
            prompt_hash=meta.get("system_prompt_hash"),
            prior_version=upsert.version_before,
            llm_version=version_after,
        )
        meta["auto_written"] = True
        meta["version_after"] = version_after
        meta["det_version"] = c_det.get("version")
        meta["prior_version"] = upsert.version_before
        if context_bundle:
            from redibis.enrich.context import clear_context_draft

            clear_context_draft(self.store, table)

        run_artifacts: dict = {}
        artifact_errors: list[str] = []
        if run_writer is not None:
            from redibis.contracts.lifecycle import (
                write_enrich_debug_log,
                write_enrichment_meta_artifact,
                write_llm_lifecycle_artifacts,
            )
            from redibis.enrich.delta_schema import assert_odcs_v3_contract

            run_artifacts.update(write_enrichment_meta_artifact(run_writer, meta))
            llm_out = write_llm_lifecycle_artifacts(
                run_writer,
                c_det,
                candidate,
                run_id=effective_run_id,
                active_version=version_after or candidate.get("version"),
                det_version=c_det.get("version"),
                agentic=bool(meta.get("multistep")),
                agentic_steps=list(meta.get("stages") or []) if meta.get("multistep") else None,
            )
            run_artifacts.update(llm_out.get("keys") or {})
            llm_key = (llm_out.get("keys") or {}).get("contract.llm.yaml")
            artifact_doc = (
                run_writer.backend.get_yaml(run_writer.bucket, llm_key)
                if llm_key
                else candidate
            )
            artifact_errors = assert_odcs_v3_contract(artifact_doc)
            if artifact_errors:
                meta["artifact_odcs_errors"] = artifact_errors
                result["warnings"] = list(result.get("warnings") or []) + artifact_errors
            run_artifacts.update(
                write_enrich_debug_log(
                    run_writer,
                    table=table,
                    run_id=effective_run_id,
                    provider=provider.name or "unknown",
                    model=provider.model or "",
                    valid=result["valid"],
                    errors=list(result.get("errors") or []) + delta_errors + artifact_errors,
                    warnings=result.get("warnings"),
                    prompt_chars=len(system_prompt or "") + len(user_prompt or ""),
                    response_chars=len(raw or ""),
                )
            )

        from redibis.agents.agent_report import (
            build_agent_report,
            compute_enrichment_status,
            write_agent_report,
        )

        enrichment_status = compute_enrichment_status(
            valid=result["valid"],
            errors=list(result.get("errors") or []),
            warnings=list(result.get("warnings") or []),
            delta_errors=delta_errors,
            scrubbed_cols=scrubbed_cols,
            artifact_odcs_errors=artifact_errors,
        )
        meta["enrichment_status"] = enrichment_status

        report = build_agent_report(
            table=table,
            run_id=effective_run_id,
            c_det=c_det,
            c_llm=candidate,
            valid=result["valid"],
            errors=list(result.get("errors") or []),
            warnings=list(result.get("warnings") or []),
            enrichment_status=enrichment_status,
            diff_report=diff_report,
            enrichment_meta=meta,
        )
        if run_writer is not None:
            run_artifacts.update(
                write_agent_report(
                    report=report,
                    run_writer=run_writer,
                    metadata_store=self.store.metadata,
                    table=table,
                )
            )
        else:
            tel = self.store.metadata.get_telemetry(table)
            tel["enrichment_status"] = enrichment_status
            tel["last_agent_report"] = {
                "run_id": effective_run_id,
                "enrichment_status": enrichment_status,
                "generated_at": report.get("generated_at"),
            }
            self.store.metadata.backend.put_json(
                self.store.metadata.bucket,
                self.store.metadata._telemetry_key(table),
                tel,
            )

        return True, version_after, artifact_errors, enrichment_status, run_artifacts

    def commit_active_write(self, result: EnrichmentResult) -> EnrichmentResult:
        """Persist a deferred enrich candidate to active/ (agent retry write-once)."""
        if result.auto_written:
            return result
        ctx = result.deferred_context or {}
        if not ctx:
            raise ValueError("deferred enrich result has no commit context")

        candidate = copy.deepcopy(result.candidate)
        meta = dict(result.enrichment_meta or {})
        result_dict = {
            "valid": result.valid,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
        }
        auto_written, version_after, artifact_errors, enrichment_status, run_artifacts = (
            self._apply_active_write(
                table=result.table,
                candidate=candidate,
                c_det=ctx["c_det"],
                delta=ctx.get("delta") or {},
                pii_changes=list(ctx.get("pii_changes") or []),
                pii_reasons=dict(ctx.get("pii_reasons") or {}),
                enriched_by=str(ctx.get("enriched_by") or "agent_pipeline"),
                effective_run_id=str(ctx.get("effective_run_id") or ""),
                meta=meta,
                sc_enabled=bool(ctx.get("sc_enabled")),
                sc_k=int(ctx.get("sc_k") or 0),
                context_bundle=ctx.get("context_bundle"),
                run_writer=ctx.get("run_writer"),
                result=result_dict,
                delta_errors=list(ctx.get("delta_errors") or []),
                scrubbed_cols=list(ctx.get("scrubbed_cols") or []),
                provider=ctx["provider"],
                system_prompt=str(ctx.get("system_prompt") or ""),
                user_prompt=str(ctx.get("user_prompt") or ""),
                raw=str(result.raw_response or ""),
                diff_report=dict(result.diff_report or {}),
            )
        )
        candidate["enrichment_meta"] = meta
        return EnrichmentResult(
            table=result.table,
            candidate=candidate,
            valid=result.valid,
            errors=result.errors,
            warnings=result.warnings,
            enrichment_meta=meta,
            raw_response=result.raw_response,
            diff_report=result.diff_report,
            auto_written=auto_written,
            version_after=version_after,
            run_artifacts={**result.run_artifacts, **run_artifacts},
            enrichment_status=enrichment_status,
            deferred_context={},
        )

    def enrich(
        self,
        table: str,
        provider: EnrichmentProvider,
        *,
        system_prompt: Optional[str] = None,
        extra_instructions: Optional[str] = None,
        example_contracts: Optional[list[str]] = None,
        enriched_by: str = "system",
        memory_config: Optional[MemoryConfig] = None,
        redibis_config: Optional[RedibisConfig] = None,
        rai_config: Optional[RAIConfig] = None,
        residency: str = "",
        external_masked_acknowledged: bool = False,
        bypass_rai: bool = False,
        run_writer: Optional["RunOutputWriter"] = None,
        run_id: Optional[str] = None,
        context_bundle: Optional[dict] = None,
        input_contract: Optional[dict] = None,
        similar_context_enabled: Optional[bool] = None,
        similar_context_k: Optional[int] = None,
        enrichment_context: Optional[EnrichmentContext] = None,
        sample_policy: Optional[str] = None,
        write_active: bool = True,
        enrichment_pack: Optional[Any] = None,
        approve_context_reduction: Optional[str] = None,
        reduction_approval_source: str = "cli_flag",
        reduction_approved_by: str = "",
        enforce_pack_reduction_approval: bool = True,
        mode: str = "normal",
        stage_kind: Optional[str] = None,
        llm_attempt: int = 1,
    ) -> EnrichmentResult:
        cfg = redibis_config or RedibisConfig()
        ctx = enrichment_context or self.build_context(
            table,
            provider,
            system_prompt=system_prompt,
            extra_instructions=extra_instructions,
            example_contracts=example_contracts,
            memory_config=memory_config,
            redibis_config=cfg,
            external_masked_acknowledged=external_masked_acknowledged,
            run_id=run_id,
            context_bundle=context_bundle,
            input_contract=input_contract,
            similar_context_enabled=similar_context_enabled,
            similar_context_k=similar_context_k,
            sample_policy=sample_policy,
            enrichment_pack=enrichment_pack,
            approve_context_reduction=approve_context_reduction,
            reduction_approval_source=reduction_approval_source,
            reduction_approved_by=reduction_approved_by,
            enforce_pack_reduction_approval=enforce_pack_reduction_approval,
            mode=mode,
            stage_kind=stage_kind,
        )
        c_det = ctx.c_det
        system_prompt = ctx.system_prompt
        user_prompt = ctx.user_prompt
        contract_for_prompt = ctx.contract_for_prompt
        context_docs = ctx.context_docs
        example_docs = ctx.example_docs
        sample_data = ctx.sample_data
        memory_context = ctx.memory_context
        prompt_redacted = ctx.prompt_redacted
        is_cloud = ctx.is_cloud
        effective_run_id = ctx.run_id

        sc_enabled = similar_context_enabled
        sc_k = similar_context_k
        if sc_enabled is None:
            sc_enabled = cfg.enrich.similar_context.enabled
        if sc_k is None:
            sc_k = cfg.enrich.similar_context.k

        rai_attested = bool(
            (external_masked_acknowledged and sample_data)
            or (is_cloud and not cfg.rai.hard_block_external_pii)
        )
        run_artifacts: dict = {}

        if run_writer is not None:
            try:
                from redibis.contracts.lifecycle import write_deterministic_snapshot

                run_artifacts.update(
                    write_deterministic_snapshot(
                        run_writer,
                        c_det,
                        run_id=effective_run_id,
                        version=c_det.get("version"),
                    )
                )
                run_artifacts.update(
                    _write_prompt_context_artifact(
                        run_writer,
                        table=table,
                        run_id=effective_run_id,
                        provider_name=provider.name or provider.model or "unknown",
                        model=provider.model,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        prompt_redacted=prompt_redacted,
                        contract_for_prompt=contract_for_prompt,
                        context_docs=[n for n, _ in context_docs],
                        example_docs=[n for n, _ in example_docs],
                        sample_data=[n for n, _ in sample_data],
                        example_contracts=ctx.example_contracts,
                        memory_context=bool(memory_context),
                        pack_digest=ctx.pack_digest,
                        enrichment_pack=ctx.enrichment_pack,
                        pack_context_provenance=ctx.pack_context_provenance,
                    )
                )
            except Exception as exc:
                log.warning(
                    "Failed to persist enrich prompt artifacts table=%r run_id=%r: %s",
                    table,
                    effective_run_id,
                    exc,
                )

        from redibis.enrich.rai_gate import compute_enrich_rai_preflight
        from redibis.telemetry.model_gateway import guarded_model_call, resolve_provider_residency

        rai_for_call = RAIConfig(enabled=False) if bypass_rai else (rai_config or cfg.rai)
        if bypass_rai:
            log.warning(
                "Enrichment RAI bypassed (dev/open-source) table=%r provider=%r",
                table, provider.name,
            )

        effective_residency = resolve_provider_residency(
            provider, rai_config=cfg.rai, override=residency,
        )
        preflight = compute_enrich_rai_preflight(
            contract=c_det,
            table=table,
            provider=provider,
            redibis_config=cfg if not bypass_rai else RedibisConfig(),
            sample_data_count=len(sample_data),
            external_masked_acknowledged=rai_attested,
            user_prompt=user_prompt,
        )
        if bypass_rai:
            preflight = {**preflight, "would_block": False, "bypass_rai": True}
        elif is_cloud and rai_attested and not external_masked_acknowledged:
            preflight = {
                **preflight,
                "would_block": False,
                "cloud_auto_redacted": True,
                "hints": (preflight.get("hints") or [])
                + ["Cloud provider: PII metadata auto-redacted in prompt (open-source default)."],
            }
        log.info(
            "Enrichment LLM preflight table=%r provider=%r model=%r residency=%r "
            "pii_columns=%d sample_files=%d attested_masked=%s would_block=%s prompt_chars=%d",
            table,
            provider.name,
            provider.model,
            effective_residency,
            len(preflight.get("pii_columns") or []),
            len(sample_data),
            external_masked_acknowledged,
            preflight.get("would_block"),
            len(user_prompt),
        )
        if preflight.get("would_block"):
            log.warning(
                "Enrichment blocked by RAI table=%r residency=%r reason=%r pii_columns=%s",
                table,
                effective_residency,
                preflight.get("block_reason"),
                preflight.get("pii_columns"),
            )

        attested = rai_attested or bypass_rai
        from contextlib import ExitStack
        from redibis.evidence.sanitize import sanitize_mapping
        from redibis.telemetry.llm_evidence import llm_evidence_recorder

        local_dir = None
        if run_writer is not None:
            from redibis.store.storage_backend import LocalBackend
            backend = getattr(run_writer, "backend", None)
            if isinstance(backend, LocalBackend):
                try:
                    local_dir = backend._full_path(run_writer.bucket, run_writer.prefix)
                except Exception:
                    local_dir = None
        stack = ExitStack()
        stack.enter_context(llm_evidence_recorder(
            run_dir=local_dir,
            run_id=effective_run_id,
            table=table,
            execution_mode="agentic" if mode == "multistep" else "single_llm",
            run_writer=run_writer,
            config_sanitized=sanitize_mapping(cfg) if cfg is not None else {},
            config=redibis_config or cfg,
        ))
        try:
            raw, rai_report = guarded_model_call(
                lambda: provider.complete(system_prompt, user_prompt, json_mode=True),
                model_id=provider.name or provider.model or "unknown",
                provider=provider,
                contract=c_det,
                table=table,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                residency=residency,
                rai_config=rai_for_call,
                redibis_config=redibis_config,
                attested_masked_external=attested,
                model_role="contract.enrichment",
                run_id=effective_run_id,
                step_id=str(stage_kind or ""),
                attempt=int(llm_attempt or 1),
                context={
                    "prompt_redacted": prompt_redacted,
                    "pack_digest": getattr(ctx, "pack_digest", None),
                    "mode": mode,
                    "stage_kind": stage_kind,
                },
            )
        finally:
            stack.close()
        log.info(
            "Enrichment LLM call completed table=%r provider=%r model=%r "
            "response_chars=%d blocked=%s",
            table,
            provider.name,
            provider.model,
            len(raw or ""),
            bool(rai_report and any(d.get("blocked") for d in (rai_report.get("decisions") or []))),
        )
        delta_raw = provider.parse_json(raw)
        from redibis.enrich.delta_schema import extract_review_findings, parse_enrichment_delta

        delta, delta_errors = parse_enrichment_delta(delta_raw)
        review_findings = extract_review_findings(delta_raw)
        if delta_errors:
            log.warning(
                "Enrichment delta validation table=%r run_id=%r errors=%s",
                table, effective_run_id, delta_errors,
            )

        pii_demotions: list[str] = []
        pii_changes: list[dict] = []
        pii_reasons = _extract_pii_reasons(delta)
        candidate = apply_enrichment(
            c_det, delta, pii_demotions=pii_demotions, pii_changes=pii_changes,
        )
        from redibis.contracts.privacy import scrub_pii_enrichment_output

        scrubbed_cols = scrub_pii_enrichment_output(candidate)

        from redibis.contracts.contract_diff import diff_contracts
        diff_report = diff_contracts(c_det, candidate)

        meta = {
            "provider": provider.name,
            "model": provider.model,
            "system_prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest()[:16],
            "context_docs": [n for n, _ in context_docs],
            "example_docs": [n for n, _ in example_docs],
            "sample_data": [n for n, _ in sample_data],
            "example_contracts": ctx.example_contracts,
            "memory_context": bool(memory_context),
            "sample_policy": ctx.sample_policy,
            "pii_output_scrubbed": scrubbed_cols,
            "pii_reasons": pii_reasons,
            "pii_demotions": pii_demotions,
            "pii_changes": pii_changes,
            "diff_report": diff_report,
            "enriched_at": _utc_now_iso(),
            "enriched_by": enriched_by,
            "external_masked_acknowledged": external_masked_acknowledged,
            "prompt_redacted": prompt_redacted,
            "cloud_provider": is_cloud,
            "rai_attested": rai_attested,
            "bypass_rai": bypass_rai,
            "rai_preflight": preflight,
            "delta_validation_errors": delta_errors,
            "review_findings": review_findings,
        }
        if ctx.enrichment_pack:
            meta["pack"] = dict(ctx.pack_context_provenance or ctx.enrichment_pack)
        if rai_report is not None:
            meta["rai"] = rai_report

        result = self.store.validate(candidate, strict=False)
        auto_written = False
        version_after = ""
        artifact_errors: list[str] = []
        enrichment_status = "clean"
        deferred_context: dict = {}

        if write_active:
            auto_written, version_after, artifact_errors, enrichment_status, persist_artifacts = (
                self._apply_active_write(
                    table=table,
                    candidate=candidate,
                    c_det=c_det,
                    delta=delta,
                    pii_changes=pii_changes,
                    pii_reasons=pii_reasons,
                    enriched_by=enriched_by,
                    effective_run_id=effective_run_id,
                    meta=meta,
                    sc_enabled=sc_enabled,
                    sc_k=sc_k,
                    context_bundle=context_bundle,
                    run_writer=run_writer,
                    result=result,
                    delta_errors=delta_errors,
                    scrubbed_cols=scrubbed_cols,
                    provider=provider,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    raw=raw,
                    diff_report=diff_report,
                )
            )
            run_artifacts.update(persist_artifacts)
        else:
            from redibis.enrich.delta_schema import assert_odcs_v3_contract
            from redibis.agents.agent_report import compute_enrichment_status

            slim = copy.deepcopy(candidate)
            slim.pop("enrichment_meta", None)
            artifact_errors = assert_odcs_v3_contract(slim)
            if artifact_errors:
                meta["artifact_odcs_errors"] = artifact_errors
            enrichment_status = compute_enrichment_status(
                valid=result["valid"],
                errors=list(result.get("errors") or []),
                warnings=list(result.get("warnings") or []),
                delta_errors=delta_errors,
                scrubbed_cols=scrubbed_cols,
                artifact_odcs_errors=artifact_errors,
            )
            meta["enrichment_status"] = enrichment_status
            deferred_context = {
                "c_det": c_det,
                "delta": delta,
                "pii_changes": pii_changes,
                "pii_reasons": pii_reasons,
                "enriched_by": enriched_by,
                "effective_run_id": effective_run_id,
                "sc_enabled": sc_enabled,
                "sc_k": sc_k,
                "context_bundle": context_bundle,
                "run_writer": run_writer,
                "delta_errors": delta_errors,
                "scrubbed_cols": scrubbed_cols,
                "provider": provider,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }

        candidate["enrichment_meta"] = meta

        return EnrichmentResult(
            table=table, candidate=candidate, valid=result["valid"],
            errors=result["errors"], warnings=result["warnings"],
            enrichment_meta=meta, raw_response=raw, diff_report=diff_report,
            auto_written=auto_written, version_after=version_after,
            run_artifacts=run_artifacts, enrichment_status=enrichment_status,
            deferred_context=deferred_context,
        )

    # ── Candidate storage ──────────────────────────────────────────────────

    def _write_candidate(self, table: str, candidate: dict) -> str:
        key = self.store._enrichment_candidate_key(table)
        self.store.backend.put_yaml(self.store.bucket, key, candidate)
        return key

    def get_candidate(self, table: str) -> Optional[dict]:
        key = self.store._enrichment_candidate_key(table)
        if not self.store.backend.exists(self.store.bucket, key):
            return None
        return self.store.backend.get_yaml(self.store.bucket, key)

    def get_diff_report(self, table: str) -> Optional[dict]:
        """Return the stored diff report for a pending enrichment candidate."""
        candidate = self.get_candidate(table)
        if candidate is None:
            return None
        meta = candidate.get("enrichment_meta") or {}
        return meta.get("diff_report")

    def validate_candidate(self, table: str) -> dict:
        candidate = self.get_candidate(table)
        if candidate is None:
            raise KeyError(f"No enrichment candidate for {table!r}")
        partial = copy.deepcopy(candidate)
        partial.pop("enrichment_meta", None)
        return self.store.validate(partial, strict=False)

    # ── Merge (validity-gated) ──────────────────────────────────────────────

    def merge_candidate(self, table: str) -> dict:
        """
        Legacy merge for stale enrichment candidates (pre-lifecycle hold).

        New enrich() runs auto-write; this only applies when an old candidate
        remains in the contracts bucket.
        """
        candidate = self.get_candidate(table)
        if candidate is None:
            raise KeyError(f"No enrichment candidate for {table!r}")
        meta = candidate.get("enrichment_meta") or {}
        partial = copy.deepcopy(candidate)
        partial.pop("enrichment_meta", None)
        result = self.store.validate(partial, strict=False)
        if not result["valid"]:
            raise ValueError("Enrichment candidate is invalid; merge refused. "
                             "Errors: " + "; ".join(result["errors"]))
        meta = candidate.get("enrichment_meta") or {}
        enriched_by = meta.get("enriched_by") or "system"
        from redibis.contracts.privacy import scrub_pii_enrichment_output

        scrub_pii_enrichment_output(partial)
        upsert = self.store.upsert(partial, table=table, workflow="business",
                                   run_id=f"enrich_{_utc_now_iso()}", validate=False,
                                   strip_pii_quality=True)
        from redibis.contracts.lifecycle import (
            apply_llm_classification_decisions,
            resolve_enrichment_overlay_changes,
        )

        pii_changes = resolve_enrichment_overlay_changes(
            self.store.get_active(table) or {},
            candidate,
            explicit_changes=meta.get("pii_changes") or [],
        )
        if not pii_changes:
            for col in meta.get("pii_demotions") or []:
                pii_changes.append({"column": col, "status": "not_pii"})
        apply_llm_classification_decisions(
            self.store,
            table,
            pii_changes,
            enriched_by=enriched_by,
            run_id=f"enrich_{meta.get('enriched_at', '')}",
        )
        # Clear the candidate now that it has merged.
        self.store.backend.delete(self.store.bucket,
                                  self.store._enrichment_candidate_key(table))
        return {"merged": True, "version_after": upsert.version_after,
                "contract_uuid": upsert.contract_uuid,
                "enrichment_meta": meta,
                "pii_demotions_applied": list(meta.get("pii_demotions") or [])}

    def _memory_context_for(
        self,
        table: str,
        contract: dict,
        memory_config: MemoryConfig,
    ) -> Optional[str]:
        if not memory_config.enabled or self._context_retriever is None:
            return None
        from redibis.memory.retriever import build_memory_context_section
        section = build_memory_context_section(
            table,
            contract,
            self._context_retriever,
            domain=memory_config.domain,
        )
        return section or None


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no I/O) — applying the LLM delta to a contract
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pii_reasons(delta: dict) -> dict[str, str]:
    """Collect optional LLM PII override rationales from the enrichment delta."""
    out: dict[str, str] = {}
    for col, cd in (delta.get("columns") or {}).items():
        if not isinstance(cd, dict):
            continue
        pii = cd.get("pii")
        if not isinstance(pii, dict):
            continue
        reason = (pii.get("reason") or pii.get("pii_reason") or "").strip()
        if reason:
            out[str(col)] = reason
    return out


def _yaml_dump(data: Any) -> str:
    import yaml
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _table_physical_name(contract: dict) -> str:
    for schema_obj in contract.get("schema", []) or []:
        if schema_obj.get("physicalName"):
            return str(schema_obj["physicalName"])
    return ""


def _table_prompt_meta(contract: dict) -> dict[str, Any]:
    """Existing table narrative/tags for the LLM to refine, not re-invent."""
    schema = {}
    for schema_obj in contract.get("schema", []) or []:
        if isinstance(schema_obj, dict):
            schema = schema_obj
            break

    def _as_text(value: Any, *, prefer: str = "description") -> str:
        if isinstance(value, dict):
            return str(value.get(prefer) or value.get("description") or value.get("purpose") or "").strip()
        if value is None:
            return ""
        return str(value).strip()

    desc = _as_text(schema.get("description"))
    purpose = _as_text(schema.get("purpose"), prefer="purpose")
    top = contract.get("description")
    if isinstance(top, dict):
        if not desc:
            desc = str(top.get("description") or "").strip()
        if not purpose:
            purpose = str(top.get("purpose") or "").strip()
    elif isinstance(top, str) and top.strip() and not desc:
        desc = top.strip()
    tags = schema.get("tags") or []
    return {
        "existing_description": desc,
        "existing_purpose": purpose,
        "table_tags": list(tags) if isinstance(tags, list) else [],
    }


def _masked_samples_from_files(sample_files: list[tuple[str, str]]) -> dict[str, list]:
    """Parse uploaded masked sample tables into per-column value lists."""
    import pandas as pd

    out: dict[str, list] = {}
    for _name, text in sample_files:
        if not text or text.startswith("("):
            continue
        try:
            from io import StringIO
            df = pd.read_csv(StringIO(text), sep="|", skipinitialspace=True)
            if df.shape[1] <= 1:
                df = pd.read_csv(StringIO(text))
        except Exception:
            continue
        for col in df.columns:
            vals = [
                str(v) for v in df[col].dropna().head(5).tolist()
                if str(v).strip()
            ]
            if vals:
                out.setdefault(str(col), []).extend(vals)
    return out


def _strip_internal(contract: dict) -> dict:
    """Drop routing-only fields before showing the contract to the model."""
    from redibis.contracts.odcs_compat import REDIBIS_ONLY_TOP_LEVEL
    skip = REDIBIS_ONLY_TOP_LEVEL | {"audit_index"}
    return {k: v for k, v in contract.items() if k not in skip}


def _pii_column_summary(contract: dict) -> list[dict]:
    """Compact PII review checklist injected into the user prompt."""
    from redibis.contracts.privacy import column_is_pii, col_pii_engine
    rows = []
    for prop in _iter_columns(contract):
        if not column_is_pii(prop):
            continue
        ce = col_pii_engine(prop)
        rows.append({
            "column": prop.get("name"),
            "classification": prop.get("classification"),
            "entity_type": ce.get("entity_type"),
            "tags": prop.get("tags") or [],
            "confidence": ce.get("confidence"),
        })
    return rows


def _iter_columns(contract: dict):
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict) and prop.get("name"):
                yield prop


def redact_contract_for_external_llm(contract: dict) -> dict:
    """Return a contract copy safe to embed in an external-model prompt.

    Strips PII evidence, scores, and example values while preserving column
    names/types so the model can still write business definitions.
    """
    from redibis.telemetry.pii_scope import _PII_TAGS, column_has_pii_markers

    out = copy.deepcopy(contract)
    for prop in _iter_columns(out):
        if not column_has_pii_markers(prop):
            continue
        prop.pop("pii", None)
        prop.pop("maskingPolicy", None)
        tags = [t for t in (prop.get("tags") or []) if str(t).lower() not in _PII_TAGS]
        prop["tags"] = tags
        if str(prop.get("classification") or "").lower().startswith("pii"):
            prop["classification"] = "restricted"
        privacy = prop.get("privacy") or {}
        ce = privacy.get("classification_engine") or {}
        if ce:
            privacy["classification_engine"] = {
                "detected": True,
                "entity_type": ce.get("entity_type") or "UNKNOWN",
                "note": "metadata only — values redacted; use masked sample data",
            }
            prop["privacy"] = privacy
        biz = prop.get("business") or {}
        if biz.get("example_values"):
            biz["example_values"] = ["(redacted — use masked sample data section)"]
            prop["business"] = biz
    return out


def _format_sample_file(content: bytes, filename: str, *, max_rows: int = _MAX_SAMPLE_ROWS) -> str:
    """Parse a masked CSV/Parquet/JSON upload into a compact text table."""
    import pandas as pd

    fn = (filename or "").lower()
    bio = io.BytesIO(content)
    try:
        if fn.endswith((".parquet", ".pq")):
            df = pd.read_parquet(bio)
        elif fn.endswith(".json"):
            df = pd.read_json(bio)
        elif fn.endswith(".tsv"):
            df = pd.read_csv(bio, sep="\t")
        else:
            df = pd.read_csv(bio)
    except Exception:
        try:
            return content.decode("utf-8", errors="replace")[:_MAX_SAMPLE_CHARS]
        except Exception:
            return ""

    if df.empty:
        return "(empty file)"

    preview = df.head(max_rows)
    lines = [" | ".join(str(c) for c in preview.columns)]
    lines.append(" | ".join("---" for _ in preview.columns))
    for _, row in preview.iterrows():
        lines.append(" | ".join(str(v) if v is not None else "" for v in row))
    footer = f"\n({len(preview)} of {len(df)} rows shown)"
    if len(df) > max_rows:
        footer += f" — capped at {max_rows} rows for context"
    return "\n".join(lines) + footer


def _write_prompt_context_artifact(
    run_writer: "RunOutputWriter",
    *,
    table: str,
    run_id: str,
    provider_name: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    prompt_redacted: bool,
    contract_for_prompt: dict,
    context_docs: list[str],
    example_docs: list[str],
    sample_data: list[str],
    example_contracts: list[str],
    memory_context: bool,
    pack_digest: Optional[dict] = None,
    enrichment_pack: Optional[dict] = None,
    pack_context_provenance: Optional[dict] = None,
) -> dict[str, str]:
    payload = {
        "table": table,
        "run_id": run_id,
        "provider": provider_name,
        "model": model,
        "prompt_redacted": prompt_redacted,
        "prompt_chars": len(system_prompt or "") + len(user_prompt or ""),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "contract_for_prompt": contract_for_prompt,
        "docs": {
            "context_docs": context_docs,
            "example_docs": example_docs,
            "sample_data": sample_data,
            "example_contracts": example_contracts,
            "pack_digest": pack_digest or {},
        },
        "memory_context": memory_context,
        "enrichment_pack": enrichment_pack or {},
        "pack_context_provenance": pack_context_provenance or {},
    }
    from redibis.telemetry.llm_evidence import persist_restricted_artifact

    persist_restricted_artifact(run_id, "llm_prompt_context.raw.json", payload)
    key = run_writer.write("llm_prompt_context.json", _shareable_prompt_context(payload))
    return {"llm_prompt_context.json": key}


def _shareable_prompt_context(payload: dict) -> dict:
    """Runs-bucket copy: hashes + redacted/omitted prompts; no exact contract samples."""
    from redibis.evidence.redact import sha256_text, shareable_llm_call

    record = {
        "system_prompt": payload.get("system_prompt") or "",
        "user_prompt": payload.get("user_prompt") or "",
        "response_text": "",
        "context": {
            "docs": payload.get("docs") or {},
            "pack_context_provenance": payload.get("pack_context_provenance") or {},
        },
        "parsed_result": None,
    }
    share = shareable_llm_call(record)
    docs = {}
    ctx = share.get("context")
    if isinstance(ctx, dict) and isinstance(ctx.get("docs"), dict):
        docs = dict(ctx.get("docs") or {})
    orig_docs = payload.get("docs") if isinstance(payload.get("docs"), dict) else {}
    if orig_docs.get("pack_digest") and not docs.get("pack_digest"):
        docs["pack_digest"] = orig_docs["pack_digest"]
    contract = _sanitize_contract_for_prompt(payload.get("contract_for_prompt"))
    from redibis.evidence.redact import looks_like_residual_pii, sanitize_shareable_payload

    out = {
        "table": payload.get("table"),
        "run_id": payload.get("run_id"),
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "prompt_redacted": True,
        "prompt_chars": payload.get("prompt_chars"),
        "hashes": {
            "system_prompt": sha256_text(str(payload.get("system_prompt") or ""))[:16],
            "user_prompt": sha256_text(str(payload.get("user_prompt") or ""))[:16],
        },
        "system_prompt": share.get("system_prompt"),
        "user_prompt": share.get("user_prompt"),
        "docs": docs,
        "memory_context": payload.get("memory_context"),
        "sensitivity": share.get("sensitivity"),
        "omitted": list(share.get("omitted") or []),
        "contract_for_prompt": contract,
        "note": "Exact prompts and contract samples live in the governed local spool.",
    }
    if share.get("omitted"):
        out["system_prompt"] = ""
        out["user_prompt"] = ""
    out = sanitize_shareable_payload(out)
    dumped = json.dumps(out.get("contract_for_prompt") or "")
    if looks_like_residual_pii(dumped):
        out["contract_for_prompt"] = {}
        omitted = list(out.get("omitted") or [])
        omitted.append({
            "field": "contract_for_prompt",
            "reason": "de-identification could not be established",
        })
        out["omitted"] = omitted
    return out


def _sanitize_contract_for_prompt(obj):
    """Keep structure (names, reasoning) but drop sample/example literals."""
    drop = {"samples", "sample", "example_values", "values", "raw", "top_values"}
    if isinstance(obj, dict):
        return {
            str(k): _sanitize_contract_for_prompt(v)
            for k, v in obj.items()
            if str(k) not in drop
        }
    if isinstance(obj, list):
        return [_sanitize_contract_for_prompt(v) for v in obj]
    if isinstance(obj, str):
        from redibis.evidence.redact import redact_text, looks_like_residual_pii

        cleaned = redact_text(obj)
        if looks_like_residual_pii(cleaned):
            return ""
        return cleaned
    return obj


def apply_enrichment(
    active: dict,
    delta: dict,
    *,
    pii_demotions: Optional[list[str]] = None,
    pii_changes: Optional[list[dict]] = None,
) -> dict:
    """
    Apply an LLM enrichment delta to the active contract, producing a candidate.

    - business layer: wholesale-replace each column's `business` block
    - businessName: set when provided
    - PII columns: review/edit classification + entity type via privacy block
    - tags: union onto columns + table
    - classification "none" → queued in pii_demotions for merge-time overlay
    """
    from redibis.contracts.privacy import apply_privacy_to_column

    candidate = copy.deepcopy(active)
    columns_delta = delta.get("columns", {}) or {}
    table_tags = delta.get("table_tags", []) or []
    table_delta = delta.get("table") or {}

    for schema_obj in candidate.get("schema", []) or []:
        # table-level tags (union)
        if table_tags:
            existing = set(schema_obj.get("tags", []) or [])
            schema_obj["tags"] = sorted(existing | set(table_tags))
        # table-level description / purpose
        if isinstance(table_delta, dict):
            desc = (table_delta.get("description") or "").strip()
            purpose = (table_delta.get("purpose") or "").strip()
            if desc:
                schema_obj["description"] = desc
            if purpose and not desc:
                schema_obj["description"] = purpose
            if desc or purpose:
                top = candidate.get("description")
                if isinstance(top, dict):
                    if desc:
                        top["description"] = desc
                    if purpose:
                        top["purpose"] = purpose
                elif top is None or top == "":
                    # ODCS Description is an object, never a bare string.
                    block: dict[str, Any] = {}
                    if desc:
                        block["description"] = desc
                    if purpose:
                        block["purpose"] = purpose
                    candidate["description"] = block
                # If top-level is already a non-dict scalar, leave it alone and
                # rely on schema[0].description (authoritative for push/diff).
        for prop in schema_obj.get("properties", []) or []:
            col = prop.get("name")
            cd = columns_delta.get(col)
            if not cd:
                continue
            # Business layer — wholesale replace
            if "business" in cd and cd["business"] is not None:
                prop["business"] = cd["business"]
            if cd.get("businessName"):
                prop["businessName"] = cd["businessName"]
            # PII review/edit
            pii_edit = cd.get("pii")
            if pii_edit:
                classification = (pii_edit.get("classification") or "").strip().lower()
                if classification == "none":
                    if col:
                        if pii_demotions is not None:
                            pii_demotions.append(col)
                        if pii_changes is not None:
                            pii_changes.append({"column": col, "status": "not_pii"})
                elif classification:
                    payload: dict[str, Any] = {"classification": pii_edit["classification"]}
                    entity = pii_edit.get("entity_type")
                    if entity:
                        payload["pii"] = {"detected": True, "entity_type": entity}
                    apply_privacy_to_column(prop, payload)
                    # Union standard PII governance tags when confirming PII.
                    pii_tags = {"pii", "gdpr_personal_data"}
                    existing = set(prop.get("tags") or [])
                    prop["tags"] = sorted(existing | pii_tags)
                    if pii_changes is not None and col:
                        pii_changes.append({
                            "column": col,
                            "status": "pii",
                            "entity_type": entity,
                            "payload": payload,
                        })
            # Tags — union
            new_tags = cd.get("tags") or []
            biz_tags = (cd.get("business", {}) or {}).get("tags", []) if cd.get("business") else []
            all_new = set(new_tags) | set(biz_tags)
            if all_new:
                existing = set(prop.get("tags", []) or [])
                prop["tags"] = sorted(existing | all_new)
    return candidate


# Re-export for tests / CLI convenience
__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "EnrichmentContext",
    "EnrichmentResult",
    "EnrichmentService",
    "apply_enrichment",
    "enrichment_service_for_store",
    "get_provider",
    "redact_contract_for_external_llm",
]
