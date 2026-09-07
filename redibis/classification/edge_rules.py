"""Declarative edge-case classification rules — safe evaluator, no code execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

import pandas as pd

from redibis.classification.policy_pack import ClassificationPolicy
from redibis.config import ConfigError
from redibis.models import PIIDetection, canonical_entity

_MAX_REGEX_LEN = 256
_CHECKSUM_PASS_RATE = 0.5
_MAX_RULES = 500
_MAX_STRING_LEN = 512
_MAX_TAGS = 50
_MAX_TAG_LEN = 128
_MAX_REGEX_GROUPS = 20

ALLOWED_WHEN_KEYS = frozenset({
    "name_matches",
    "entity",
    "logical_type",
    "is_numeric",
    "is_integer",
    "in_range",
    "cardinality_ratio_gte",
    "cardinality_ratio_lte",
    "null_rate_lte",
    "valid_rate_gte",
    "luhn_checksum",
    "iccid_checksum",
    "nid_checksum",
    "context_hit",
    "region_in",
})

ALLOWED_THEN_KEYS = frozenset({
    "set_entity",
    "forbid_entity",
    "set_classification",
    "set_masking_policy",
    "require_review",
    "add_tags",
    "note",
})


@dataclass
class EdgeRuleColumnContext:
    """Column semantics available to edge rules — never individual cell values."""

    column: str
    entity: Optional[str] = None
    raw_entity: Optional[str] = None
    logical_type: Optional[str] = None
    is_numeric: bool = False
    is_integer: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    cardinality_ratio: Optional[float] = None
    null_rate: Optional[float] = None
    valid_rates: dict[str, float] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    context_hit: Optional[bool] = None
    regions: list[str] = field(default_factory=list)
    detected: bool = False
    checksum_backed: bool = False


@dataclass
class RefineResult:
    """Outcome of evaluating edge rules against one column context."""

    matched_rule_id: Optional[str] = None
    set_entity: Optional[str] = None
    forbid_entity: Optional[str] = None
    set_classification: Optional[str] = None
    set_masking_policy: Optional[str] = None
    require_review: bool = False
    add_tags: list[str] = field(default_factory=list)
    note: str = ""


def _validate_regex(pattern: str, *, field_name: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"{field_name}: regex pattern must be a non-empty string")
    if len(pattern) > _MAX_REGEX_LEN:
        raise ConfigError(f"{field_name}: regex pattern exceeds {_MAX_REGEX_LEN} chars")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{field_name}: invalid regex: {exc}") from exc


_ALLOWED_RULE_KEYS = frozenset({"id", "when", "then", "priority", "terminal"})

_RATE_WHEN_KEYS = frozenset({
    "cardinality_ratio_gte", "cardinality_ratio_lte",
    "null_rate_lte", "valid_rate_gte",
})
_BOOL_WHEN_KEYS = frozenset({"is_numeric", "is_integer", "context_hit"})


def _validate_rate(value: Any, field: str) -> None:
    """Reject anything that is not a float in [0, 1]."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{field} must be a number, got {type(value).__name__}")
    v = float(value)
    if not (0.0 <= v <= 1.0):
        raise ConfigError(f"{field} must be in [0, 1], got {v!r}")


def _validate_bool_field(value: Any, field: str) -> None:
    """Reject non-bool values — truthy strings like 'true' are not acceptable."""
    if not isinstance(value, bool):
        raise ConfigError(
            f"{field} must be a boolean (true/false), got {type(value).__name__} {value!r}"
        )


def _validate_in_range(value: Any, field: str) -> None:
    """Validate that in_range is a two-element [lo, hi] with finite lo <= hi."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{field} must be a list of two numbers [lo, hi]")
    lo_raw, hi_raw = value[0], value[1]
    if not isinstance(lo_raw, (int, float)) or isinstance(lo_raw, bool):
        raise ConfigError(f"{field}[0] (lo) must be a finite number")
    if not isinstance(hi_raw, (int, float)) or isinstance(hi_raw, bool):
        raise ConfigError(f"{field}[1] (hi) must be a finite number")
    import math
    lo, hi = float(lo_raw), float(hi_raw)
    if not math.isfinite(lo):
        raise ConfigError(f"{field}[0] (lo) must be finite")
    if not math.isfinite(hi):
        raise ConfigError(f"{field}[1] (hi) must be finite")
    if lo > hi:
        raise ConfigError(f"{field} lo ({lo}) must be <= hi ({hi})")


def _validate_tags(value: Any, field: str) -> None:
    """Validate add_tags is a list of bounded non-empty strings."""
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list of strings")
    if len(value) > _MAX_TAGS:
        raise ConfigError(f"{field} exceeds maximum tag count ({_MAX_TAGS})")
    for i, tag in enumerate(value):
        if not isinstance(tag, str) or not tag.strip():
            raise ConfigError(f"{field}[{i}] must be a non-empty string")
        if len(tag) > _MAX_TAG_LEN:
            raise ConfigError(
                f"{field}[{i}] exceeds maximum tag length ({_MAX_TAG_LEN})"
            )


def _validate_string_field(value: Any, field: str) -> None:
    """Validate a string field is non-empty and within length limit."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    if len(value) > _MAX_STRING_LEN:
        raise ConfigError(f"{field} exceeds maximum string length ({_MAX_STRING_LEN})")


