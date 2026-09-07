"""Classification policy engine — multi-domain governed tagging."""

from redibis.classification.approval import ApprovalGate, ApprovalStatus, gate_for_result
from redibis.classification.edge_rules import (
    EdgeRuleColumnContext,
    RefineResult,
    apply_edge_rules,
    apply_edge_rules_to_detection,
    build_edge_context,
    merge_edge_rules,
    merge_policy_edge_rules,
    suggest_rule_from_correction,
    validate_edge_rules,
)
from redibis.classification.ensemble import ClassifierEnsemble
from redibis.classification.jurisdiction import JurisdictionContext
from redibis.classification.models import (
    CandidateTag,
    ClassificationContext,
    ClassificationResult,
    ResolvedTag,
    TagRef,
)
from redibis.classification.policy_engine import PolicyEngine
from redibis.classification.pack_digest import (
    build_classification_pack_digest,
    build_pack_digest,
)
from redibis.classification.policy_pack import (
    ClassificationPolicy,
    get_builtin_pack,
    list_builtin_packs,
    load_policy_pack,
)
from redibis.classification.reasoning import build_deterministic_reasoning
from redibis.classification.service import ClassificationService

__all__ = [
    "ApprovalGate",
    "ApprovalStatus",
    "CandidateTag",
    "ClassificationContext",
    "ClassificationPolicy",
    "ClassificationResult",
    "ClassificationService",
    "ClassifierEnsemble",
    "EdgeRuleColumnContext",
    "JurisdictionContext",
    "PolicyEngine",
    "RefineResult",
    "ResolvedTag",
    "TagRef",
    "apply_edge_rules",
    "apply_edge_rules_to_detection",
    "build_classification_pack_digest",
    "build_deterministic_reasoning",
    "build_edge_context",
    "build_pack_digest",
    "gate_for_result",
    "get_builtin_pack",
    "list_builtin_packs",
    "load_policy_pack",
    "merge_edge_rules",
    "merge_policy_edge_rules",
    "suggest_rule_from_correction",
    "validate_edge_rules",
]
