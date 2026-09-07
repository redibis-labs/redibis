"""Classifier ensemble — evidence producers feeding the policy engine."""

from __future__ import annotations

from typing import Any, Iterator, Optional

from redibis.classification.models import CandidateTag, ClassificationContext
from redibis.classification.policy_pack import ClassificationPolicy
from redibis.contracts.privacy import col_pii_engine, column_is_pii
from redibis.services.catalog.mapping import iter_columns


def candidates_from_pii_entity(
    entity_type: str,
    *,
    confidence: float,
    policy: ClassificationPolicy,
    source: str = "pii_detector",
) -> list[CandidateTag]:
    """Map a PII entity type to policy-pack candidate tags."""
    mapping = policy.entity_type_mapping.get(entity_type.upper())
    if not mapping:
        return [
            CandidateTag(
                domain="DataSensitivity",
                tag="PII",
                confidence=confidence,
                source=source,
            )
        ]
    return [
        CandidateTag(
            domain=mapping["domain"],
            tag=mapping["tag"],
            confidence=confidence,
            source=source,
        )
    ]


def candidates_from_column_name(
    column_name: str,
    *,
    policy: ClassificationPolicy,
) -> list[CandidateTag]:
    """Heuristic column-name hints (deterministic, no LLM)."""
    name = column_name.lower()
    out: list[CandidateTag] = []
    hints = {
        "msisdn": ("TelcoDataType", "MSISDN"),
        "imei": ("TelcoDataType", "IMEI"),
        "imsi": ("TelcoDataType", "IMSI"),
        "cdr": ("TelcoDataType", "CDR_Data"),
        "invoice": ("TelcoDataType", "BillingData"),
        "billing": ("TelcoDataType", "BillingData"),
        "biometric": ("DataSensitivity", "BiometricData"),
        "fingerprint": ("DataSensitivity", "BiometricData"),
        "status": ("Lifecycle", "Active"),
    }
    for hint, (domain, tag) in hints.items():
        if hint in name and policy.tag_spec(domain, tag):
            out.append(CandidateTag(domain=domain, tag=tag, confidence=0.6, source="column_name"))
    return out


def candidates_from_contract_column(
    prop: dict,
    *,
    policy: ClassificationPolicy,
) -> list[CandidateTag]:
    """Build candidate tags from an ODCS column property."""
    out: list[CandidateTag] = []
    col_name = str(prop.get("name") or "")

    if column_is_pii(prop):
        ce = col_pii_engine(prop)
        entity = str(ce.get("entity_type") or "UNKNOWN")
        conf = float(ce.get("confidence") or 0.0)
        out.extend(candidates_from_pii_entity(entity, confidence=conf, policy=policy))

    out.extend(candidates_from_column_name(col_name, policy=policy))

    for raw in prop.get("tags") or []:
        tag = str(raw)
        if tag.lower() == "lifecycle":
            continue
        if tag.lower() == "regulated":
            # CPNI is jurisdiction-specific (US); do not infer from generic 'regulated'
            continue

    desc = (prop.get("description") or "").lower()
    if "churned" in desc or "churn" in desc:
        out.append(CandidateTag(
            domain="Lifecycle", tag="Churned", confidence=0.7, source="description",
        ))

    # PrivacyState reflects actual transformed state, not masking intent
    privacy = prop.get("privacy") or {}
    masking = privacy.get("masking_policy") or {}
    if masking and column_is_pii(prop):
        out.append(CandidateTag(
            domain="PrivacyState",
            tag="RawPersonalData",
            confidence=1.0,
            source="privacy_state_default",
        ))

    return out


def candidates_from_llm_suggestion(
    suggestions: list[dict[str, Any]],
    *,
    source: str = "llm_judgment",
) -> list[CandidateTag]:
    """Wrap LLM-proposed tags as suggest-only candidates."""
    out: list[CandidateTag] = []
    for item in suggestions:
        domain = str(item.get("domain") or "")
        tag = str(item.get("tag") or "")
        if not domain or not tag:
            continue
        out.append(CandidateTag(
            domain=domain,
            tag=tag,
            confidence=float(item.get("confidence") or 0.5),
            attributes=dict(item.get("attributes") or {}),
            source=source,
            suggest_only=True,
        ))
    return out


def iter_contract_columns(
    contract: dict,
    table: str,
) -> Iterator[tuple[str, dict]]:
    """Yield (column_name, property_dict) pairs from a contract."""
    for col in iter_columns(contract, table):
        for prop in _schema_properties(contract, table):
            if prop.get("name") == col.name:
                yield col.name, prop
                break


def _schema_properties(contract: dict, table: str) -> list[dict]:
    for obj in contract.get("schema", []) or []:
        if isinstance(obj, dict):
            pn = obj.get("physicalName") or ""
            if pn == table or not pn:
                return [p for p in (obj.get("properties") or []) if isinstance(p, dict)]
    return []


class ClassifierEnsemble:
    """Aggregate evidence sources for one column."""

    def __init__(self, policy: ClassificationPolicy):
        self.policy = policy

    def collect(
        self,
        prop: dict,
        *,
        llm_suggestions: Optional[list[dict[str, Any]]] = None,
        memory_suggestions: Optional[list[dict[str, Any]]] = None,
        learned_suggestions: Optional[list[dict[str, Any]]] = None,
    ) -> list[CandidateTag]:
        out = candidates_from_contract_column(prop, policy=self.policy)
        if llm_suggestions:
            out.extend(candidates_from_llm_suggestion(llm_suggestions))
        if memory_suggestions:
            out.extend(candidates_from_llm_suggestion(
                memory_suggestions, source="memory_retrieval",
            ))
        if learned_suggestions:
            out.extend(candidates_from_llm_suggestion(
                learned_suggestions, source="learned_classifier",
            ))
        return _dedupe_candidates(out)


def _dedupe_candidates(candidates: list[CandidateTag]) -> list[CandidateTag]:
    best: dict[str, CandidateTag] = {}
    for cand in candidates:
        key = cand.ref().key()
        prev = best.get(key)
        if prev is None:
            best[key] = cand
            continue
        # Prefer hard (non-suggest_only) over suggest-only at equal/higher confidence.
        if prev.suggest_only and not cand.suggest_only:
            best[key] = cand
            continue
        if (not prev.suggest_only) and cand.suggest_only:
            continue
        if cand.confidence > prev.confidence:
            best[key] = cand
    return list(best.values())
