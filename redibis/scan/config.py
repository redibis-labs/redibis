"""
redibis.scan.config
===================
``ScanConfig`` — the **engine-level** configuration for one scan run, consumed by both
the in-memory engine (``redibis.scan.Scan``) and the scan service
(``redibis.services.scan_service.ScanService``).

This is the *projected* engine config. The user-facing YAML spine is
``redibis.config.RedibisConfig``; project it here with
``redibis.services.scan_service.to_scan_config(redibis_config)``.

Home note: this dataclass previously lived in ``services/scan_service.py``. It is the
engine config, so it now lives in the ``scan`` package (consistent with the one-way
dependency rule: ``scan`` may import ``quality``/``pii``, not the reverse). The old import
path ``from redibis.services.scan_service import ScanConfig`` still works via a re-export.

This module is intentionally a **leaf**: it has no runtime imports from other redibis
packages (field types are resolved lazily under ``TYPE_CHECKING``), so importing it can
never create a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # type-only — never imported at runtime (no cycles)
    from redibis.config import GlinerConfig, LLMConfig, NERConfig
    from redibis.pii.ner_backend import NERBackend
    from redibis.pii.regex_overrides import RegexOverrides
    from redibis.pii.thresholds import Thresholds
    from redibis.quality.rule_set import QualityRuleSet
    from redibis.quality.sampling import SamplingConfig


@dataclass
class ScanConfig:
    """Engine-level configuration for a scan run.

    Consumed by ``redibis.scan.Scan`` (engine) and ``ScanService`` (service). Build it
    from a ``RedibisConfig`` via ``to_scan_config``. See ``docs/ARCHITECTURE_NAMING_REVIEW.md``.
    """
    table:              str                           # e.g. "telecom.customers"
    equation_mode:      str             = "independent"
    sampling_config:    Optional["SamplingConfig"] = None
    thresholds:         Optional["Thresholds"]    = None
    triage_threshold:   float           = 0.0
    run_profile:        bool            = True
    run_pii:            bool            = True        # False = quality-only scan
    run_quality:        bool            = True        # GE gatekeeper + quality contract
    profiler_engine:    str             = "great_expectations"
    generate_ge_docs:   bool            = True
    validate_contracts: bool            = True
    output_dir:         Path            = Path("./reports")

    # v2: per-type automerge — "none" | "pii" | "quality" | "both".
    # Each scan writes a run subcontract into its type bucket; a kind covered
    # here is also immediately selected-and-merged into the active contract.
    automerge:          str             = "none"

    # Optional session/run layout (CLI + programmatic scans). When ``artifacts_dir``
    # is set, all run outputs are written there instead of ``output_dir / run_id``.
    session_id:         Optional[str]           = None
    run_id:             Optional[str]           = None
    artifacts_dir:      Optional[Path]          = None

    def automerges(self, kind: str) -> bool:
        mode = (self.automerge or "none").lower()
        return mode == "both" or mode == kind

    # ── PII granular control ──────────────────────────────────────────────
    pii_columns:              Optional[List[str]]     = None     # scan specific columns only
    pii_engines:              str                     = "both"   # "regex" | "gliner" | "llm" | "both"
    pii_regex_confidence:     Optional[float]         = None
    pii_gliner_confidence:    Optional[float]         = None
    pii_llm_confidence:       Optional[float]         = None
    pii_gliner_always_run:    bool                    = False    # Deprecated — use ner_config.always_run
    pii_regex_overrides:      Optional["RegexOverrides"] = None  # add_regex or replace_all
    ner_config:               Optional["NERConfig"]   = None
    ner_backend:              Optional["NERBackend"]  = None     # optional pre-loaded backend
    gliner_config:            Optional["GlinerConfig"] = None    # deprecated legacy path resolution
    llm_config:               Optional["LLMConfig"]   = None

    def ner_always_run(self) -> bool:
        if self.ner_config and self.ner_config.always_run:
            return True
        return self.pii_gliner_always_run

    # Role-based masking override (dict / path). None → env REDIBIS_MASKING_ROLES,
    # ./masking_roles.json, then the packaged default.
    masking_roles:            Optional[Any]           = None

    # Optional user-curated quality rules (web session global config).
    quality_rule_set:         Optional["QualityRuleSet"] = None

    # Per-run override for libphonenumber engine (None → config default).
    use_phonenumbers:         Optional[bool]           = None

    # ── Evidence bundle (report.evidence_bundle in RedibisConfig) ────────────
    evidence_bundle_enabled:              bool          = True
    evidence_bundle_sample_mode:          str           = "raw"   # raw | masked | none
    evidence_bundle_sample_n:             int           = 10
    evidence_bundle_top_values_n:         int           = 20
    evidence_bundle_format_masks_n:       int           = 10
    evidence_bundle_numeric_histogram_bins: int         = 20
    evidence_bundle_length_histogram_bins:  int         = 20
    evidence_bundle_include_profile_groups: Optional[List[str]] = None
