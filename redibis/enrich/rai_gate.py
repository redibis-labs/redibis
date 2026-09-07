"""RAI preflight helpers for contract enrichment (no LLM calls)."""

from __future__ import annotations

from typing import Any, Optional

from redibis.config import RAIConfig, RedibisConfig
from redibis.enrich.providers import EnrichmentProvider
from redibis.telemetry.model_gateway import resolve_provider_residency
from redibis.telemetry.pii_scope import contract_columns_with_pii, infer_contains_raw_pii
from redibis.telemetry.rai import RAIMiddleware


def compute_enrich_rai_preflight(
    *,
    contract: dict,
    table: str,
    provider: EnrichmentProvider,
    redibis_config: Optional[RedibisConfig] = None,
    sample_data_count: int = 0,
    external_masked_acknowledged: bool = False,
    user_prompt: str = "",
) -> dict[str, Any]:
    """Return residency / PII scope / block prediction for the enrich UI and logs."""
    cfg = (redibis_config.rai if redibis_config else RAIConfig())
    residency = resolve_provider_residency(provider, rai_config=cfg)
    pii_columns = contract_columns_with_pii(contract, table)
    contains_raw_pii, _ = infer_contains_raw_pii(
        contract=contract,
        table=table,
        user_prompt=user_prompt,
        attested_masked_external=external_masked_acknowledged and sample_data_count > 0,
    )

    would_block = False
    block_reason = ""
    if cfg.enabled:
        rai = RAIMiddleware(cfg)
        model_check = rai.check_model_allowed(provider.name or provider.model or "unknown")
        if not model_check.allowed:
            would_block = rai._should_block(model_check)
            block_reason = model_check.reason
        else:
            residency_check = rai.check_residency(
                residency=residency,
                contains_raw_pii=contains_raw_pii,
            )
            if not residency_check.allowed:
                would_block = rai._should_block(residency_check)
                block_reason = residency_check.reason

    hints: list[str] = []
    if provider.name == "demo":
        hints.append("Demo provider — offline template enrichment, no LLM or network required.")
        return {
            "provider": provider.name,
            "model": provider.model or "offline",
            "residency": "local",
            "pii_columns": pii_columns,
            "pii_column_count": len(pii_columns),
            "contains_raw_pii": bool(pii_columns),
            "sample_data_count": sample_data_count,
            "external_masked_acknowledged": external_masked_acknowledged,
            "would_block": False,
            "block_reason": "",
            "rai_enabled": cfg.enabled,
            "hard_block_external_pii": cfg.hard_block_external_pii,
            "hints": hints,
            "offline_demo": True,
        }

    if pii_columns and residency != "local":
        if not sample_data_count:
            hints.append(
                "Upload masked sample data (from the Masking export) before external enrichment."
            )
        if not external_masked_acknowledged:
            hints.append(
                "Check “Masked export approved for external LLM” after uploading de-identified samples."
            )
        if sample_data_count and external_masked_acknowledged:
            hints.append(
                "Contract PII metadata will be redacted; the model sees column names + masked samples only."
            )
    if residency == "local":
        hints.append("Local/on-prem provider — external PII residency rules do not apply.")

    return {
        "provider": provider.name,
        "model": provider.model,
        "residency": residency,
        "pii_columns": pii_columns,
        "pii_column_count": len(pii_columns),
        "contains_raw_pii": contains_raw_pii,
        "sample_data_count": sample_data_count,
        "external_masked_acknowledged": external_masked_acknowledged,
        "would_block": would_block,
        "block_reason": block_reason,
        "rai_enabled": cfg.enabled,
        "hard_block_external_pii": cfg.hard_block_external_pii,
        "hints": hints,
    }
