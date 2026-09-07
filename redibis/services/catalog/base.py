"""Neutral catalog publisher interface — backend-agnostic push/status types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CatalogPushOptions:
    tags: bool = True
    glossary: bool = True
    contract: bool = True
    quality: bool = True
    masked_samples: bool = False
    lifecycle_diff: bool = True
    lifecycle_artifacts: Optional[dict] = None
    # Deterministic ClassificationResult list (policy engine). Suggestions are
    # never published; LawfulIntercept is filtered out by the OM/Atlas bridges.
    classification_results: Optional[list] = None
    # Reconciler mode: "normal" (human wins) or "enforce" (Redibis wins, audited).
    reconcile_mode: str = "normal"
    clear_suppressions: bool = False
    enforce_reason: str = ""
    enforce_actor: str = ""
    # Per-column discovery evidence from ContractMetadataStore (confidence, …).
    column_telemetry: Optional[dict] = None
    # Optional ContractMetadataStore — builder reads column telemetry when set.
    metadata_store: Any = None
    # Scan coverage records used verbatim when provided; else contract-derived.
    scan_coverage: Optional[list] = None
    # Normalized quality monitor results for OM testCaseResult publish.
    quality_results: Optional[list] = None
    quality_run_id: str = ""
    quality_artifact_ref: str = ""
    # Bypass entity-resolution cache and re-lookup the target FQN.
    refresh_entity: bool = False


@dataclass
class CatalogPushResult:
    table: str
    backend: str
    entity_fqn: str
    entity_id: str = ""
    contract_id: str = ""
    glossary_count: int = 0
    dry_run: bool = False
    preview: Optional[dict[str, Any]] = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CatalogStatus:
    table: str
    backend: str
    contract_version: str
    last_push: Optional[dict[str, Any]] = None
    in_sync: Optional[bool] = None


class CatalogPublisher(ABC):
    """Push an active ODCS contract into a data-governance catalog."""

    backend: str = ""

    @classmethod
    @abstractmethod
    def from_config(cls, config: Any) -> "CatalogPublisher":
        """Build a publisher from ``RedibisConfig.catalog``."""

    @abstractmethod
    def preview(
        self,
        contract: dict,
        table: str,
        *,
        options: CatalogPushOptions,
    ) -> dict[str, Any]:
        """Return backend-specific payloads without network I/O."""

    @abstractmethod
    def push(
        self,
        contract: dict,
        table: str,
        *,
        options: CatalogPushOptions,
        dry_run: bool = False,
    ) -> CatalogPushResult:
        """Push (or dry-run preview wrapped in ``CatalogPushResult``)."""
