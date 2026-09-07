"""
redibis.behavior
=================
Framework-neutral Behavior Policy Runtime.

Public surface
--------------
  compile_policy    — parse/validate/normalize a policy document
  lint_policy       — structural and semantic lint before activation
  PolicyEvaluator   — priority-ordered rule evaluator with full trace
  PatchReducer      — capability-aware patch validator/reducer
  BehaviorRegistry  — fact/operator/effect registry
  get_builtin_registry — singleton built-in registry

Models:
  HookStage, TruthValue, Authority, PolicySource, FailMode, RuntimeMode
  AuthoredRule, CompiledRule, BehaviorPolicy, BehaviorPolicySet
  BehaviorContext, BehaviorCapabilities
  BehaviorPatch, PatchOperation
  EvaluationTrace, RuleTrace, BlockedOperation
  LintFinding, BehaviorPolicyWarning, PolicyAuditEvent

Configuration:
  BehaviorConfig

The package has no dependency on Presidio, GE, LangGraph, CopilotKit,
CLI code, or web code.
"""

from redibis.behavior.models import (
    Authority,
    AuthoredRule,
    BehaviorCapabilities,
    BehaviorContext,
    BehaviorPatch,
    BehaviorPolicy,
    BehaviorPolicySet,
    BehaviorPolicyWarning,
    BlockedOperation,
    CompiledRule,
    ConditionGroup,
    EffectCall,
    EvaluationTrace,
    FailMode,
    HookStage,
    LintFinding,
    LintSeverity,
    PatchOperation,
    PolicyAuditEvent,
    PolicySource,
    Predicate,
    RuleScope,
    RuleTrace,
    RuleTraceStatus,
    RuntimeMode,
    TruthValue,
    UnknownBehavior,
)

from redibis.behavior.compiler import compile_policy, compile_rules_for_evaluation
from redibis.behavior.lint import lint_policy, has_blocking_findings
from redibis.behavior.registry import (
    BehaviorRegistry,
    EffectDescriptor,
    FactDescriptor,
    OperatorDescriptor,
    get_builtin_registry,
)
from redibis.behavior.conditions import evaluate_condition, condition_matches
from redibis.behavior.evaluator import PolicyEvaluator, sort_rules
from redibis.behavior.reducer import PatchReducer
from redibis.behavior.config import BehaviorConfig
from redibis.behavior.plugins import apply_plugins_to_registry, load_plugins, plugin_manifest

__all__ = [
    # Enums
    "Authority", "FailMode", "HookStage", "LintSeverity", "PolicySource",
    "RuntimeMode", "RuleTraceStatus", "TruthValue", "UnknownBehavior",
    # Models
    "AuthoredRule", "BehaviorCapabilities", "BehaviorContext", "BehaviorPatch",
    "BehaviorPolicy", "BehaviorPolicySet", "BehaviorPolicyWarning",
    "BlockedOperation", "CompiledRule", "ConditionGroup", "EffectCall",
    "EvaluationTrace", "LintFinding", "PatchOperation", "PolicyAuditEvent",
    "Predicate", "RuleScope", "RuleTrace",
    # Functions
    "compile_policy", "compile_rules_for_evaluation",
    "condition_matches", "evaluate_condition",
    "has_blocking_findings", "lint_policy",
    "sort_rules",
    # Classes
    "BehaviorConfig", "BehaviorRegistry", "PolicyEvaluator", "PatchReducer",
    # Registry
    "EffectDescriptor", "FactDescriptor", "OperatorDescriptor",
    "get_builtin_registry",
    # Plugin SDK
    "apply_plugins_to_registry", "load_plugins", "plugin_manifest",
]
