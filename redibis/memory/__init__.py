"""
redibis.memory
==============
Column fingerprinting, consent-gated redaction, and the enrichment learning loop.
"""

from redibis.memory.consent import ConsentEntry, SamplingConsentStore
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import EmbeddingProvider, get_embedding_provider
from redibis.memory.fingerprint import (
    ColumnFingerprint,
    FingerprintField,
    build_fingerprints,
    compute_format_signature,
)
from redibis.memory.rationale import derive_rationale
from redibis.memory.redaction import redact_samples
from redibis.memory.retriever import ContextRetriever, RetrievedContext, get_context_retriever
from redibis.memory.store import MemoryStore, get_memory_store

__all__ = [
    "ColumnFingerprint",
    "FingerprintField",
    "ReviewDecision",
    "ConsentEntry",
    "SamplingConsentStore",
    "EmbeddingProvider",
    "MemoryStore",
    "ContextRetriever",
    "RetrievedContext",
    "build_fingerprints",
    "compute_format_signature",
    "derive_rationale",
    "get_context_retriever",
    "get_embedding_provider",
    "get_memory_store",
    "redact_samples",
]
