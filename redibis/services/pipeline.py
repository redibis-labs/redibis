"""
redibis.services.pipeline
==========================
Shared, presentation-agnostic scan pipeline steps.

This module is the SINGLE source of truth for the core scan steps —
profiling, quality validation, PII detection, and classification. Both callers use it:

  - ``ScanService``         (stateless: CSV/bytes/DataFrame -> ScanResult -> S3)
  - ``session_service``     (stateful: ScanSession -> RunRecord -> disk)

Keeping the steps here means the two entry points can never drift apart (they
previously each re-implemented the pipeline, and one had dead code referencing
fields that no longer exist).

No REST / CLI / session knowledge lives here.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Any, Sequence, TYPE_CHECKING

import pandas as pd

from redibis.models import PIIDetection
from redibis.profiling.base import ProfileResult
from redibis.config import ProfilingConfig
from redibis.profiling import get_profiler
from redibis.quality.gatekeeper import QualityGatekeeper
from redibis.quality.rule_set import QualityRuleSet
from redibis.config import NERConfig
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.ner_backend import NERBackend
from redibis.pii.ner_registry import NERModelRegistry
from redibis.pii.thresholds import Thresholds
from redibis.pii.regex_overrides import RegexOverrides

if TYPE_CHECKING:
    from redibis.services.catalog.assertions import CoverageRecord
    from redibis.store.run_output_writer import RunOutputWriter

log = logging.getLogger(__name__)

#: Per-run artifact name for scan coverage (written via ``RunOutputWriter``).
SCAN_COVERAGE_ARTIFACT = "scan_coverage.json"

_PII_TAG_FACETS = ("pii_tag", "entity_tag", "policy_tag")
_PROFILE_COL_FACETS = ("column_description", "column_display_name")
_SKIP_DECISION_PATHS = frozenset({"skipped_by_triage", "empty_column"})


def split_table(table: str) -> tuple[str, str]:
    """Split a ``db.table`` (or bare ``table``) into ``(database, table)``."""
    parts = table.split(".")
    return ("default", parts[0]) if len(parts) == 1 else (parts[0], parts[1])


def select_columns(df: pd.DataFrame, columns: Optional[List[str]]) -> pd.DataFrame:
    """Restrict ``df`` to the requested columns (ignoring any that don't exist)."""
    if not columns:
        return df
    valid = [c for c in columns if c in df.columns]
    return df[valid] if valid else df


# ── Step 1: profiling ─────────────────────────────────────────────────────────

def profile_dataframe(
    df: pd.DataFrame,
    dataset_name: str,
    triage_threshold: float = 0.0,
    *,
    profiling: Optional[ProfilingConfig] = None,
    source: Optional["SourceConfig"] = None,
    table: Optional[str] = None,
    domain: str = "",
    spark: Any = None,
    connection: Any = None,
    relationships: bool = False,
    relationship_engine: str = "auto",
    relationship_opts: Optional[dict[str, Any]] = None,
) -> ProfileResult:
    """Run profiling via the configured engine and return a ``ProfileResult``."""
    from redibis.config import ProfilingConfig, SourceConfig
    from redibis.profiling.metadata_tiers import enrich_profile_with_metadata
    from redibis.profiling.relationships import (
        apply_relationship_flags,
        profile_relationships,
        resolve_relationship_data,
    )

    cfg = profiling or ProfilingConfig(triage_threshold=triage_threshold)
    profiler = get_profiler(cfg)
    result = profiler.profile(df, dataset_name=dataset_name)

    if cfg.metadata.enabled and source is not None:
        enrich_profile_with_metadata(
            result,
            df,
            table=table or dataset_name,
            profiling=cfg,
            source=source,
            domain=domain,
            spark=spark,
            connection=connection,
        )

    if relationships:
        rel_table = table or dataset_name
        data, backend = resolve_relationship_data(
            df,
            engine=relationship_engine,
            source=source,
            table=rel_table,
            spark=spark,
        )
        rel = profile_relationships(data, engine=backend, **(relationship_opts or {}))
        rel["backend"] = backend
        apply_relationship_flags(result, rel)

    from redibis.profiling.fingerprint import compute_table_fingerprints
    pii_cols: set[str] = set()
    for p in result.column_profiles:
        if getattr(p, "send_to_detector", False):
            pii_cols.add(p.column)
    result.structural_fingerprints = compute_table_fingerprints(
        df,
        pii_columns=pii_cols,
        arabic_columns=result.arabic_columns,
    )

    return result


# ── Step 2: quality ────────────────────────────────────────────────────────────

def apply_quality_rules(
    qa: QualityGatekeeper,
    rule_set: Optional[QualityRuleSet],
    profiler_expectations,
) -> None:
    """
    Feed expectations into the gatekeeper.

    If the user supplied an editable ``QualityRuleSet`` (from discovery /
    settings) it wins; otherwise we fall back to the profiler's auto-generated
    expectations. The ``column`` field of each rule is merged into kwargs.
    """
    if rule_set and rule_set.rules:
        for r in rule_set.rules:
            kwargs = dict(r.get("kwargs", {}))
            col = r.get("column")
            if col and "column" not in kwargs:
                kwargs["column"] = col
            meta = r.get("meta")
            if meta:
                kwargs["meta"] = meta
            qa.add_gx_expectation(r["rule"], **kwargs)
    else:
        qa.merge_expectations(profiler_expectations)


# ── Step 3: PII detection + equation ───────────────────────────────────────────

def thresholds_from(
    regex_confidence: Optional[float],
    gliner_confidence: Optional[float],
    llm_confidence: Optional[float],
) -> Thresholds:
    """Build a Thresholds object from per-engine confidence floors."""
    return Thresholds(
        presidio_min=regex_confidence or 0.80,
        gliner_min=gliner_confidence or 0.70,
        llm_min=llm_confidence or 0.82,
    )


def run_pii_detection(
    df: pd.DataFrame,
    *,
    columns: Optional[List[str]] = None,
    engines: str = "both",
    gliner_always_run: bool = False,
    ner_always_run: bool = False,
    regex_overrides: Optional[RegexOverrides] = None,
    ner_config: Optional[NERConfig] = None,
    gliner_config=None,
    ner_backend: Optional[NERBackend] = None,
    equation_mode: str = "independent",
    thresholds: Optional[Thresholds] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    table: str = "",
    pii_config=None,
    observability_config=None,
    edge_rules_enabled: Optional[bool] = None,
    policy_pack: Optional[str] = None,
    edge_rules_overlay: Optional[list] = None,
) -> List[PIIDetection]:
    """
    Detect PII (evidence) then apply the equation (verdict).

    This is the one and only PII path. The detector produces evidence
    (``detected=False``); ``decide_pii`` sets the final verdict — preserving the
    detector/equation separation invariant.
    """
    thresholds = thresholds or Thresholds()
    always_run = ner_always_run or gliner_always_run
    from redibis.config import resolve_pii_config
    from dataclasses import replace as _dc_replace

    try:
        from redibis.classification.evidence import clear_current_pack_stack

        clear_current_pack_stack()
    except Exception:
        pass

    pii_config = resolve_pii_config(pii_config)
    # Sync learned consume knobs onto thresholds (disabled by default).
    learned_cfg = getattr(pii_config, "learned", None)
    if learned_cfg is not None:
        thresholds = _dc_replace(
            thresholds,
            learned_min=float(getattr(learned_cfg, "min_score", None) or thresholds.learned_min),
            learned_enabled=bool(getattr(learned_cfg, "enabled", False)),
            learned_promoted=bool(getattr(learned_cfg, "promoted", False)),
        )
    if ner_backend is None and ner_config is not None:
        ner_backend = NERModelRegistry.try_load(ner=ner_config, gliner=gliner_config)
    if ner_backend is None and ner_config is not None:
        from redibis.pii.detector import _engines_run_ner

        if _engines_run_ner(engines) and not NERModelRegistry.resolve_model_path(
            ner_config, gliner_config
        ):
            msg = (
                f"NER requested (pii_engines={engines!r}) but no model is configured — "
                "running regex-only. Activate a model in Settings → NER Models, set "
                "pii.ner.model_path, or export REDIBIS_NER_MODEL=/models/…"
            )
            log.warning(msg)
            if progress_callback:
                progress_callback("⚠ " + msg)
    detections = detect_pii(
        df=df,
        columns=columns if columns else df.columns.tolist(),
        engines=engines,
        gliner_always_run=always_run,
        ner_always_run=always_run,
        regex_overrides=regex_overrides,
        ner_backend=ner_backend,
        progress_callback=progress_callback,
        table=table,
        log_samples=bool(
            observability_config.log_samples if observability_config else False
        ),
        log_regex_hits=bool(
            pii_config.logging.log_regex_hits if pii_config else True
        ),
        max_regex_hits_logged=int(
            pii_config.logging.max_regex_hits_logged if pii_config else 20
        ),
        pii_config=pii_config,
    )

    # Optional learned classifier evidence (never sets detected — equation decides).
    if learned_cfg is not None and bool(getattr(learned_cfg, "enabled", False)):
        try:
            from redibis.training.registry_hook import (
                apply_learned_to_detections,
                learned_backend_from_config,
            )

            backend = learned_backend_from_config(pii_config)
            detections = apply_learned_to_detections(
                detections, backend=backend, table=table,
            )
        except Exception as exc:
            log.warning("Learned classifier skipped: %s", exc)
            if progress_callback:
                progress_callback(f"⚠ Learned classifier skipped: {exc}")

    from redibis.obs import bind_context

    # Behavior config (default disabled — byte-identical to prior path).
    behavior_cfg = None
    behavior_mode = None
    try:
        from redibis.behavior.config import BehaviorConfig
        from redibis.behavior.models import FailMode, RuntimeMode
        from redibis.config import RedibisConfig

        behavior_cfg = getattr(RedibisConfig.load(), "behavior", None) or BehaviorConfig()
        behavior_mode = behavior_cfg.effective_mode("pii")
    except Exception:
        behavior_cfg = None
        behavior_mode = None
        FailMode = None  # type: ignore[misc, assignment]

    pack_name = policy_pack
    if pack_name is None:
        try:
            from redibis.config import RedibisConfig

            pack_name = RedibisConfig.load().classification.policy_pack
        except Exception:
            pack_name = "telecom"

    # Optional pre_verdict threshold effects (FN tuning) before equation.
    run_thresholds = thresholds
    baseline_thresholds = thresholds
    if (
        behavior_cfg is not None
        and behavior_mode is not None
        and behavior_mode.value != "disabled"
    ):
        try:
            from redibis.behavior.models import HookStage
            from redibis.behavior.runtime import (
                apply_pre_verdict_thresholds,
                resolve_policies_for_stage,
            )

            pre_policies = resolve_policies_for_stage(
                behavior_cfg,
                engine="pii",
                stage=HookStage.PRE_VERDICT,
                policy_pack=pack_name or "telecom",
                edge_rules_overlay=None,
            )
            if pre_policies:
                thr_result = apply_pre_verdict_thresholds(
                    detections,
                    thresholds,
                    policies=pre_policies,
                    df=df,
                    table=table,
                    behavior_config=behavior_cfg,
                    progress_callback=progress_callback,
                )
                run_thresholds = thr_result.thresholds
                baseline_thresholds = thr_result.baseline_thresholds or thresholds
                for warn in thr_result.warnings:
                    log.warning(
                        "behavior_policy_warning column=%s fallback=%s error=%s",
                        warn.target,
                        warn.fallback,
                        warn.error,
                    )
        except Exception as exc:
            log.warning("Behavior pre_verdict skipped: %s", exc)
            if progress_callback:
                progress_callback(f"⚠ Behavior pre_verdict skipped: {exc}")
            if (
                FailMode is not None
                and behavior_cfg is not None
                and behavior_cfg.effective_fail_mode() is FailMode.FAIL_RUN
            ):
                raise

    results: List[PIIDetection] = []
    for d in detections:
        with bind_context(column=d.column, fn="pii.equations.decide_pii"):
            results.append(decide_pii(d, equation_mode, run_thresholds))

    # Mark FN→TP promotions caused by active pre_verdict threshold changes.
    if (
        behavior_cfg is not None
        and behavior_mode is not None
        and behavior_mode.value == "active"
        and run_thresholds is not baseline_thresholds
    ):
        try:
            from redibis.behavior.adapters.pii import mark_threshold_promotions

            baseline_results = [
                decide_pii(d, equation_mode, baseline_thresholds) for d in detections
            ]
            results = mark_threshold_promotions(baseline_results, results)
        except Exception as exc:
            log.warning("Behavior promotion marking skipped: %s", exc)

    apply_edge = edge_rules_enabled
    if apply_edge is None:
        try:
            from redibis.config import RedibisConfig

            apply_edge = bool(RedibisConfig.load().classification.edge_rules_enabled)
        except Exception:
            apply_edge = True

    refined: List[PIIDetection] = list(results)
    if apply_edge:
        from redibis.classification.edge_rules import (
            apply_edge_rules_to_detection,
            merge_policy_edge_rules,
        )
        from redibis.classification.pack_store import load_pack
        from redibis.config import ConfigError

        policy = load_pack(pack_name or "telecom")
        if edge_rules_overlay:
            # Enforce per_run_overlay_allowed before applying overlay
            overlay_allowed: bool = True
            try:
                from redibis.config import RedibisConfig

                overlay_allowed = bool(
                    RedibisConfig.load().classification.per_run_overlay_allowed
                )
            except Exception:
                overlay_allowed = True
            if not overlay_allowed:
                raise ConfigError(
                    "edge_rules_overlay provided but classification.per_run_overlay_allowed "
                    "is False — per-run edge-rule overlays are disabled in configuration"
                )
            policy = merge_policy_edge_rules(policy, edge_rules_overlay)
        try:
            from redibis.classification.evidence import resolve_and_record_pack_stack

            resolve_and_record_pack_stack(policy, pack_name=pack_name or "telecom")
        except Exception:
            pass
        refined = []
        for d in results:
            with bind_context(column=d.column, fn="classification.edge_rules"):
                updated, _ = apply_edge_rules_to_detection(d, policy, df=df)
                refined.append(updated)

    # Behavior Policy Runtime (default disabled). Shadow compares without
    # applying; active replaces the edge-rule result with the behavior patch.
    # Evaluation always uses equation (pre-edge) outcomes as input.
    if behavior_cfg is not None and behavior_mode is not None and behavior_mode.value != "disabled":
        try:
            from redibis.behavior.runtime import apply_pii_behavior_policies

            applied = apply_pii_behavior_policies(
                results,
                df=df,
                table=table,
                policy_pack=pack_name or "telecom",
                edge_rules_overlay=edge_rules_overlay if apply_edge else None,
                behavior_config=behavior_cfg,
                progress_callback=progress_callback,
                baseline=refined if apply_edge else results,
            )
            for warn in applied.warnings:
                log.warning(
                    "behavior_policy_warning column=%s fallback=%s error=%s",
                    warn.target,
                    warn.fallback,
                    warn.error,
                )
                if progress_callback:
                    progress_callback(
                        f"⚠ Behavior [{warn.fallback}] {warn.target}: {warn.error}"
                    )
            if behavior_mode.value == "active":
                return applied.detections
            # shadow: keep edge-rule (or equation) outcomes; warnings already surfaced
            return refined if apply_edge else results
        except Exception as exc:
            log.warning("Behavior policy runtime skipped: %s", exc)
            if progress_callback:
                progress_callback(f"⚠ Behavior policy runtime skipped: {exc}")
            if (
                FailMode is not None
                and behavior_cfg is not None
                and behavior_cfg.effective_fail_mode() is FailMode.FAIL_RUN
            ):
                raise

    return refined if apply_edge else results


def run_pii_detection_with_context(
    df: pd.DataFrame,
    *,
    run_id: str = "",
    table: str = "",
    columns: Optional[List[str]] = None,
    engines: str = "both",
    gliner_always_run: bool = False,
    ner_always_run: bool = False,
    regex_overrides: Optional[RegexOverrides] = None,
    ner_config: Optional[NERConfig] = None,
    gliner_config=None,
    ner_backend: Optional[NERBackend] = None,
    equation_mode: str = "independent",
    thresholds: Optional[Thresholds] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    pii_config=None,
    observability_config=None,
    edge_rules_enabled: Optional[bool] = None,
    policy_pack: Optional[str] = None,
    edge_rules_overlay: Optional[list] = None,
) -> List[PIIDetection]:
    """``run_pii_detection`` with observability context binding."""
    from redibis.obs import bind_context

    with bind_context(run_id=run_id, table=table, fn="pipeline.run_pii_detection"):
        return run_pii_detection(
            df,
            columns=columns,
            engines=engines,
            gliner_always_run=gliner_always_run,
            ner_always_run=ner_always_run,
            regex_overrides=regex_overrides,
            ner_config=ner_config,
            gliner_config=gliner_config,
            ner_backend=ner_backend,
            equation_mode=equation_mode,
            thresholds=thresholds,
            progress_callback=progress_callback,
            table=table,
            pii_config=pii_config,
            observability_config=observability_config,
            edge_rules_enabled=edge_rules_enabled,
            policy_pack=policy_pack,
            edge_rules_overlay=edge_rules_overlay,
        )


# ── Step 4: classification ─────────────────────────────────────────────────────

def contract_for_classification(
    *,
    pii_partial: Optional[dict[str, Any]],
    quality_partial: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Prefer the PII partial (has privacy signals); fall back to quality partial."""
    if pii_partial:
        return pii_partial
    if quality_partial:
        return quality_partial
    return None


def classification_result_rows(results: list[Any]) -> list[dict[str, Any]]:
    """Serialize ``ClassificationResult`` rows for scan / session consumers."""
    return [
        {
            "column": r.column,
            "tags": r.tag_keys(),
            "approval_role": r.approval_role,
            "escalations": list(r.escalations),
            "violations": list(r.violations),
        }
        for r in results
    ]


def run_classification(
    contract: dict[str, Any],
    table: str,
    *,
    config: "RedibisConfig",
    classification_service: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """
    Classify contract columns via the deterministic policy engine.

    No-op when ``classification.enabled`` is false. Shared by ``Scan``,
    session steps, and the agentic tool runner.
    """
    from redibis.config import RedibisConfig
    from redibis.classification import ClassificationService, JurisdictionContext, get_builtin_pack

    if not isinstance(config, RedibisConfig):
        config = RedibisConfig.default()
    if not config.classification.enabled:
        return []

    pack_name = config.classification.policy_pack or "telecom"
    svc = classification_service or ClassificationService(get_builtin_pack(pack_name))
    jurisdiction = (config.classification.default_jurisdiction or "").strip()
    if jurisdiction:
        svc.set_jurisdiction(JurisdictionContext(table=table, jurisdiction=jurisdiction))

    results = svc.classify_contract(
        contract,
        table,
        use_memory=config.memory.enabled,
    )
    return classification_result_rows(results)


# ── Scan coverage artifact (catalog deletion gate) ─────────────────────────────

def build_scan_coverage(
    *,
    scan_id: str,
    columns: Sequence[str],
    asset_fqn: str = "",
    profile: Optional[ProfileResult] = None,
    pii_detections: Optional[Sequence[PIIDetection]] = None,
    run_profile: bool = False,
    run_pii: bool = False,
    run_quality: bool = False,
    pii_column_filter: Optional[Sequence[str]] = None,
    profile_error: Optional[str] = None,
    pii_error: Optional[str] = None,
    quality_error: Optional[str] = None,
    quality_results: Optional[Sequence[Any]] = None,
    empty_sample: bool = False,
) -> list["CoverageRecord"]:
    """Build one ``CoverageRecord`` per ``(column, facet)`` the scan attempted.

    Status rules:
      - ``EVALUATED`` on success
      - ``SKIPPED`` when triage/filter/empty-sample excluded the column
      - ``ERROR`` when the profiler or detector failed
    """
    from redibis.services.catalog.assertions import (
        CoverageRecord,
        CoverageStatus,
        Facet,
    )

    records: list[CoverageRecord] = []

    def _rec(
        column_path: str,
        facet: Facet,
        status: CoverageStatus,
        reason: str = "",
    ) -> None:
        records.append(
            CoverageRecord(
                scan_id=scan_id,
                asset_fqn=asset_fqn,
                column_path=column_path,
                facet=facet,
                status=status,
                reason=reason,
            )
        )

    cols = [str(c) for c in columns]
    skip_all_reason = "empty sample" if empty_sample else ""

    if run_profile:
        if profile_error:
            _rec("", Facet.TABLE_DESCRIPTION, CoverageStatus.ERROR, profile_error)
            for col in cols:
                for fname in _PROFILE_COL_FACETS:
                    _rec(col, Facet(fname), CoverageStatus.ERROR, profile_error)
        elif empty_sample:
            _rec("", Facet.TABLE_DESCRIPTION, CoverageStatus.SKIPPED, skip_all_reason)
            for col in cols:
                for fname in _PROFILE_COL_FACETS:
                    _rec(col, Facet(fname), CoverageStatus.SKIPPED, skip_all_reason)
        else:
            _rec("", Facet.TABLE_DESCRIPTION, CoverageStatus.EVALUATED)
            for col in cols:
                for fname in _PROFILE_COL_FACETS:
                    _rec(col, Facet(fname), CoverageStatus.EVALUATED)

    if run_pii:
        filter_set = (
            {str(c) for c in pii_column_filter} if pii_column_filter is not None else None
        )
        by_col = {d.column: d for d in (pii_detections or [])}
        for col in cols:
            if pii_error:
                status, reason = CoverageStatus.ERROR, pii_error
            elif empty_sample:
                status, reason = CoverageStatus.SKIPPED, skip_all_reason
            elif filter_set is not None and col not in filter_set:
                status, reason = CoverageStatus.SKIPPED, "column excluded"
            elif col not in by_col:
                status, reason = CoverageStatus.SKIPPED, "column not scanned"
            else:
                path = (by_col[col].decision_path or "").strip()
                if path in _SKIP_DECISION_PATHS:
                    status = CoverageStatus.SKIPPED
                    reason = (
                        "skipped by triage"
                        if path == "skipped_by_triage"
                        else "empty column"
                    )
                else:
                    status, reason = CoverageStatus.EVALUATED, ""
            for fname in _PII_TAG_FACETS:
                _rec(col, Facet(fname), status, reason)

    if run_quality:
        q_error = quality_error
        q_results = list(quality_results or [])
        if q_error:
            _rec("", Facet.QUALITY_TEST, CoverageStatus.ERROR, q_error)
            for col in cols:
                _rec(col, Facet.QUALITY_TEST, CoverageStatus.ERROR, q_error)
        elif empty_sample:
            _rec("", Facet.QUALITY_TEST, CoverageStatus.SKIPPED, skip_all_reason)
            for col in cols:
                _rec(col, Facet.QUALITY_TEST, CoverageStatus.SKIPPED, skip_all_reason)
        elif not q_results:
            _rec("", Facet.QUALITY_TEST, CoverageStatus.SKIPPED, "no quality rules")
        else:
            for row in q_results:
                col_path = ""
                col = getattr(row, "column", None) if not isinstance(row, dict) else row.get("column")
                if col:
                    col_path = str(col)
                ok = getattr(row, "success", None) if not isinstance(row, dict) else row.get("success")
                reason = "" if ok else "quality check failed"
                status = CoverageStatus.EVALUATED if ok else CoverageStatus.ERROR
                _rec(col_path, Facet.QUALITY_TEST, status, reason)

    return records


def scan_coverage_payload(
    records: Sequence["CoverageRecord"],
    *,
    table: str,
    scan_id: str,
) -> dict[str, Any]:
    """JSON-serializable payload for ``scan_coverage.json``."""
    from redibis.services.catalog.assertions import coverage_record_to_dict

    return {
        "scan_id": scan_id,
        "table": table,
        "records": [coverage_record_to_dict(r) for r in records],
    }


def write_scan_coverage(
    run_writer: "RunOutputWriter",
    records: Sequence["CoverageRecord"],
    *,
    table: str,
    scan_id: str,
) -> str:
    """Persist scan coverage through ``RunOutputWriter`` (invariant 7)."""
    return run_writer.write(
        SCAN_COVERAGE_ARTIFACT,
        scan_coverage_payload(records, table=table, scan_id=scan_id),
    )


def parse_scan_coverage_payload(payload: Any) -> list["CoverageRecord"]:
    """Deserialize a ``scan_coverage.json`` body into ``CoverageRecord`` list."""
    from redibis.services.catalog.assertions import (
        CoverageRecord,
        coverage_record_from_dict,
    )

    if not isinstance(payload, dict):
        return []
    raw = payload.get("records")
    if not isinstance(raw, list):
        return []
    out: list[CoverageRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(coverage_record_from_dict(item))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def load_latest_scan_coverage(
    backend: Any,
    bucket: str,
    table: str,
    *,
    workflow: str = "scan",
) -> Optional[list["CoverageRecord"]]:
    """Load ``scan_coverage.json`` from the newest run under ``{workflow}/{table}/``."""
    table_safe = table.replace(".", "_")
    prefix = f"{workflow}/{table_safe}/"
    run_prefixes: set[str] = set()
    try:
        keys = backend.list_keys(bucket, prefix=prefix)
    except Exception:  # noqa: BLE001
        return None
    for key in keys:
        parts = key[len(prefix):].split("/")
        if parts and parts[0]:
            run_prefixes.add(parts[0])
    if not run_prefixes:
        return None

    latest_run = sorted(run_prefixes)[-1]
    key = f"{prefix}{latest_run}/{SCAN_COVERAGE_ARTIFACT}"
    try:
        if not backend.exists(bucket, key):
            return None
        payload = backend.get_json(bucket, key)
    except Exception:  # noqa: BLE001
        return None
    records = parse_scan_coverage_payload(payload)
    return records or None
