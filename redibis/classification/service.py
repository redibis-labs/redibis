"""High-level classification service — contract columns → resolved tags."""

from __future__ import annotations

from typing import Any, Optional

from redibis.classification.approval import ApprovalGate, gate_for_result
from redibis.classification.ensemble import ClassifierEnsemble, _schema_properties
from redibis.classification.jurisdiction import JurisdictionContext, jurisdiction_for_table
from redibis.classification.models import ClassificationContext, ClassificationResult
from redibis.classification.policy_engine import PolicyEngine
from redibis.classification.policy_pack import ClassificationPolicy, get_builtin_pack
from redibis.memory.retriever import ContextRetriever, RetrievedContext


class ClassificationService:
    """Classify contract columns using the deterministic policy engine."""

    def __init__(
        self,
        policy: Optional[ClassificationPolicy] = None,
        *,
        retriever: Optional[ContextRetriever] = None,
    ):
        self.policy = policy or get_builtin_pack()
        self.engine = PolicyEngine(self.policy)
        self.ensemble = ClassifierEnsemble(self.policy)
        self._retriever = retriever
        self._jurisdictions: dict[str, JurisdictionContext] = {}

    def set_jurisdiction(self, ctx: JurisdictionContext) -> None:
        self._jurisdictions[ctx.table] = ctx

    def classify_column(
        self,
        contract: dict,
        table: str,
        column: str,
        *,
        llm_suggestions: Optional[list[dict[str, Any]]] = None,
        use_memory: bool = False,
        fingerprint: Any = None,
    ) -> ClassificationResult:
        prop = self._find_column_prop(contract, table, column)
        memory_suggestions = None
        if use_memory and self._retriever and fingerprint is not None:
            memory_suggestions = self._memory_suggestions(fingerprint)

        candidates = self.ensemble.collect(
            prop,
            llm_suggestions=llm_suggestions,
            memory_suggestions=memory_suggestions,
        )
        jurisdiction = jurisdiction_for_table(self._jurisdictions, table)
        self.engine.apply_jurisdiction_inference(candidates, jurisdiction, prop)
        ctx = ClassificationContext(
            table=table,
            column=column,
            jurisdiction=jurisdiction,
            column_logical_type=str(prop.get("logicalType") or ""),
        )
        return self.engine.resolve(candidates, ctx)

    def classify_contract(
        self,
        contract: dict,
        table: str,
        *,
        use_memory: bool = False,
    ) -> list[ClassificationResult]:
        results: list[ClassificationResult] = []
        for prop in _schema_properties(contract, table):
            name = prop.get("name")
            if not name:
                continue
            results.append(self.classify_column(
                contract, table, name, use_memory=use_memory,
            ))
        return results

    def classify_with_gate(
        self,
        contract: dict,
        table: str,
        column: str,
        **kwargs: Any,
    ) -> ApprovalGate:
        return gate_for_result(self.classify_column(contract, table, column, **kwargs))

    def _find_column_prop(self, contract: dict, table: str, column: str) -> dict:
        for prop in _schema_properties(contract, table):
            if prop.get("name") == column:
                return prop
        raise KeyError(f"column {column!r} not found in contract for {table!r}")

    def _memory_suggestions(self, fingerprint: Any) -> list[dict[str, Any]]:
        if not self._retriever:
            return []
        try:
            contexts: list[RetrievedContext] = self._retriever.retrieve(fingerprint)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for ctx in contexts:
            for decision in ctx.decisions:
                classification = decision.classification
                if not classification:
                    continue
                if isinstance(classification, dict):
                    out.append(classification)
                elif isinstance(classification, str) and ":" in classification:
                    domain, tag = classification.split(":", 1)
                    out.append({"domain": domain, "tag": tag, "confidence": 0.4})
        return out
