"""
redibis
=======
Data contract pipeline for Cloudera CDP — ODCS v3 contracts with
Great Expectations quality, Presidio + GLiNER PII detection, and
Arabic + English multilingual support.

Quick start (facades)::

    from redibis import PIIScan, ScanConfig

    result = PIIScan(ScanConfig(table="db.table")).run(df)

``import redibis`` loads only lightweight symbols (models, config).
Scan facades, runners, and store helpers load on first attribute access.
"""

from __future__ import annotations

__version__ = "0.6.11"

# ── Eager: lightweight core (no GE / Presidio import chain) ───────────────
from redibis.config import RedibisConfig  # noqa: F401
from redibis.models import (  # noqa: F401
    ARABIC_UNICODE_RE,
    ColumnProfile,
    PII_NAME_HINTS,
    PIIDetection,
    RunMetadata,
    SENSITIVE_ENTITIES,
    classify_sensitivity,
)
from redibis.pipeline_config import GEPipelineConfig, PipelineConfig  # noqa: F401
from redibis.pii.equations import decide_pii  # noqa: F401
from redibis.pii.thresholds import DEFAULT_EQUATION, EQUATION_MODES, Thresholds  # noqa: F401

__all__ = [
    "__version__",
    # Primary scan API
    "PIIScan",
    "ProfileScan",
    "QualityScan",
    "ScanConfig",
    "RedibisConfig",
    # Core models
    "PIIDetection",
    "ColumnProfile",
    "RunMetadata",
    "classify_sensitivity",
    "SENSITIVE_ENTITIES",
    "ARABIC_UNICODE_RE",
    "PII_NAME_HINTS",
    # PII helpers
    "Thresholds",
    "DEFAULT_EQUATION",
    "EQUATION_MODES",
    "decide_pii",
    # Legacy workflow config
    "PipelineConfig",
    "GEPipelineConfig",
    # Scan orchestrator (advanced)
    "Scan",
    "ReportBundle",
    "ScanContractWriter",
    # Legacy runners
    "PIIDetectionRunner",
    "QualityRunner",
    # Store
    "ContractStore",
    "LocalBackend",
    "StorageBackend",
    "S3Config",
    "S3Backend",
    "get_backend",
    "RunOutputWriter",
    "UpsertResult",
    "TableHistoryEntry",
    "IdentityConflictError",
    "merge_two_contracts",
    "merge_odcs_contracts",
    # Writers / sampling
    "PIIContractWriter",
    "TableSampler",
    "PandasTableSampler",
    "SamplingConfig",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "PIIScan": ("redibis.scan.facades", "PIIScan"),
    "ProfileScan": ("redibis.scan.facades", "ProfileScan"),
    "QualityScan": ("redibis.scan.facades", "QualityScan"),
    "ScanConfig": ("redibis.services.scan_service", "ScanConfig"),
    "Scan": ("redibis.scan", "Scan"),
    "ReportBundle": ("redibis.scan", "ReportBundle"),
    "ScanContractWriter": ("redibis.scan", "ScanContractWriter"),
    "PIIDetectionRunner": ("redibis.pii.runner", "PIIDetectionRunner"),
    "QualityRunner": ("redibis.quality.runner", "QualityRunner"),
    "ContractStore": ("redibis.store.contract_store", "ContractStore"),
    "LocalBackend": ("redibis.store.storage_backend", "LocalBackend"),
    "StorageBackend": ("redibis.store.storage_backend", "StorageBackend"),
    "S3Config": ("redibis.store.storage_backend", "S3Config"),
    "S3Backend": ("redibis.store.storage_backend", "S3Backend"),
    "get_backend": ("redibis.store.storage_backend", "get_backend"),
    "RunOutputWriter": ("redibis.store.run_output_writer", "RunOutputWriter"),
    "UpsertResult": ("redibis.store.contract_store", "UpsertResult"),
    "TableHistoryEntry": ("redibis.store.contract_store", "TableHistoryEntry"),
    "IdentityConflictError": ("redibis.store.merger", "IdentityConflictError"),
    "merge_two_contracts": ("redibis.store.merger", "merge_two_contracts"),
    "merge_odcs_contracts": ("redibis.store.merger", "merge_odcs_contracts"),
    "PIIContractWriter": ("redibis.pii.contract_writer", "PIIContractWriter"),
    "TableSampler": ("redibis.quality.sampling", "TableSampler"),
    "PandasTableSampler": ("redibis.quality.sampling", "PandasTableSampler"),
    "SamplingConfig": ("redibis.quality.sampling", "SamplingConfig"),
}

_RENAMES: dict[str, tuple[str, str, str]] = {
    "PIIPipeline": (
        "PIIDetectionRunner",
        "redibis.pii.runner",
        "PIIDetectionRunner",
    ),
    "GEPipeline": (
        "QualityRunner",
        "redibis.quality.runner",
        "QualityRunner",
    ),
    "ContractDraftService": (
        "ScanContractWriter",
        "redibis.scan",
        "ScanContractWriter",
    ),
}

_ALIASES: dict[str, tuple[str, str, str]] = {
    "ContractStoreService": (
        "ContractStore",
        "redibis.store.contract_store",
        "ContractStore",
    ),
    "PIISampler": (
        "TableSampler",
        "redibis.quality.sampling",
        "TableSampler",
    ),
    "PandasSampler": (
        "PandasTableSampler",
        "redibis.quality.sampling",
        "PandasTableSampler",
    ),
    "SamplerConfig": (
        "SamplingConfig",
        "redibis.quality.sampling",
        "SamplingConfig",
    ),
    "PIIColumnProfile": (
        "ColumnProfile",
        "redibis.models",
        "ColumnProfile",
    ),
}


def _lazy_import(module: str, attr: str):
    import importlib

    return getattr(importlib.import_module(module), attr)


def __getattr__(name: str):
    import warnings

    if name in _LAZY_EXPORTS:
        module, attr = _LAZY_EXPORTS[name]
        value = _lazy_import(module, attr)
        globals()[name] = value
        return value

    if name in _RENAMES:
        new_name, module, attr = _RENAMES[name]
        warnings.warn(
            f"{name} is renamed to {new_name}",
            DeprecationWarning,
            stacklevel=2,
        )
        value = _lazy_import(module, attr)
        globals()[name] = value
        return value

    if name in _ALIASES:
        new_name, module, attr = _ALIASES[name]
        warnings.warn(
            f"{name} is renamed to {new_name}",
            DeprecationWarning,
            stacklevel=2,
        )
        value = _lazy_import(module, attr)
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | set(_ALIASES) | set(_RENAMES))
