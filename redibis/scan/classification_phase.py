"""Optional classification phase during unified scan."""

from __future__ import annotations

from typing import Any, Optional

from redibis.config import RedibisConfig
from redibis.services import pipeline


def run_classification_phase(
    contract: dict[str, Any],
    table: str,
    config: RedibisConfig,
) -> list[dict[str, Any]]:
    """Classify columns when ``classification.enabled`` is set in config."""
    return pipeline.run_classification(contract, table, config=config)


def contract_for_classification(
    *,
    pii_partial: Optional[dict[str, Any]],
    quality_partial: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Prefer the PII partial (has privacy signals); fall back to quality partial."""
    return pipeline.contract_for_classification(
        pii_partial=pii_partial,
        quality_partial=quality_partial,
    )
