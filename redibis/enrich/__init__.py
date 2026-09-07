"""
redibis.enrich — full-contract LLM enrichment (architecture v2 §3.5).

Enrichment operates over the whole active contract: the LLM is handed the
combined pii + quality view as one document and returns an enriched full
contract. It is the most powerful editor in the system, so it is gated by a
validity check (ODCS schema) before anything merges.

All providers are called through LiteLLM and configured declaratively in a
JSON file (``llm_providers.json``) — add a provider without writing code.

Public API:
    EnrichmentProvider     — adapter interface
    LiteLLMProvider        — the single LiteLLM-backed implementation
    get_provider(name)     — build a provider from the JSON registry
    list_providers()       — list configured providers
    EnrichmentService      — enrich(), validate(), merge_candidate()
"""

from redibis.enrich.providers import (
    EnrichmentProvider,
    LiteLLMProvider,
    EnrichmentError,
    get_provider,
    list_providers,
    load_provider_configs,
    PROVIDERS,
)
from redibis.enrich.service import (
    EnrichmentContext,
    EnrichmentResult,
    EnrichmentService,
    enrichment_service_for_store,
)

__all__ = [
    "EnrichmentProvider", "LiteLLMProvider", "EnrichmentError",
    "get_provider", "list_providers", "load_provider_configs", "PROVIDERS",
    "EnrichmentContext", "EnrichmentService", "EnrichmentResult",
    "enrichment_service_for_store",
]
