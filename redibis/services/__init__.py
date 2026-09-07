"""
redibis.services
================
Business logic layer. Three services, one responsibility each.

  ScanService      — full pipeline: CSV → quality + PII → S3 → links
  BrowseService    — read-only: list tables, contracts, runs, artifacts
  ContractService  — write: business definitions, SQL constraints, overrides

REST endpoints and CLI commands are thin adapters over these services.
A future GUI will use the same services. Change logic in one place.

Public facade names load lazily so ``import redibis.services`` stays light.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redibis.services.browse_service import BrowseService, ContractSummary, RunSummary
    from redibis.services.code_scan_session import CodeScanSession
    from redibis.services.contract_service import (
        BusinessUpdate,
        ColumnDefinition,
        ContractService,
        SQLConstraint,
    )
    from redibis.services.scan_service import ScanConfig, ScanResult, ScanService

__all__ = [
    "BrowseService",
    "BusinessUpdate",
    "CodeScanSession",
    "ColumnDefinition",
    "ContractService",
    "ContractSummary",
    "RunSummary",
    "SQLConstraint",
    "ScanConfig",
    "ScanResult",
    "ScanService",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BrowseService": ("redibis.services.browse_service", "BrowseService"),
    "BusinessUpdate": ("redibis.services.contract_service", "BusinessUpdate"),
    "CodeScanSession": ("redibis.services.code_scan_session", "CodeScanSession"),
    "ColumnDefinition": ("redibis.services.contract_service", "ColumnDefinition"),
    "ContractService": ("redibis.services.contract_service", "ContractService"),
    "ContractSummary": ("redibis.services.browse_service", "ContractSummary"),
    "RunSummary": ("redibis.services.browse_service", "RunSummary"),
    "SQLConstraint": ("redibis.services.contract_service", "SQLConstraint"),
    "ScanConfig": ("redibis.services.scan_service", "ScanConfig"),
    "ScanResult": ("redibis.services.scan_service", "ScanResult"),
    "ScanService": ("redibis.services.scan_service", "ScanService"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        import importlib

        module_name, attr = _LAZY_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
