"""GlobalConfig and RedibisConfig adapters."""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from redibis.pii.regex_overrides import RegexSet
from redibis.quality.rule_set import QualityRuleSet
from redibis.services.scan_service import ScanConfig
from redibis.store.subcontract_store import SubcontractStore
from redibis.store.storage_backend import StorageBackend

log = logging.getLogger(__name__)

def build_subcontract_store(backend: StorageBackend) -> SubcontractStore:
    """Build a SubcontractStore for the two v2 run buckets (env-overridable)."""
    import os
    return SubcontractStore(
        backend,
        pii_bucket=os.getenv("S3_PII_RUNS_BUCKET", "pii-contracts"),
        quality_bucket=os.getenv("S3_QUALITY_RUNS_BUCKET", "quality-contracts"),
    )


@dataclass
class GlobalConfig:
    """
    The single global config object for a session.

    Captured once at session creation, mutated between runs (e.g. when the
    user edits the regex / quality lists in discovery), and snapshotted in
    full into every run so each run is independently reproducible.

    The two editable lists — ``regex_set`` and ``quality_rule_set`` — are
    embedded here as first-class objects (not by-name references), so editing
    a pattern/rule then re-running a scan operates on this object directly.
    """
    scan_mode: str = "both"
    equation_mode: str = "independent"
    pii_engines: str = "both"
    pii_regex_confidence: float = 0.80
    pii_gliner_confidence: float = 0.40
    pii_llm_confidence: float = 0.82
    pii_gliner_always_run: bool = False
    pii_gliner_model: str = ""
    active_ner_model_name: str = ""
    pii_models_dir: str = ""
    # GLiNER zero-shot entity labels (empty → manifest / built-in defaults at scan time).
    pii_ner_labels: List[str] = field(default_factory=list)
    llm_enabled: bool = False
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.5-flash"
    llm_api_key: str = ""
    llm_endpoint_url: Optional[str] = None
    quality_rules_mode: str = "profiler"
    generate_ge_docs: bool = True
    validate_contracts: bool = True
    triage_threshold: float = 0.0
    selected_columns: Optional[List[str]] = None
    use_phonenumbers: Optional[bool] = None
    masking_default_locale: str = "default"

    # When True, a scan auto-merges its partial into the active contract via
    # ContractStore.upsert(). The web UI keeps this False (merge is manual,
    # after editing sub-contracts); the CLI / object model can opt in to
    # auto-create the contract after a full / PII / quality scan.
    # Deprecated by ``automerge`` (per-type) below; kept for back-compat.
    auto_merge_contract: bool = False

    # v2 per-type automerge toggle: "none" | "pii" | "quality" | "both".
    # On scan, a run whose kind is covered here is written to its type bucket
    # AND immediately selected-and-merged into the active contract.
    automerge: str = "none"

    def automerges(self, kind: str) -> bool:
        """True if a run of the given kind should be auto-merged on scan."""
        mode = (self.automerge or "none").lower()
        if self.auto_merge_contract and mode == "none":
            import warnings
            warnings.warn(
                "auto_merge_contract no longer forces automerge; set automerge='both' explicitly",
                DeprecationWarning,
                stacklevel=2,
            )
        return mode == "both" or mode == kind

    # ── Embedded editable lists (the heart of edit-then-rerun) ─────────────
    regex_set: RegexSet = field(default_factory=RegexSet)
    quality_rule_set: QualityRuleSet = field(default_factory=QualityRuleSet)

    # Names of the saved configs these lists were loaded from (informational).
    @property
    def pii_regex_config_name(self) -> Optional[str]:
        return self.regex_set.name

    @property
    def quality_config_name(self) -> Optional[str]:
        return self.quality_rule_set.name

    def to_dict(self) -> dict:
        d = {
            k: getattr(self, k)
            for k in self.__dataclass_fields__
            if k not in ("regex_set", "quality_rule_set")
        }
        d["regex_set"] = self.regex_set.to_dict()
        d["quality_rule_set"] = self.quality_rule_set.to_dict()
        d["pii_regex_config_name"] = self.pii_regex_config_name
        d["quality_config_name"] = self.quality_config_name
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalConfig":
        scalar_fields = {
            f for f in cls.__dataclass_fields__
            if f not in ("regex_set", "quality_rule_set")
        }
        kwargs = {k: v for k, v in d.items() if k in scalar_fields}
        kwargs["regex_set"] = RegexSet.from_dict(d.get("regex_set"))
        kwargs["quality_rule_set"] = QualityRuleSet.from_dict(d.get("quality_rule_set"))
        return cls(**kwargs)


