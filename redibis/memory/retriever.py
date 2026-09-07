"""
redibis.memory.retriever — hybrid retrieval of past steward reviews for enrichment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from redibis.config import MemoryConfig
from redibis.memory.decision import ReviewDecision
from redibis.memory.embedding import EmbeddingProvider, get_embedding_provider
from redibis.memory.fingerprint import ColumnFingerprint
from redibis.memory.store import MemoryStore, get_memory_store


def _decision_conflicts(decisions: list[ReviewDecision]) -> list[str]:
    """Surface fields where past steward decisions disagree."""
    if len(decisions) < 2:
        return []
    conflicts: list[str] = []

    def _distinct(field: str) -> set[str]:
        return {
            str(getattr(d, field))
            for d in decisions
            if getattr(d, field, None) not in (None, "")
        }

    for field, label in (
        ("pii_verdict", "PII verdict"),
        ("classification", "classification"),
        ("masking_strategy", "masking"),
    ):
        values = _distinct(field)
        if len(values) > 1:
            conflicts.append(f"{label}: {sorted(values)}")

    definitions = {
        str(d.business_definition).strip()
        for d in decisions
        if d.business_definition and str(d.business_definition).strip()
    }
    if len(definitions) > 1:
        conflicts.append(f"business definitions: {len(definitions)} distinct")

    return conflicts


@dataclass
class RetrievedContext:
    """One past review match for suggest-only enrichment context."""

    fingerprint_summary: dict
    decisions: list[ReviewDecision] = field(default_factory=list)
    rationale: str = ""
    similarity: float = 0.0
    provenance: dict = field(default_factory=dict)
    occurrence_count: int = 1

    def to_prompt_block(self) -> str:
        """Render a provenance-tagged few-shot line for the enrichment prompt."""
        fp = self.fingerprint_summary
        col = fp.get("name_normalized") or fp.get("column") or "column"
        fmt = fp.get("format_signature", "")
        ltype = fp.get("logical_type", "")
        lines = [
            f"- Column like `{col}` ({ltype}, format={fmt})",
        ]
        if self.decisions:
            conflicts = _decision_conflicts(self.decisions)
            if conflicts:
                lines.append(
                    "  past review conflicts: " + "; ".join(conflicts)
                )
            d = self.decisions[-1]
            if d.pii_verdict:
                lines.append(f"  PII verdict: {d.pii_verdict}")
            if d.classification:
                lines.append(f"  classification: {d.classification}")
            if d.business_definition:
                lines.append(f"  business definition: {d.business_definition}")
            if d.masking_strategy:
                lines.append(f"  masking: {d.masking_strategy}")
            if d.quality_rules:
                lines.append(f"  quality rules: {len(d.quality_rules)} approved")
        if self.rationale:
            lines.append(f"  rationale: {self.rationale}")
        reviewer = self.provenance.get("reviewer") or ""
        decided = self.provenance.get("decided_at") or ""
        version = self.provenance.get("contract_version") or ""
        prov_bits = [b for b in (reviewer, decided, version) if b]
        if prov_bits:
            lines.append(f"  provenance: {', '.join(prov_bits)}")
        lines.append("  (suggest-only — steward must still approve)")
        return "\n".join(lines)


class ContextRetriever:
    """Filter+vector retrieval over the column memory store."""

    def __init__(
        self,
        memory_config: MemoryConfig,
        memory_store: MemoryStore,
        *,
        embedding: Optional[EmbeddingProvider] = None,
    ) -> None:
        self._config = memory_config
        self._store = memory_store
        self._embedding = embedding or get_embedding_provider(memory_config)

    def retrieve(
        self,
        fingerprint: ColumnFingerprint,
        *,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
    ) -> list[RetrievedContext]:
        query_vec = self._embedding.embed(fingerprint.to_column_card())
        return self._retrieve_with_vector(
            fingerprint,
            query_vec,
            top_k=top_k,
            min_similarity=min_similarity,
        )

    def retrieve_batch(
        self,
        fingerprints: list[ColumnFingerprint],
        *,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
    ) -> list[list[RetrievedContext]]:
        """Retrieve context for many columns with one embedding batch call."""
        if not fingerprints:
            return []
        cards = [fp.to_column_card() for fp in fingerprints]
        vectors = self._embedding.embed_batch(cards)
        return [
            self._retrieve_with_vector(
                fp, vec, top_k=top_k, min_similarity=min_similarity,
            )
            for fp, vec in zip(fingerprints, vectors)
        ]

    def _retrieve_with_vector(
        self,
        fingerprint: ColumnFingerprint,
        query_vec: list[float],
        *,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
    ) -> list[RetrievedContext]:
        top_k = top_k if top_k is not None else self._config.top_k
        min_similarity = (
            min_similarity
            if min_similarity is not None
            else self._config.min_similarity
        )
        logical_type = str(fingerprint.logical_type.value)
        format_signature = str(fingerprint.format_signature.value)

        # Type+format agreement is the hard gate. When both are concrete, similarity
        # floor is relaxed (sim_floor = -1.0) so near-miss embeddings still surface
        # for steward review; min_similarity still applies when format is empty/mixed.
        sim_floor = (
            -1.0
            if logical_type and format_signature and format_signature not in ("empty", "mixed")
            else min_similarity
        )
        hits = self._store.search(
            query_vec,
            top_k=max(top_k * 3, top_k),
            min_similarity=sim_floor,
            logical_type=logical_type,
            format_signature=format_signature,
        )

        ranked: list[RetrievedContext] = []
        for hit in hits:
            ctx = self._to_context(hit, fingerprint.domain)
            ctx.similarity = _rank_score(
                hit.similarity,
                ctx,
                query_domain=fingerprint.domain or self._config.domain,
            )
            ranked.append(ctx)

        ranked.sort(key=lambda c: c.similarity, reverse=True)
        return ranked[:top_k]

    def _to_context(self, hit, query_domain: str) -> RetrievedContext:
        decisions = [ReviewDecision.from_dict(d) for d in (hit.decisions or [])]
        latest = decisions[-1] if decisions else ReviewDecision("", "", "")
        provenance = {
            "reviewer": latest.reviewer,
            "decided_at": latest.decided_at,
            "contract_version": latest.contract_version,
            **(latest.provenance or {}),
        }
        return RetrievedContext(
            fingerprint_summary=dict(hit.fingerprint or {}),
            decisions=decisions,
            rationale=latest.rationale,
            similarity=float(hit.similarity),
            provenance=provenance,
            occurrence_count=int(hit.occurrence_count or 1),
        )


def get_context_retriever(
    memory_config: MemoryConfig,
    *,
    memory_store: Optional[MemoryStore] = None,
    embedding: Optional[EmbeddingProvider] = None,
) -> Optional[ContextRetriever]:
    if not memory_config.enabled:
        return None
    store = memory_store or get_memory_store(memory_config, embedding=embedding)
    if store is None:
        return None
    return ContextRetriever(memory_config, store, embedding=embedding)


def hint_to_dict(ctx: RetrievedContext) -> dict:
    """JSON-serializable hint for CLI / API consumers."""
    latest = ctx.decisions[-1] if ctx.decisions else None
    conflicts = _decision_conflicts(ctx.decisions)
    fp = ctx.fingerprint_summary or {}
    table = fp.get("table") or ""
    col = fp.get("name_normalized") or fp.get("column") or ""
    table_column = f"{table}.{col}" if table and col else col
    prior = latest.classification if latest and latest.classification else (
        latest.pii_verdict if latest else ""
    )
    return {
        "table_column": table_column,
        "score": round(float(ctx.similarity), 4),
        "similarity": round(float(ctx.similarity), 4),
        "prior_decision": prior,
        "prior_classification": latest.classification if latest else "",
        "prior_pii_verdict": latest.pii_verdict if latest else "",
        "fingerprint": fp,
        "rationale": ctx.rationale,
        "occurrence_count": ctx.occurrence_count,
        "provenance": ctx.provenance,
        "conflicts": conflicts,
        "pii_verdict": latest.pii_verdict if latest else "",
        "classification": latest.classification if latest else "",
        "business_definition": latest.business_definition if latest else "",
        "masking_strategy": latest.masking_strategy if latest else "",
        "prior_decisions": len(ctx.decisions),
    }


def build_memory_context_section(
    table: str,
    contract: dict,
    retriever: ContextRetriever,
    *,
    domain: str = "",
) -> str:
    """Assemble per-column retrieved review context for the enrichment prompt."""
    from redibis.memory.writer import fingerprint_from_column_prop

    column_fps: list[tuple[str, ColumnFingerprint]] = []
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            col = prop.get("name")
            if not col:
                continue
            fp = fingerprint_from_column_prop(
                table, str(col), prop, domain=domain or retriever._config.domain,
            )
            column_fps.append((str(col), fp))

    if not column_fps:
        return ""

    fingerprints = [fp for _, fp in column_fps]
    all_contexts = retriever.retrieve_batch(fingerprints)

    blocks: list[str] = []
    for (col, _), contexts in zip(column_fps, all_contexts):
        if not contexts:
            continue
        blocks.append(f"## Column `{col}`")
        for ctx in contexts:
            blocks.append(ctx.to_prompt_block())

    if not blocks:
        return ""
    header = (
        "# SIMILAR PAST STEWARD REVIEWS (suggest-only context)\n"
        "These are prior human decisions on similar columns. Use as hints only; "
        "the steward still approves all changes.\n"
    )
    return header + "\n".join(blocks) + "\n"


def build_similar_columns_for_table(
    table: str,
    contract: dict,
    retriever: ContextRetriever,
    *,
    top_k: Optional[int] = None,
) -> list[dict]:
    """Structured similar-column neighbors with score + prior decision (G3)."""
    from redibis.memory.writer import fingerprint_from_column_prop

    out: list[dict] = []
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if not isinstance(prop, dict):
                continue
            col = prop.get("name")
            if not col:
                continue
            fp = fingerprint_from_column_prop(
                table, str(col), prop, domain=retriever._config.domain,
            )
            contexts = retriever.retrieve(fp, top_k=top_k)
            for ctx in contexts:
                row = hint_to_dict(ctx)
                row["column"] = col
                out.append(row)
    out.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    if top_k is not None:
        return out[:top_k]
    return out


def build_similar_columns_per_column(
    table: str,
    contract: dict,
    retriever: ContextRetriever,
    *,
    top_k: Optional[int] = None,
) -> dict[str, list[dict]]:
    """Per-column similar neighbors for enrichment column context (R7)."""
    from redibis.enrich.evidence import group_similar_columns_by_name

    flat = build_similar_columns_for_table(
        table, contract, retriever, top_k=top_k,
    )
    return group_similar_columns_by_name(flat)


def _rank_score(
    base_similarity: float,
    ctx: RetrievedContext,
    *,
    query_domain: str,
) -> float:
    score = float(base_similarity)
    fp_domain = (ctx.fingerprint_summary or {}).get("domain") or ""
    if query_domain and fp_domain and query_domain == fp_domain:
        score += 0.05
    decided = ctx.provenance.get("decided_at") or ""
    score += 0.1 * _recency_weight(decided)
    reviewer = (ctx.provenance.get("reviewer") or "").lower()
    if reviewer.startswith("session:") or reviewer in ("web", "cli", "steward"):
        score += 0.02
    occ = max(1, int(ctx.occurrence_count or 1))
    score += 0.03 * min(occ / 10.0, 1.0)
    return score


def _recency_weight(decided_at: str) -> float:
    if not decided_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
        return max(0.0, 1.0 - age_days / 365.0)
    except (TypeError, ValueError):
        return 0.0