def validate_edge_rules(raw: list[Any]) -> list[dict[str, Any]]:
    """Validate edge_rules list from a pack; fail closed on unknown keys and bad values."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("edge_rules must be a list")
    if len(raw) > _MAX_RULES:
        raise ConfigError(f"edge_rules exceeds maximum rule count ({_MAX_RULES})")

    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for idx, rule in enumerate(raw):
        if not isinstance(rule, dict):
            raise ConfigError(f"edge_rules[{idx}] must be a mapping")

        # --- rule-level unknown keys ---
        unknown_rule = set(rule) - _ALLOWED_RULE_KEYS
        if unknown_rule:
            raise ConfigError(
                f"edge_rules[{idx}] has unknown keys: {sorted(unknown_rule)}"
            )

        rid = rule.get("id")
        if not rid or not isinstance(rid, str):
            raise ConfigError(f"edge_rules[{idx}] requires a string id")
        if len(rid) > _MAX_STRING_LEN:
            raise ConfigError(f"edge_rules[{idx}].id exceeds maximum length")
        if rid in seen_ids:
            raise ConfigError(f"duplicate edge_rules id: {rid!r}")
        seen_ids.add(rid)

        when = rule.get("when")
        if not isinstance(when, dict) or not when:
            raise ConfigError(f"edge_rules[{rid}].when must be a non-empty mapping")
        unknown_when = set(when) - ALLOWED_WHEN_KEYS
        if unknown_when:
            raise ConfigError(
                f"edge_rules[{rid}].when has unknown keys: {sorted(unknown_when)}"
            )
        if "name_matches" in when:
            _validate_regex(str(when["name_matches"]), field_name=f"edge_rules[{rid}].name_matches")

        # boolean when-keys must be actual bools
        for bk in _BOOL_WHEN_KEYS:
            if bk in when:
                _validate_bool_field(when[bk], f"edge_rules[{rid}].when.{bk}")

        # rate when-keys must be float in [0, 1]
        for rk in _RATE_WHEN_KEYS:
            if rk in when:
                _validate_rate(when[rk], f"edge_rules[{rid}].when.{rk}")

        # in_range: two finite numbers with lo <= hi
        if "in_range" in when:
            _validate_in_range(when["in_range"], f"edge_rules[{rid}].when.in_range")

        # checksum keys must be 'pass' or 'fail'
        for ck in ("luhn_checksum", "iccid_checksum", "nid_checksum"):
            if ck in when and when[ck] not in ("pass", "fail"):
                raise ConfigError(f"edge_rules[{rid}].when.{ck} must be 'pass' or 'fail'")

        # region_in must be a list or string
        if "region_in" in when:
            rv = when["region_in"]
            if not isinstance(rv, (list, str)) or (isinstance(rv, list) and not rv):
                raise ConfigError(
                    f"edge_rules[{rid}].when.region_in must be a non-empty string or list"
                )

        then = rule.get("then")
        if not isinstance(then, dict) or not then:
            raise ConfigError(f"edge_rules[{rid}].then must be a non-empty mapping")
        unknown_then = set(then) - ALLOWED_THEN_KEYS
        if unknown_then:
            raise ConfigError(
                f"edge_rules[{rid}].then has unknown keys: {sorted(unknown_then)}"
            )

        # require_review must be bool if present
        if "require_review" in then:
            _validate_bool_field(then["require_review"], f"edge_rules[{rid}].then.require_review")

        # string then-keys must be non-empty strings within length limits
        for sk in ("set_entity", "forbid_entity", "set_classification", "set_masking_policy"):
            if sk in then:
                _validate_string_field(then[sk], f"edge_rules[{rid}].then.{sk}")

        # add_tags must be a list of bounded non-empty strings
        if "add_tags" in then:
            _validate_tags(then["add_tags"], f"edge_rules[{rid}].then.add_tags")

        # note must be a string within length limits
        if "note" in then:
            if not isinstance(then["note"], str):
                raise ConfigError(f"edge_rules[{rid}].then.note must be a string")
            if len(then["note"]) > _MAX_STRING_LEN:
                raise ConfigError(
                    f"edge_rules[{rid}].then.note exceeds maximum length ({_MAX_STRING_LEN})"
                )

        out.append(dict(rule))
    return out


def merge_edge_rules(base: list[dict], overlay: list[dict]) -> list[dict]:
    """Overlay rules replace base rules with the same id; new ids are appended."""
    if not overlay:
        return list(base)
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for rule in base:
        rid = rule.get("id")
        if not rid:
            continue
        by_id[rid] = dict(rule)
        order.append(rid)
    for rule in overlay:
        rid = rule.get("id")
        if not rid:
            raise ConfigError("overlay edge_rules entries require an id")
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = dict(rule)
    return [by_id[rid] for rid in order]


def merge_policy_edge_rules(
    policy: ClassificationPolicy,
    overlay: Optional[list[dict]],
) -> ClassificationPolicy:
    if not overlay:
        return policy
    merged = merge_edge_rules(policy.edge_rules, overlay)
    return replace(policy, edge_rules=merged)


def _safe_name_match(pattern: str, column: str) -> bool:
    compiled = _validate_regex(pattern, field_name="name_matches")
    return bool(compiled.search(column))


def _checksum_status(pass_rate: float, tested: bool) -> Optional[str]:
    if not tested:
        return None
    return "pass" if pass_rate >= _CHECKSUM_PASS_RATE else "fail"


def build_edge_context(
    detection: PIIDetection,
    df: Optional[pd.DataFrame] = None,
    *,
    column_profile: Any = None,
) -> EdgeRuleColumnContext:
    """Build column-semantics context from a PIIDetection (+ optional frame/profile)."""
    col = detection.column
    raw_entity = detection.entity_type
    entity = canonical_entity(detection.entity_type)
    logical_type: Optional[str] = None
    is_numeric = False
    is_integer = False
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    cardinality_ratio: Optional[float] = None
    null_rate: Optional[float] = None

    if column_profile is not None:
        logical_type = getattr(column_profile, "logical_type", None) or getattr(
            column_profile, "dtype", None
        )
        cardinality_ratio = getattr(column_profile, "cardinality_ratio", None)
        null_rate = getattr(column_profile, "null_rate", None)
        lt = str(logical_type or "").lower()
        is_numeric = lt in ("integer", "float", "number", "double", "decimal")
        is_integer = lt == "integer"

    if df is not None and col in df.columns:
        series = df[col]
        if logical_type is None:
            if pd.api.types.is_numeric_dtype(series):
                is_numeric = True
                is_integer = pd.api.types.is_integer_dtype(series)
                logical_type = "integer" if is_integer else "float"
            else:
                logical_type = "string"
        if is_numeric:
            non_null = pd.to_numeric(series, errors="coerce").dropna()
            if len(non_null):
                min_val = float(non_null.min())
                max_val = float(non_null.max())
        if null_rate is None and len(series):
            null_rate = float(series.isna().mean())
        if cardinality_ratio is None and len(series):
            cardinality_ratio = float(series.nunique(dropna=True) / len(series))

    checksums: dict[str, str] = {}
    checksum_backed = False
    if df is not None and col in df.columns:
        from redibis.pii.telecom_signals import checksum_pass_rates

        str_values = [str(v) for v in df[col].dropna().head(500).tolist()]
        rates = checksum_pass_rates(str_values)
        luhn_tested = any(
            len(re.sub(r"\D", "", str(v))) == 15 for v in str_values if v
        )
        iccid_tested = any(
            re.sub(r"\D", "", str(v)).startswith("89")
            and 17 <= len(re.sub(r"\D", "", str(v))) <= 20
            for v in str_values
            if v
        )
        nid_tested = any(len(re.sub(r"\D", "", str(v))) == 14 for v in str_values if v)
        luhn_st = _checksum_status(rates.get("luhn_pass_rate", 0.0), luhn_tested) or "fail"
        iccid_st = _checksum_status(rates.get("iccid_pass_rate", 0.0), iccid_tested) or "fail"
        nid_st = _checksum_status(rates.get("egypt_nid_pass_rate", 0.0), nid_tested) or "fail"
        checksums["luhn_checksum"] = luhn_st
        checksums["iccid_checksum"] = iccid_st
        checksums["nid_checksum"] = nid_st
        checksum_backed = any(v == "pass" for v in checksums.values())

    valid_rates: dict[str, float] = {}
    if detection.msisdn_valid_rate is not None:
        valid_rates["msisdn"] = float(detection.msisdn_valid_rate)
    if detection.phone_valid_rate is not None:
        valid_rates["phone"] = float(detection.phone_valid_rate)
    if detection.presidio_match_rate is not None:
        valid_rates["regex"] = float(detection.presidio_match_rate)
    if detection.gliner_match_rate is not None:
        valid_rates["ner"] = float(detection.gliner_match_rate)

    regions: list[str] = []
    if detection.phone_regions:
        regions = list(detection.phone_regions.keys())

    context_hit: Optional[bool] = None
    if detection.presidio_pattern:
        context_hit = True
    elif detection.triage_score and detection.triage_score >= 0.5:
        context_hit = True

    return EdgeRuleColumnContext(
        column=col,
        entity=entity,
        raw_entity=raw_entity,
        logical_type=str(logical_type) if logical_type else None,
        is_numeric=is_numeric,
        is_integer=is_integer,
        min_value=min_val,
        max_value=max_val,
        cardinality_ratio=cardinality_ratio,
        null_rate=null_rate,
        valid_rates=valid_rates,
        checksums=checksums,
        context_hit=context_hit,
        regions=regions,
        detected=bool(detection.detected),
        checksum_backed=checksum_backed,
    )


def _eval_when(ctx: EdgeRuleColumnContext, when: dict[str, Any]) -> bool:
    for key, expected in when.items():
        if key == "name_matches":
            if not _safe_name_match(str(expected), ctx.column):
                return False
        elif key == "entity":
            want = str(expected)
            if (
                canonical_entity(want) != ctx.entity
                and want != (ctx.raw_entity or "")
                and canonical_entity(want) != canonical_entity(ctx.raw_entity or "")
            ):
                return False
        elif key == "logical_type":
            if str(expected).lower() != str(ctx.logical_type or "").lower():
                return False
        elif key == "is_numeric":
            if bool(expected) != ctx.is_numeric:
                return False
        elif key == "is_integer":
            if bool(expected) != ctx.is_integer:
                return False
        elif key == "in_range":
            if not isinstance(expected, (list, tuple)) or len(expected) != 2:
                return False
            lo, hi = float(expected[0]), float(expected[1])
            if ctx.min_value is None or ctx.max_value is None:
                return False
            if ctx.min_value < lo or ctx.max_value > hi:
                return False
        elif key == "cardinality_ratio_gte":
            if ctx.cardinality_ratio is None or ctx.cardinality_ratio < float(expected):
                return False
        elif key == "cardinality_ratio_lte":
            if ctx.cardinality_ratio is None or ctx.cardinality_ratio > float(expected):
                return False
        elif key == "null_rate_lte":
            if ctx.null_rate is None or ctx.null_rate > float(expected):
                return False
        elif key == "valid_rate_gte":
            if not ctx.valid_rates:
                return False
            if max(ctx.valid_rates.values()) < float(expected):
                return False
        elif key in ("luhn_checksum", "iccid_checksum", "nid_checksum"):
            actual = ctx.checksums.get(key)
            if actual != str(expected):
                return False
        elif key == "context_hit":
            if ctx.context_hit is None or bool(expected) != ctx.context_hit:
                return False
        elif key == "region_in":
            if not ctx.regions:
                return False
            allowed = expected if isinstance(expected, list) else [expected]
            if not any(r in allowed for r in ctx.regions):
                return False
    return True


def _checksum_blocks_rule(ctx: EdgeRuleColumnContext, then: dict[str, Any]) -> bool:
    """Validators outrank name-based demotions — skip rule when checksum passes."""
    new_entity = then.get("set_entity")
    if not new_entity or not ctx.checksum_backed:
        return False
    if canonical_entity(str(new_entity)) == ctx.entity:
        return False
    return True


def apply_edge_rules(
    ctx: EdgeRuleColumnContext,
    policy: ClassificationPolicy,
) -> RefineResult:
    """First matching rule wins (list order). No code execution — fixed handlers only."""
    for rule in policy.edge_rules:
        when = rule.get("when") or {}
        then = rule.get("then") or {}
        if not _eval_when(ctx, when):
            continue
        if _checksum_blocks_rule(ctx, then):
            continue
        return RefineResult(
            matched_rule_id=str(rule.get("id") or ""),
            set_entity=then.get("set_entity"),
            forbid_entity=then.get("forbid_entity"),
            set_classification=then.get("set_classification"),
            set_masking_policy=then.get("set_masking_policy"),
            require_review=bool(then.get("require_review", False)),
            add_tags=list(then.get("add_tags") or []),
            note=str(then.get("note") or ""),
        )
    return RefineResult()


def refine_detection(
    detection: PIIDetection,
    result: RefineResult,
    *,
    policy_name: str = "",
) -> PIIDetection:
    """Apply a RefineResult to a PIIDetection verdict.

    Consumed actions:
      set_entity        — override entity_type (NOT_PII → demote)
      forbid_entity     — demote to NOT_PII when current entity matches
      require_review    — flag llm_verdict as UNCERTAIN
      add_tags          — carry forward to edge_tags (consumed by classification)
      set_classification — carry forward to edge_classification
      set_masking_policy — carry forward to edge_masking_policy

    ``policy_name`` namespaces the matched rule id (``cr.<policy_name>.<rule_id>``)
    so it resolves into ``header.rules.custom_rules`` in the evidence bundle —
    see ``redibis.classification.evidence.classification_pack_stack_for_evidence``.
    """
    if not result.matched_rule_id:
        return detection

    path = detection.decision_path or ""
    trace = f"edge_rule:{result.matched_rule_id}"
    if result.note:
        trace += f" ({result.note})"
    decision_path = f"{path}; {trace}".strip("; ")

    edge_rule_ids = list(detection.edge_rule_ids or [])
    namespaced_id = f"cr.{policy_name}.{result.matched_rule_id}" if policy_name else f"cr.{result.matched_rule_id}"
    if namespaced_id not in edge_rule_ids:
        edge_rule_ids.append(namespaced_id)

    # forbid_entity: demote if current entity matches the forbidden one
    if result.forbid_entity:
        current = canonical_entity(detection.entity_type) or ""
        forbidden = canonical_entity(str(result.forbid_entity)) or ""
        if current and current == forbidden:
            return replace(
                detection,
                detected=False,
                entity_type=None,
                confidence=0.0,
                decision_path=decision_path,
                edge_rule_ids=edge_rule_ids,
            )

    if result.set_entity == "NOT_PII":
        return replace(
            detection,
            detected=False,
            entity_type=None,
            confidence=0.0,
            decision_path=decision_path,
            edge_rule_ids=edge_rule_ids,
        )

    updates: dict[str, Any] = {"decision_path": decision_path, "edge_rule_ids": edge_rule_ids}
    if result.set_entity:
        updates["entity_type"] = canonical_entity(str(result.set_entity))
        updates["detected"] = True
    if result.require_review:
        updates["llm_verdict"] = "UNCERTAIN"

    # Carry-forward fields — consumed by downstream classification/masking stages
    if result.add_tags:
        existing = list(detection.edge_tags or [])
        for tag in result.add_tags:
            if tag not in existing:
                existing.append(tag)
        updates["edge_tags"] = existing
    if result.set_classification:
        updates["edge_classification"] = str(result.set_classification)
    if result.set_masking_policy:
        updates["edge_masking_policy"] = str(result.set_masking_policy)

    return replace(detection, **updates)


def apply_edge_rules_to_detection(
    detection: PIIDetection,
    policy: ClassificationPolicy,
    *,
    df: Optional[pd.DataFrame] = None,
    column_profile: Any = None,
) -> tuple[PIIDetection, RefineResult]:
    if not policy.edge_rules:
        return detection, RefineResult()
    ctx = build_edge_context(detection, df, column_profile=column_profile)
    result = apply_edge_rules(ctx, policy)
    return refine_detection(detection, result, policy_name=policy.name), result


def suggest_rule_from_correction(
    *,
    column: str,
    from_entity: str,
    to_entity: str,
    note: str = "",
) -> dict[str, Any]:
    """Build an edge rule dict from a human-approved LLM correction."""
    tokens = [t for t in re.split(r"[_\s]+", column.lower()) if t]
    if len(tokens) > 1:
        name_pat = "(?i)" + "|".join(re.escape(t) for t in tokens)
    else:
        name_pat = f"(?i)\\b{re.escape(column)}\\b"
    slug = re.sub(r"[^a-z0-9]+", "_", column.lower()).strip("_") or "col"
    rid = f"promoted_{slug}_{from_entity.lower()}_to_{to_entity.lower()}"[:64]
    return {
        "id": rid,
        "when": {
            "name_matches": name_pat,
            "entity": from_entity,
        },
        "then": {
            "set_entity": to_entity,
            "note": note or f"Promoted from review: {column}",
        },
    }
