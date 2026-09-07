"""LangGraph (or sequential) multistep enrichment runner."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict

from redibis.config import RAIConfig, RedibisConfig
from redibis.enrich.delta_schema import (
    assert_odcs_v3_contract,
    cap_delta_changes,
    filter_delta_for_stage,
)
from redibis.enrich.providers import EnrichmentProvider
from redibis.enrich.service import EnrichmentResult, EnrichmentService, apply_enrichment
from redibis.enrich.workflow import (
    EnrichmentStep,
    EnrichmentWorkflow,
    default_workflow,
    load_workflow_file,
    stage_prompt_guidance,
)

log = logging.getLogger(__name__)


class MultistepState(TypedDict, total=False):
    table: str
    candidate: dict
    c_det: dict
    stage_index: int
    stages: list[dict]
    errors: list[str]
    attempt: int
    last_error: str
    done: bool


@dataclass
class StageAttemptRecord:
    step_id: str
    kind: str
    attempt: int
    ok: bool
    errors: list[str] = field(default_factory=list)
    delta: dict = field(default_factory=dict)
    pii_changes: list[dict] = field(default_factory=list)
    pii_reasons: dict[str, str] = field(default_factory=dict)
    system_prompt_hash: str = ""
    review_findings: list[dict] = field(default_factory=list)


def langgraph_available() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except ImportError:
        return False


def _table_summary_for_prompt(candidate: dict, table: str, *, full: bool = False) -> str:
    import yaml

    schema = (candidate.get("schema") or [{}])[0]
    rows = []
    for prop in schema.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        biz = prop.get("business") or {}
        privacy = ((prop.get("privacy") or {}).get("classification_engine") or {})
        row = {
            "name": prop.get("name"),
            "logicalType": prop.get("logicalType"),
            "businessName": prop.get("businessName"),
            "definition": biz.get("definition"),
            "classification": privacy.get("classification") or prop.get("classification"),
            "entity_type": privacy.get("entity_type"),
            "tags": prop.get("tags") or [],
        }
        if full:
            row["synonyms"] = biz.get("synonyms") or []
            row["example_values"] = biz.get("example_values") or []
            row["pii_reason"] = privacy.get("reason") or privacy.get("rationale")
        rows.append(row)
    payload = {
        "table": table,
        "physicalName": candidate.get("physicalName") or table,
        "existing_description": schema.get("description") or candidate.get("description"),
        "table_tags": schema.get("tags") or [],
        "column_definitions": rows,
        "column_count": len(rows),
        "pii_column_count": sum(
            1 for r in rows
            if str(r.get("classification") or "").startswith("pii")
        ),
    }
    if full:
        payload["purpose"] = schema.get("purpose") or candidate.get("purpose")
        payload["review_checks"] = [
            "missing or placeholder column definitions",
            "definitions that contradict the assigned PII classification",
            "table description inconsistent with the column set or grain",
            "tag vocabulary drift across columns",
            "leftover raw sample values in definitions or example_values",
        ]
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _repair_instructions(errors: list[str], stage_kind: str) -> str:
    joined = "\n".join(f"- {e}" for e in errors[:20])
    return (
        f"Your previous {stage_kind} enrichment output failed validation.\n"
        f"Fix these errors and return ONLY a corrected JSON delta for this stage:\n"
        f"{joined}\n"
        "Do not re-emit the full ODCS contract."
    )


def _stage_output_errors(kind: str, stage_delta: dict) -> list[str]:
    """Require in-scope output so empty/out-of-scope LLM replies retry."""
    if kind in ("validate", "contract_review"):
        return []
    if kind == "table_definition":
        table = stage_delta.get("table") or {}
        desc = str(table.get("description") or table.get("purpose") or "").strip()
        tags = stage_delta.get("table_tags") or []
        if not desc and not tags:
            return [
                "table_definition produced no table.description/purpose or table_tags",
            ]
        return []
    if kind in ("column_definitions", "classification_pii", "tags"):
        if not (stage_delta.get("columns") or {}):
            return [f"{kind} produced no in-scope column changes"]
    return []


def _merge_stage_delta(into: dict, delta: dict) -> dict:
    out = into if into is not None else {"columns": {}}
    if delta.get("table"):
        out["table"] = dict(delta["table"])
    if delta.get("table_tags") is not None:
        existing = list(out.get("table_tags") or [])
        out["table_tags"] = sorted(set(existing) | set(delta.get("table_tags") or []))
    cols = out.setdefault("columns", {})
    for name, cd in (delta.get("columns") or {}).items():
        if not isinstance(cd, dict):
            continue
        base = cols.setdefault(name, {})
        base.update(cd)
    return out


class MultistepEnrichmentRunner:
    """Execute a YAML workflow with retries and one final active write."""

    def __init__(
        self,
        service: EnrichmentService,
        provider: EnrichmentProvider,
        *,
        workflow: EnrichmentWorkflow,
        redibis_config: Optional[RedibisConfig] = None,
        enriched_by: str = "cli",
        bypass_rai: bool = False,
        external_masked_acknowledged: bool = False,
        run_writer: Any = None,
        run_id: Optional[str] = None,
        input_contract: Optional[dict] = None,
        enrichment_pack: Any = None,
        system_prompt: Optional[str] = None,
        extra_instructions: Optional[str] = None,
        example_contracts: Optional[list[str]] = None,
        approve_context_reduction: Optional[str] = None,
        reduction_approval_source: str = "cli_flag",
        reduction_approved_by: str = "",
        enforce_pack_reduction_approval: bool = True,
    ):
        self.service = service
        self.provider = provider
        self.workflow = workflow
        self.cfg = redibis_config or RedibisConfig()
        self.enriched_by = enriched_by
        self.bypass_rai = bypass_rai
        self.external_masked_acknowledged = external_masked_acknowledged
        self.run_writer = run_writer
        self.run_id = run_id
        self.input_contract = input_contract
        self.enrichment_pack = enrichment_pack
        self.system_prompt = system_prompt
        self.extra_instructions = extra_instructions
        self.example_contracts = example_contracts
        self.approve_context_reduction = approve_context_reduction
        self.reduction_approval_source = reduction_approval_source
        self.reduction_approved_by = reduction_approved_by
        self.enforce_pack_reduction_approval = enforce_pack_reduction_approval
        self.stage_records: list[StageAttemptRecord] = []
        self._last_state: Optional[MultistepState] = None
        self._last_stage_meta: dict = {}

    @classmethod
    def from_config(
        cls,
        service: EnrichmentService,
        provider: EnrichmentProvider,
        *,
        redibis_config: Optional[RedibisConfig] = None,
        steps_file: Optional[str] = None,
        **kwargs: Any,
    ) -> "MultistepEnrichmentRunner":
        cfg = redibis_config or RedibisConfig()
        default_attempts = int(getattr(cfg.enrich, "max_attempts", 3) or 3)
        if steps_file:
            workflow = load_workflow_file(steps_file, default_max_attempts=default_attempts)
        elif cfg.enrich.multistep.steps_file:
            workflow = load_workflow_file(
                cfg.enrich.multistep.steps_file,
                default_max_attempts=default_attempts,
            )
        else:
            workflow = default_workflow(max_attempts=default_attempts)
            if not bool(getattr(cfg.enrich.multistep, "review_enabled", True)):
                workflow.steps = [s for s in workflow.steps if s.kind != "contract_review"]
        if workflow.rai_enabled is None and cfg.enrich.multistep.rai_enabled is not None:
            workflow.rai_enabled = cfg.enrich.multistep.rai_enabled
        return cls(service, provider, workflow=workflow, redibis_config=cfg, **kwargs)

    def _rai_enabled(self) -> bool:
        if self.bypass_rai:
            return False
        if self.workflow.rai_enabled is not None:
            return bool(self.workflow.rai_enabled)
        return bool(self.cfg.rai.enabled)

    def run(self, table: str) -> EnrichmentResult:
        from redibis.telemetry.llm_evidence import llm_evidence_recorder

        local_dir = None
        if self.run_writer is not None:
            from redibis.store.storage_backend import LocalBackend
            backend = getattr(self.run_writer, "backend", None)
            if isinstance(backend, LocalBackend):
                try:
                    local_dir = backend._full_path(self.run_writer.bucket, self.run_writer.prefix)
                except Exception:
                    local_dir = None
        with llm_evidence_recorder(
            run_dir=local_dir,
            run_id=str(self.run_id or ""),
            table=table,
            execution_mode="agentic",
            run_writer=self.run_writer,
            config=getattr(self, "config", None) or getattr(self, "redibis_config", None),
        ):
            return self._run_inner(table)

    def _run_inner(self, table: str) -> EnrichmentResult:
        if langgraph_available():
            try:
                compiled = self._build_langgraph()
            except Exception as exc:
                # Nothing has executed yet (no LLM calls made) — safe to fall
                # back to the sequential runner.
                log.warning(
                    "LangGraph graph construction failed (%s); using sequential runner",
                    exc,
                )
            else:
                try:
                    return self._invoke_langgraph(compiled, table)
                except Exception as exc:
                    # Stages may have already run (and called the LLM) before
                    # the graph-level failure. Do NOT restart from scratch —
                    # that would duplicate completed LLM calls. Finalize with
                    # whatever partial progress we recorded instead.
                    log.warning(
                        "LangGraph invoke failed after %d stage attempt(s) (%s); "
                        "finalizing with partial progress instead of restarting",
                        len(self.stage_records), exc,
                    )
                    state = self._last_state or {}
                    seed = self._seed_candidate(table)
                    candidate = state.get("candidate") or seed
                    errors = list(state.get("errors") or []) + [
                        f"langgraph invocation error: {exc}",
                    ]
                    return self._finalize(table, candidate, errors)
        return self._run_sequential(table)

    def _build_langgraph(self) -> Any:
        from langgraph.graph import END, START, StateGraph

        enabled = self.workflow.enabled_steps()
        graph = StateGraph(MultistepState)
        for step in enabled:
            graph.add_node(step.id, self._langgraph_node(step))

        if enabled:
            graph.add_edge(START, enabled[0].id)
            for i, step in enumerate(enabled):
                next_id = enabled[i + 1].id if i + 1 < len(enabled) else None

                def _route(state: MultistepState, nxt: Optional[str] = next_id) -> str:
                    if state.get("done"):
                        return END
                    return nxt if nxt is not None else END

                graph.add_conditional_edges(step.id, _route)

        return graph.compile()

    def _langgraph_node(self, step: EnrichmentStep):
        def _node(state: MultistepState) -> MultistepState:
            # Defense in depth: skip if a prior stage already failed.
            if state.get("done"):
                self._last_state = state
                return state
            candidate, errors, ok = self._execute_step(
                table=state.get("table") or "",
                step=step,
                candidate=state.get("candidate") or state.get("c_det") or {},
                c_det=state.get("c_det") or {},
            )
            out = dict(state)
            out["candidate"] = candidate
            out["last_error"] = "; ".join(errors) if errors else ""
            stages = list(out.get("stages") or [])
            stages.append({
                "id": step.id,
                "kind": step.kind,
                "ok": ok,
                "errors": errors,
            })
            out["stages"] = stages
            if not ok:
                out["errors"] = list(out.get("errors") or []) + errors
                out["done"] = True
            self._last_state = out
            return out
        return _node

    def _invoke_langgraph(self, compiled: Any, table: str) -> EnrichmentResult:
        seed = self._seed_candidate(table)
        self._last_state = None
        final_state = compiled.invoke({
            "table": table,
            "c_det": seed,
            "candidate": copy.deepcopy(seed),
            "stages": [],
            "errors": [],
            "done": False,
        })
        self._last_state = final_state
        return self._finalize(table, final_state.get("candidate") or seed, final_state.get("errors") or [])

    def _run_sequential(self, table: str) -> EnrichmentResult:
        seed = self._seed_candidate(table)
        candidate = copy.deepcopy(seed)
        errors: list[str] = []
        for step in self.workflow.enabled_steps():
            candidate, step_errors, ok = self._execute_step(
                table=table,
                step=step,
                candidate=candidate,
                c_det=seed,
            )
            if not ok:
                errors.extend(step_errors)
                break
        return self._finalize(table, candidate, errors)

    def _seed_candidate(self, table: str) -> dict:
        if self.input_contract is not None:
            return copy.deepcopy(self.input_contract)
        active = self.service.store.get_active(table)
        if active is None:
            raise ValueError(f"No active contract for {table!r}")
        return copy.deepcopy(active)

    def _execute_step(
        self,
        *,
        table: str,
        step: EnrichmentStep,
        candidate: dict,
        c_det: dict,
    ) -> tuple[dict, list[str], bool]:
        if step.kind == "validate":
            errors = assert_odcs_v3_contract({
                k: v for k, v in candidate.items() if k != "enrichment_meta"
            })
            self.stage_records.append(StageAttemptRecord(
                step_id=step.id, kind=step.kind, attempt=1, ok=not errors, errors=errors,
            ))
            return candidate, errors, not errors

        max_attempts = step.max_attempts or self.workflow.max_attempts
        last_errors: list[str] = []
        working = copy.deepcopy(candidate)
        for attempt in range(1, max_attempts + 1):
            try:
                delta, errors = self._call_stage_llm(
                    table=table,
                    step=step,
                    candidate=working,
                    attempt=attempt,
                    prior_errors=last_errors,
                )
            except Exception as exc:
                errors = [str(exc)]
                delta = {}
            # Any LLM/parse/validation error is retry-worthy (even with a partial delta).
            if errors:
                last_errors = errors
                self.stage_records.append(StageAttemptRecord(
                    step_id=step.id, kind=step.kind, attempt=attempt,
                    ok=False, errors=errors, delta=delta or {},
                ))
                continue

            stage_delta = filter_delta_for_stage(delta, step.kind)
            review_findings = list(
                (self._last_stage_meta or {}).get("review_findings") or []
            )
            if step.kind == "contract_review":
                max_changes = int(getattr(self.cfg.enrich.multistep, "review_max_changes", 25) or 25)
                stage_delta, truncated = cap_delta_changes(stage_delta, max_changes)
                if truncated:
                    review_findings.append({
                        "severity": "warning",
                        "target": "table",
                        "message": (
                            f"review_max_changes={max_changes} truncated extra corrections"
                        ),
                    })
            output_errors = _stage_output_errors(step.kind, stage_delta)
            if output_errors:
                last_errors = output_errors
                self.stage_records.append(StageAttemptRecord(
                    step_id=step.id, kind=step.kind, attempt=attempt,
                    ok=False, errors=output_errors, delta=stage_delta,
                ))
                continue

            pii_demotions: list[str] = []
            pii_changes: list[dict] = []
            next_candidate = apply_enrichment(
                working, stage_delta,
                pii_demotions=pii_demotions, pii_changes=pii_changes,
            )
            from redibis.contracts.privacy import scrub_pii_enrichment_output
            scrub_pii_enrichment_output(next_candidate)
            next_candidate.pop("enrichment_meta", None)
            odcs_errors = assert_odcs_v3_contract(next_candidate)
            if odcs_errors:
                last_errors = odcs_errors
                self.stage_records.append(StageAttemptRecord(
                    step_id=step.id, kind=step.kind, attempt=attempt,
                    ok=False, errors=odcs_errors, delta=stage_delta,
                    pii_changes=pii_changes,
                ))
                continue

            stage_meta = self._last_stage_meta or {}
            self.stage_records.append(StageAttemptRecord(
                step_id=step.id, kind=step.kind, attempt=attempt,
                ok=True, errors=[], delta=stage_delta, pii_changes=pii_changes,
                pii_reasons=dict(stage_meta.get("pii_reasons") or {}),
                system_prompt_hash=str(stage_meta.get("system_prompt_hash") or ""),
                review_findings=review_findings if step.kind == "contract_review" else [],
            ))
            return next_candidate, [], True

        return working, last_errors or [f"step {step.id} failed after {max_attempts} attempts"], False

    def _call_stage_llm(
        self,
        *,
        table: str,
        step: EnrichmentStep,
        candidate: dict,
        attempt: int,
        prior_errors: list[str],
    ) -> tuple[dict, list[str]]:
        stage_extra = stage_prompt_guidance(step.kind)
        if step.instructions:
            stage_extra = f"{stage_extra}\n{step.instructions}".strip()
        if step.kind == "table_definition":
            stage_extra = (
                f"{stage_extra}\n\n# TABLE SUMMARY (for table-level definition)\n"
                f"{_table_summary_for_prompt(candidate, table)}"
            ).strip()
        if step.kind == "contract_review":
            stage_extra = (
                f"{stage_extra}\n\n# FULL CONTRACT SUMMARY (for whole-contract review)\n"
                f"{_table_summary_for_prompt(candidate, table, full=True)}"
            ).strip()
        if attempt > 1 and prior_errors:
            stage_extra = f"{stage_extra}\n{_repair_instructions(prior_errors, step.kind)}"

        extra = self.extra_instructions or ""
        if stage_extra:
            extra = f"{extra}\n{stage_extra}".strip()

        rai_cfg = self.cfg.rai
        if not self._rai_enabled():
            rai_cfg = RAIConfig(enabled=False)
            bypass = True
        else:
            bypass = self.bypass_rai

        # Intermediate stages never write active and never share run-folder keys
        # (finalize writes once via _apply_active_write).
        result = self.service.enrich(
            table,
            self.provider,
            system_prompt=self.system_prompt,
            extra_instructions=extra or None,
            example_contracts=self.example_contracts,
            enriched_by=self.enriched_by,
            redibis_config=self.cfg,
            rai_config=rai_cfg,
            external_masked_acknowledged=self.external_masked_acknowledged,
            bypass_rai=bypass,
            run_writer=None,
            run_id=self.run_id,
            input_contract=candidate,
            enrichment_pack=self.enrichment_pack,
            write_active=False,
            approve_context_reduction=self.approve_context_reduction,
            reduction_approval_source=self.reduction_approval_source,
            reduction_approved_by=self.reduction_approved_by,
            enforce_pack_reduction_approval=self.enforce_pack_reduction_approval,
            mode="multistep",
            stage_kind=step.kind,
            llm_attempt=attempt,
        )
        meta = result.enrichment_meta or {}
        self._last_stage_meta = meta
        delta = _infer_delta_from_contracts(candidate, result.candidate, step.kind)
        # Demotions are overlay-only and do not mutate column privacy on the
        # candidate, so reconstruct them from enrich meta.
        for col in meta.get("pii_demotions") or []:
            if col:
                delta.setdefault("columns", {}).setdefault(col, {})["pii"] = {
                    "classification": "none",
                }
        for ch in meta.get("pii_changes") or []:
            col = ch.get("column")
            if not col:
                continue
            cols = delta.setdefault("columns", {})
            entry = cols.setdefault(col, {})
            if ch.get("status") == "not_pii":
                entry["pii"] = {"classification": "none"}
            elif ch.get("status") == "pii" and "pii" not in entry:
                entry["pii"] = {
                    "classification": (ch.get("payload") or {}).get("classification")
                    or "pii",
                    "entity_type": ch.get("entity_type"),
                }

        # Intermediate enrich() applies the full (possibly multi-stage) LLM
        # payload to a throwaway candidate. ODCS errors from that unscoped apply
        # are not stage failures — we re-apply a filtered delta and ODCS-assert
        # in _execute_step. Keep delta-schema / RAI / empty-delta failures.
        errors: list[str] = list(meta.get("delta_validation_errors") or [])
        hard = [
            e for e in (result.errors or [])
            if any(tok in str(e).lower() for tok in ("rai", "blocked", "provider", "json", "parse"))
        ]
        errors.extend(hard)
        has_content = bool(
            (delta.get("columns") or {})
            or delta.get("table")
            or delta.get("table_tags")
        )
        if not has_content and not errors and step.kind != "contract_review":
            errors = list(result.errors or []) or [
                f"stage {step.id} returned empty delta",
            ]
        return delta, errors

    def _aggregated_delta_and_pii(self) -> tuple[dict, list[dict]]:
        agg: dict = {"columns": {}}
        pii_changes: list[dict] = []
        for rec in self.stage_records:
            if not rec.ok:
                continue
            _merge_stage_delta(agg, rec.delta or {})
            pii_changes.extend(list(rec.pii_changes or []))
        return agg, pii_changes

    def _aggregated_pii_reasons(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rec in self.stage_records:
            if rec.ok and rec.pii_reasons:
                out.update(rec.pii_reasons)
        return out

    def _write_prerun_artifacts(self, table: str, c_det: dict, effective_run_id: str) -> dict[str, str]:
        """Write once-per-run artifacts that single-shot enrich() writes up front
        (deterministic snapshot + consolidated prompt context), since intermediate
        multistep stages pass run_writer=None to avoid clobbering each other."""
        if self.run_writer is None:
            return {}
        run_artifacts: dict[str, str] = {}
        try:
            from redibis.contracts.lifecycle import write_deterministic_snapshot

            run_artifacts.update(
                write_deterministic_snapshot(
                    self.run_writer, c_det, run_id=effective_run_id, version=c_det.get("version"),
                )
            )
            prompt_payload = {
                "table": table,
                "run_id": effective_run_id,
                "provider": self.provider.name or self.provider.model or "unknown",
                "model": self.provider.model,
                "multistep": True,
                "workflow": self.workflow.to_dict(),
                "stages": [
                    {
                        "step_id": r.step_id,
                        "kind": r.kind,
                        "attempt": r.attempt,
                        "ok": r.ok,
                        "system_prompt_hash": r.system_prompt_hash,
                    }
                    for r in self.stage_records
                ],
            }
            key = self.run_writer.write("llm_prompt_context.json", prompt_payload)
            run_artifacts["llm_prompt_context.json"] = key
        except Exception as exc:
            log.warning(
                "Failed to persist multistep prompt artifacts table=%r run_id=%r: %s",
                table, effective_run_id, exc,
            )
        return run_artifacts

    def _finalize(self, table: str, candidate: dict, errors: list[str]) -> EnrichmentResult:
        from redibis.contracts.privacy import scrub_pii_enrichment_output
        from redibis.enrich.diff import build_enrichment_diff

        candidate = copy.deepcopy(candidate)
        candidate.pop("enrichment_meta", None)
        odcs_errors = assert_odcs_v3_contract(candidate)
        all_errors = list(errors) + odcs_errors
        valid = not all_errors
        c_det = self._seed_candidate(table)
        scrub_pii_enrichment_output(candidate)
        effective_run_id = self.run_id or "enrich-multistep"
        last_stage_hash = next(
            (r.system_prompt_hash for r in reversed(self.stage_records) if r.system_prompt_hash),
            "",
        )
        meta = {
            "multistep": True,
            "workflow": self.workflow.to_dict(),
            "provider": self.provider.name,
            "model": self.provider.model,
            "system_prompt_hash": last_stage_hash,
            "stages": [
                {
                    "step_id": r.step_id,
                    "kind": r.kind,
                    "attempt": r.attempt,
                    "ok": r.ok,
                    "errors": r.errors,
                    **({"review_findings": r.review_findings} if r.review_findings else {}),
                }
                for r in self.stage_records
            ],
            "rai_enabled": self._rai_enabled(),
            "max_attempts": self.workflow.max_attempts,
            "enriched_by": self.enriched_by,
            "review_findings": [
                f for r in self.stage_records if r.ok for f in (r.review_findings or [])
            ],
        }
        auto_written = False
        version_after = ""
        enrichment_status = "invalid" if all_errors else "clean"
        diff_report = build_enrichment_diff(c_det, candidate)
        run_artifacts = self._write_prerun_artifacts(table, c_det, effective_run_id)

        sc_enabled = bool(self.cfg.enrich.similar_context.enabled)
        sc_k = int(self.cfg.enrich.similar_context.k)

        if valid:
            agg_delta, agg_pii = self._aggregated_delta_and_pii()
            pii_reasons = self._aggregated_pii_reasons()
            result_dict = {
                "valid": True,
                "errors": [],
                "warnings": [],
            }
            auto_written, version_after, artifact_errors, enrichment_status, write_artifacts = (
                self.service._apply_active_write(
                    table=table,
                    candidate=candidate,
                    c_det=c_det,
                    delta=agg_delta,
                    pii_changes=agg_pii,
                    pii_reasons=pii_reasons,
                    enriched_by=self.enriched_by,
                    effective_run_id=effective_run_id,
                    meta=meta,
                    sc_enabled=sc_enabled,
                    sc_k=sc_k,
                    context_bundle=None,
                    run_writer=self.run_writer,
                    result=result_dict,
                    delta_errors=[],
                    scrubbed_cols=[],
                    provider=self.provider,
                    system_prompt=self.system_prompt or "",
                    user_prompt="",
                    raw="",
                    diff_report=diff_report,
                )
            )
            run_artifacts.update(write_artifacts)
            if artifact_errors:
                all_errors.extend(artifact_errors)

        validation = self.service.store.validate(candidate, strict=False)
        candidate["enrichment_meta"] = meta
        return EnrichmentResult(
            table=table,
            candidate=candidate,
            valid=bool(validation.get("valid")) and valid,
            errors=all_errors or list(validation.get("errors") or []),
            warnings=list(validation.get("warnings") or []),
            enrichment_meta=meta,
            diff_report=diff_report,
            auto_written=auto_written,
            version_after=version_after,
            run_artifacts=run_artifacts,
            enrichment_status=enrichment_status,
            deferred_context={},
        )


def _infer_delta_from_contracts(before: dict, after: dict, stage_kind: str) -> dict:
    """Best-effort delta reconstruction for stage filtering after write_active=False enrich."""
    delta: dict[str, Any] = {"columns": {}}
    before_schema = (before.get("schema") or [{}])[0]
    after_schema = (after.get("schema") or [{}])[0]
    if stage_kind == "table_definition":
        desc = after_schema.get("description")
        if desc and desc != before_schema.get("description"):
            delta["table"] = {"description": desc}
        before_tags = set(before_schema.get("tags") or [])
        after_tags = set(after_schema.get("tags") or [])
        added = sorted(after_tags - before_tags)
        if added:
            delta["table_tags"] = added
        return filter_delta_for_stage(delta, stage_kind)

    before_cols = {
        p.get("name"): p for p in (before_schema.get("properties") or [])
        if isinstance(p, dict) and p.get("name")
    }
    for prop in after_schema.get("properties") or []:
        if not isinstance(prop, dict) or not prop.get("name"):
            continue
        name = prop["name"]
        prev = before_cols.get(name) or {}
        cd: dict[str, Any] = {}
        if prop.get("business") != prev.get("business"):
            cd["business"] = prop.get("business")
        if prop.get("businessName") != prev.get("businessName"):
            cd["businessName"] = prop.get("businessName")
        if prop.get("privacy") != prev.get("privacy"):
            engine = ((prop.get("privacy") or {}).get("classification_engine") or {})
            if engine:
                cd["pii"] = {
                    "classification": engine.get("classification"),
                    "entity_type": engine.get("entity_type"),
                }
        if set(prop.get("tags") or []) != set(prev.get("tags") or []):
            cd["tags"] = prop.get("tags") or []
        if cd:
            delta["columns"][name] = cd
    if stage_kind == "contract_review":
        desc = after_schema.get("description")
        if desc and desc != before_schema.get("description"):
            delta["table"] = {"description": desc}
        before_tags = set(before_schema.get("tags") or [])
        after_tags = set(after_schema.get("tags") or [])
        added = sorted(after_tags - before_tags)
        if added:
            delta["table_tags"] = added
    return filter_delta_for_stage(delta, stage_kind)
