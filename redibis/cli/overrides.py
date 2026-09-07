"""CLI flag overrides for ``RedibisConfig``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from redibis.scan_mode import parse_scan_mode
from redibis.config import RedibisConfig


def apply_cli_overrides(cfg: RedibisConfig, args: Any) -> RedibisConfig:
    """Mutate a loaded config from CLI flag overrides (scan command)."""
    if getattr(args, "table", None):
        cfg.table = args.table
    if getattr(args, "mode", None):
        run_pii, run_profile, run_quality = parse_scan_mode(args.mode)
        scan_types: list[str] = []
        if run_profile:
            scan_types.append("profile")
        if run_quality:
            scan_types.append("quality")
        if run_pii:
            scan_types.append("pii")
        cfg.scan_types = scan_types or ["profile", "quality", "pii"]
    if getattr(args, "equation", None):
        cfg.pii.equation_mode = args.equation
    if getattr(args, "pii_engines", None):
        cfg.pii.engines = args.pii_engines
    ner_model = getattr(args, "ner_model", None) or getattr(args, "gliner_model", None)
    if ner_model:
        cfg.pii.ner.model_path = ner_model
        cfg.pii.gliner.model_id = ner_model
    ner_labels = (getattr(args, "ner_labels", None) or "").strip()
    if ner_labels:
        cfg.pii.ner.labels = [x.strip() for x in ner_labels.split(",") if x.strip()]
    if getattr(args, "no_ge_docs", False):
        cfg.quality.generate_ge_docs = False
    if getattr(args, "no_validate", False):
        cfg.contract.validate = False
    if getattr(args, "automerge", None):
        cfg.contract.automerge = args.automerge
    if getattr(args, "scan_output_dir", None):
        cfg.report.output_dir = Path(args.scan_output_dir)
    elif getattr(args, "output_dir", None):
        cfg.report.output_dir = Path(args.output_dir)
    if getattr(args, "profiler_engine", None):
        cfg.profiling.engine = args.profiler_engine
    if getattr(args, "enable_metadata", False):
        cfg.profiling.metadata.enabled = True
    if getattr(args, "enable_pushdown", False):
        cfg.profiling.pushdown.enabled = True
    if getattr(args, "enable_memory", False):
        cfg.memory.enabled = True
    if getattr(args, "memory_domain", None):
        cfg.memory.domain = args.memory_domain
    if getattr(args, "memory_store", None):
        cfg.memory.store = args.memory_store
    if getattr(args, "source_engine", None):
        cfg.source.engine = args.source_engine
    if getattr(args, "use_s3", False):
        cfg.storage.backend = "s3"
    else:
        cfg.storage.backend = "local"
    for attr, bucket_field in (
        ("s3_runs_bucket", "runs_bucket"),
        ("s3_contracts_bucket", "contracts_bucket"),
        ("s3_pii_runs_bucket", "pii_runs_bucket"),
        ("s3_quality_runs_bucket", "quality_runs_bucket"),
    ):
        val = getattr(args, attr, None)
        if val:
            setattr(cfg.storage, bucket_field, val)
    if getattr(args, "no_phonenumbers", False):
        cfg.pii.use_phonenumbers = False
    return cfg