# Backward-compat alias — the class was previously named CommonConfig.
CommonConfig = GlobalConfig



def _effective_ner_labels(cfg: CommonConfig) -> list[str]:
    """Session labels override; empty list defers to manifest / defaults in the registry."""
    return list(cfg.pii_ner_labels or [])


def _effective_ner_model_path(cfg: CommonConfig) -> str:
    """Session path → env → sole model under ``pii_models_dir`` / ``REDIBIS_MODELS_DIR``."""
    from redibis.config import GlinerConfig, NERConfig
    from redibis.pii.ner_registry import NERModelRegistry

    explicit = (cfg.pii_gliner_model or "").strip()
    if explicit:
        return explicit
    resolved = NERModelRegistry.resolve_model_path(
        NERConfig(),
        GlinerConfig(model_id=""),
        models_dir=(cfg.pii_models_dir or "").strip() or None,
    )
    return resolved or ""


def _build_scan_config(table: str, cfg: CommonConfig, output_dir: Path) -> ScanConfig:
    from redibis.config import GlinerConfig, LLMConfig, NERConfig
    llm_cfg = None
    if cfg.llm_enabled:
        llm_cfg = LLMConfig(provider=cfg.llm_provider, model_name=cfg.llm_model,
                            api_key=cfg.llm_api_key or None, endpoint_url=cfg.llm_endpoint_url)
    # The engine-facing RegexOverrides is derived from the embedded RegexSet,
    # so edits to cfg.regex_set are picked up on the next scan automatically.
    regex_overrides = cfg.regex_set.to_overrides() if cfg.regex_set else None
    ner_model_path = _effective_ner_model_path(cfg)
    return ScanConfig(
        table=table, equation_mode=cfg.equation_mode, output_dir=output_dir,
        run_pii=cfg.scan_mode in ("pii", "both"),
        run_quality=cfg.scan_mode in ("quality", "both"),
        pii_engines=cfg.pii_engines,
        pii_regex_confidence=cfg.pii_regex_confidence,
        pii_gliner_confidence=cfg.pii_gliner_confidence,
        pii_llm_confidence=cfg.pii_llm_confidence,
        pii_gliner_always_run=cfg.pii_gliner_always_run,
        pii_columns=cfg.selected_columns,
        pii_regex_overrides=regex_overrides if regex_overrides else None,
        generate_ge_docs=cfg.generate_ge_docs,
        validate_contracts=cfg.validate_contracts,
        triage_threshold=cfg.triage_threshold,
        gliner_config=GlinerConfig(model_id=ner_model_path),
        ner_config=NERConfig(
            model_path=ner_model_path,
            labels=_effective_ner_labels(cfg),
            always_run=cfg.pii_gliner_always_run,
        ),
        llm_config=llm_cfg,
        quality_rule_set=cfg.quality_rule_set,
        use_phonenumbers=cfg.use_phonenumbers,
    )

