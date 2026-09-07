"""Target-system artifact validators for OSS local codegen."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

MAX_ARTIFACT_CHARS = 100_000

_RANGER_TOP_LEVEL = frozenset({"service", "table", "intent", "policies", "note"})
_RANGER_POLICY_KEYS = frozenset({
    "name", "resource", "column", "masking_policy", "entity_type", "effect",
})
_CODE_TARGETS = frozenset({"python", "pyspark", "script", "code"})


@dataclass
class ArtifactValidation:
    """Result of target-specific schema / structure validation."""

    passed: bool
    findings: list[dict[str, Any]] = field(default_factory=list)
    validator: str = ""
    parsed: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": list(self.findings),
            "validator": self.validator,
        }


ValidatorFn = Callable[[str], ArtifactValidation]

_REGISTRY: dict[str, ValidatorFn] = {}


def register_validator(target_system: str):
    """Decorator: ``@register_validator("ranger")``."""

    def _decorator(fn: ValidatorFn) -> ValidatorFn:
        _REGISTRY[target_system.strip().lower()] = fn
        return fn

    return _decorator


def emits_executable_code(target_system: str) -> bool:
    """True when Python-oriented SAST regexes are meaningful for this target."""
    return (target_system or "").strip().lower() in _CODE_TARGETS


def registered_targets() -> list[str]:
    """Sorted list of ``target_system`` keys with registered validators."""
    return sorted(_REGISTRY.keys())


def validate_artifact(source: str, target_system: str) -> ArtifactValidation:
    """Dispatch to the registered validator for ``target_system``."""
    key = (target_system or "").strip().lower() or "unknown"
    fn = _REGISTRY.get(key)
    if fn is None:
        return ArtifactValidation(
            passed=False,
            findings=[{
                "rule": "unknown_target",
                "severity": "high",
                "detail": f"no validator registered for target_system={target_system!r}",
            }],
            validator="none",
        )
    return fn(source or "")


def _fail(validator: str, *findings: dict[str, Any]) -> ArtifactValidation:
    return ArtifactValidation(passed=False, findings=list(findings), validator=validator)


@register_validator("ranger")
def validate_ranger(source: str) -> ArtifactValidation:
    """JSON parse → top-level keys → policies[] shape → bounded size."""
    name = "ranger-schema"
    if len(source) > MAX_ARTIFACT_CHARS:
        return _fail(name, {
            "rule": "size_limit",
            "severity": "high",
            "detail": f"artifact exceeds {MAX_ARTIFACT_CHARS} characters",
        })
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        return _fail(name, {
            "rule": "json_parse",
            "severity": "high",
            "detail": f"malformed JSON: {exc}",
        })
    if not isinstance(data, dict):
        return _fail(name, {
            "rule": "root_type",
            "severity": "high",
            "detail": "root must be a JSON object",
        })

    findings: list[dict[str, Any]] = []
    unexpected = set(data.keys()) - _RANGER_TOP_LEVEL
    if unexpected:
        findings.append({
            "rule": "unexpected_keys",
            "severity": "high",
            "detail": f"unexpected top-level keys: {sorted(unexpected)}",
        })
    for required in ("service", "table", "policies"):
        if required not in data:
            findings.append({
                "rule": "missing_key",
                "severity": "high",
                "detail": f"missing required key {required!r}",
            })
    if data.get("service") not in (None, "ranger"):
        findings.append({
            "rule": "service_value",
            "severity": "medium",
            "detail": f"service must be 'ranger', got {data.get('service')!r}",
        })

    policies = data.get("policies")
    if "policies" in data and not isinstance(policies, list):
        findings.append({
            "rule": "policies_type",
            "severity": "high",
            "detail": "policies must be a list",
        })
        policies = []

    for idx, policy in enumerate(policies or []):
        if not isinstance(policy, dict):
            findings.append({
                "rule": "policy_type",
                "severity": "high",
                "detail": f"policies[{idx}] must be an object",
            })
            continue
        bad_keys = set(policy.keys()) - _RANGER_POLICY_KEYS
        if bad_keys:
            findings.append({
                "rule": "policy_unexpected_keys",
                "severity": "medium",
                "detail": f"policies[{idx}] unexpected keys: {sorted(bad_keys)}",
            })
        for req in ("name", "column", "effect"):
            if not policy.get(req):
                findings.append({
                    "rule": "policy_missing_field",
                    "severity": "high",
                    "detail": f"policies[{idx}] missing {req!r}",
                })
        effect = policy.get("effect")
        if effect is not None and effect not in ("mask", "row_filter", "deny", "allow"):
            findings.append({
                "rule": "policy_effect",
                "severity": "medium",
                "detail": f"policies[{idx}] effect {effect!r} not in allowed set",
            })
        resource = policy.get("resource")
        if resource is not None:
            if not isinstance(resource, dict):
                findings.append({
                    "rule": "resource_type",
                    "severity": "high",
                    "detail": f"policies[{idx}].resource must be an object",
                })
            else:
                extra = set(resource.keys()) - {"database", "table", "column"}
                if extra:
                    findings.append({
                        "rule": "resource_keys",
                        "severity": "medium",
                        "detail": f"policies[{idx}].resource unexpected keys: {sorted(extra)}",
                    })

    if findings:
        return ArtifactValidation(passed=False, findings=findings, validator=name, parsed=data)
    return ArtifactValidation(passed=True, findings=[], validator=name, parsed=data)


@register_validator("text")
def validate_text(source: str) -> ArtifactValidation:
    """Bounded plain-text artifact (non-JSON targets)."""
    name = "text-bounded"
    if len(source) > MAX_ARTIFACT_CHARS:
        return _fail(name, {
            "rule": "size_limit",
            "severity": "high",
            "detail": f"artifact exceeds {MAX_ARTIFACT_CHARS} characters",
        })
    if not (source or "").strip():
        return _fail(name, {
            "rule": "empty",
            "severity": "high",
            "detail": "artifact is empty",
        })
    return ArtifactValidation(passed=True, findings=[], validator=name)