def from_global_config(gc: GlobalConfig) -> "RedibisConfig":
    """Project web ``GlobalConfig`` → ``RedibisConfig``."""
    from redibis.config import (
        ContractConfig,
        GlinerConfig,
        LLMConfig,
        MaskingConfig,
        NERConfig,
        PIIConfig,
        ProfilingConfig,
        QualityConfig,
        RedibisConfig,
    )
    from redibis.pii.thresholds import Thresholds
    from redibis.quality.rule_set import QualityRuleSet

    scan_types: list[str] = []
    if gc.scan_mode in ("quality", "both"):
        scan_types.extend(["profile", "quality"])
    if gc.scan_mode in ("pii", "both"):
        scan_types.append("pii")

    thresholds = Thresholds(
        presidio_min=gc.pii_regex_confidence,
        gliner_min=gc.pii_gliner_confidence,
        llm_min=gc.pii_llm_confidence,
    )

    regex_overrides = gc.regex_set.to_dict() if gc.regex_set else None

    automerge = gc.automerge
    if gc.auto_merge_contract and automerge == "none":
        import warnings
        warnings.warn(
            "auto_merge_contract no longer forces automerge; set automerge='both' explicitly",
            DeprecationWarning,
            stacklevel=2,
        )

    return RedibisConfig(
        table="",
        scan_types=scan_types or ["profile", "quality", "pii"],
        profiling=ProfilingConfig(
            triage_threshold=gc.triage_threshold,
        ),
        quality=QualityConfig(
            generate_ge_docs=gc.generate_ge_docs,
            rule_set=QualityRuleSet.from_dict(gc.quality_rule_set.to_dict())
            if gc.quality_rule_set else QualityRuleSet(),
        ),
        pii=PIIConfig(
            engines=gc.pii_engines,
            equation_mode=gc.equation_mode,
            thresholds=thresholds,
            models_dir=(gc.pii_models_dir or "").strip() or "/models",
            ner=NERConfig(
                model_path=_effective_ner_model_path(gc),
                labels=_effective_ner_labels(gc),
                always_run=gc.pii_gliner_always_run,
            ),
            gliner=GlinerConfig(model_id=_effective_ner_model_path(gc)),
            llm=LLMConfig(
                provider=gc.llm_provider,
                model_name=gc.llm_model,
                api_key=gc.llm_api_key or None,
                endpoint_url=gc.llm_endpoint_url,
                enabled=gc.llm_enabled,
            ),
            gliner_always_run=gc.pii_gliner_always_run,
            regex_overrides=regex_overrides,
            selected_columns=gc.selected_columns,
            use_phonenumbers=gc.use_phonenumbers if gc.use_phonenumbers is not None else True,
        ),
        masking=MaskingConfig(
            default_locale=gc.masking_default_locale,
        ),
        contract=ContractConfig(
            automerge=automerge,
            validate=gc.validate_contracts,
            auto_write=False,
        ),
    )


def to_global_config(cfg: "RedibisConfig", *, table: str = "") -> GlobalConfig:
    """Project ``RedibisConfig`` → web ``GlobalConfig``."""
    from redibis.config import _flags_to_scan_mode, _scan_types_to_flags
    from redibis.pii.regex_overrides import RegexSet
    from redibis.quality.rule_set import QualityRuleSet

    run_pii, run_profile, run_quality = _scan_types_to_flags(cfg.scan_types)
    scan_mode = _flags_to_scan_mode(run_pii, run_profile, run_quality)

    regex_set = RegexSet.from_dict(cfg.pii.regex_overrides) if cfg.pii.regex_overrides else RegexSet()

    return GlobalConfig(
        scan_mode=scan_mode,
        equation_mode=cfg.pii.equation_mode,
        pii_engines=cfg.pii.engines,
        pii_regex_confidence=cfg.pii.thresholds.presidio_min,
        pii_gliner_confidence=cfg.pii.thresholds.gliner_min,
        pii_llm_confidence=cfg.pii.thresholds.llm_min,
        pii_gliner_always_run=cfg.pii.ner_always_run(),
        pii_gliner_model=cfg.pii.ner.model_path or cfg.pii.gliner.model_id,
        pii_models_dir=cfg.pii.models_dir,
        pii_ner_labels=list(cfg.pii.ner.labels or []),
        llm_enabled=cfg.pii.llm.enabled,
        llm_provider=cfg.pii.llm.provider,
        llm_model=cfg.pii.llm.model_name,
        llm_api_key=cfg.pii.llm.api_key,
        llm_endpoint_url=cfg.pii.llm.endpoint_url,
        generate_ge_docs=cfg.quality.generate_ge_docs,
        validate_contracts=cfg.contract.validate,
        triage_threshold=cfg.profiling.triage_threshold,
        selected_columns=cfg.pii.selected_columns,
        use_phonenumbers=cfg.pii.use_phonenumbers,
        masking_default_locale=cfg.masking.default_locale,
        automerge=cfg.contract.automerge,
        regex_set=regex_set,
        quality_rule_set=QualityRuleSet.from_dict(cfg.quality.rule_set.to_dict()),
    )
