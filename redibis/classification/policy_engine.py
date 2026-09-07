"""Deterministic classification policy engine — LLM proposes, engine disposes."""

from __future__ import annotations

from typing import Optional

from redibis.classification.models import (
    CandidateTag,
    ClassificationContext,
    ClassificationResult,
    ResolvedTag,
    TagRef,
)
from redibis.classification.policy_pack import ClassificationPolicy


class PolicyEngine:
    """Apply golden rules, co-tag expansion, retention, and validation."""

    def __init__(self, policy: ClassificationPolicy):
        self.policy = policy

    def resolve(
        self,
        candidates: list[CandidateTag],
        context: ClassificationContext,
    ) -> ClassificationResult:
        result = ClassificationResult(table=context.table, column=context.column)
        selected: dict[str, ResolvedTag] = {}

        for cand in candidates:
            key = cand.ref().key()
            if cand.suggest_only:
                # Proposal / evidence only — never auto-apply into resolved tags.
                result.suggestions.append(cand)
                result.candidates_dropped.append(key)
                continue
            spec = self.policy.tag_spec(cand.domain, cand.tag)
            if spec and spec.apply_forbidden:
                result.escalations.append(
                    f"{key} detected — SecOps escalation required; tag must not be applied"
                )
                result.candidates_dropped.append(key)
                continue
            if key not in selected or cand.confidence > 0:
                selected[key] = ResolvedTag(
                    domain=cand.domain,
                    tag=cand.tag,
                    attributes=dict(cand.attributes),
                    source=cand.source,
                )

        self._expand_cotags(selected)
        self._apply_jurisdiction(selected, context.jurisdiction)
        self._derive_data_security(selected)
        self._apply_lifecycle_context(selected, context)
        self._apply_privacy_state(selected, context)
        self._apply_retention(selected, result, context)
        self._apply_golden_rules(selected, result)
        self._validate_required_attributes(selected, result)

        result.resolved_tags = list(selected.values())
        if result.escalations:
            result.approval_role = "SecOps"
        else:
            result.approval_role = self._route_approval(result.resolved_tags)
        return result

    def _expand_cotags(self, selected: dict[str, ResolvedTag]) -> None:
        for tag in list(selected.values()):
            mandatory = self.policy.cotag_matrix.get(tag.tag) or []
            for entry in mandatory:
                domain = entry["domain"]
                cotag = entry["tag"]
                key = f"{domain}:{cotag}"
                if key not in selected:
                    selected[key] = ResolvedTag(
                        domain=domain,
                        tag=cotag,
                        derived=True,
                        source=f"cotag:{tag.tag}",
                    )

    def apply_jurisdiction_inference(
        self,
        candidates: list[CandidateTag],
        jurisdiction: str,
        column_prop: dict,
    ) -> None:
        if not jurisdiction:
            return
        col_tags = {str(t).lower() for t in (column_prop.get("tags") or [])}
        for rule in self.policy.jurisdiction_inference.get(jurisdiction.upper(), []):
            when_any = {str(t).lower() for t in (rule.get("when_column_tags_any") or [])}
            if when_any and not (col_tags & when_any):
                continue
            spec = rule.get("candidate") or {}
            domain = str(spec.get("domain") or "")
            tag = str(spec.get("tag") or "")
            if not domain or not tag:
                continue
            candidates.append(CandidateTag(
                domain=domain,
                tag=tag,
                confidence=float(spec.get("confidence", 0.7)),
                source=str(spec.get("source") or f"jurisdiction_inference:{jurisdiction}"),
            ))

    def _apply_jurisdiction(
        self,
        selected: dict[str, ResolvedTag],
        jurisdiction: str,
    ) -> None:
        if not jurisdiction:
            return
        for entry in self.policy.jurisdiction_cotags.get(jurisdiction.upper(), []):
            key = f"{entry['domain']}:{entry['tag']}"
            if key not in selected:
                selected[key] = ResolvedTag(
                    domain=entry["domain"],
                    tag=entry["tag"],
                    derived=True,
                    source=f"jurisdiction:{jurisdiction}",
                )

    def _derive_data_security(self, selected: dict[str, ResolvedTag]) -> None:
        security_domain = "DataSecurity"
        existing = [t for t in selected.values() if t.domain == security_domain]
        if existing:
            return

        min_level = -1
        min_tag = ""
        for tag in selected.values():
            if tag.domain != "DataSensitivity":
                continue
            derived = self.policy.security_derivation.get(tag.tag)
            if not derived:
                continue
            level = self.policy.security_level(derived)
            if level > min_level:
                min_level = level
                min_tag = derived

        if min_tag:
            key = f"{security_domain}:{min_tag}"
            selected[key] = ResolvedTag(
                domain=security_domain,
                tag=min_tag,
                derived=True,
                source="security_derivation",
            )

    def _apply_lifecycle_context(
        self,
        selected: dict[str, ResolvedTag],
        context: ClassificationContext,
    ) -> None:
        if not context.lifecycle:
            return
        key = f"Lifecycle:{context.lifecycle}"
        if key not in selected and self.policy.tag_spec("Lifecycle", context.lifecycle):
            selected[key] = ResolvedTag(
                domain="Lifecycle",
                tag=context.lifecycle,
                source="context.lifecycle",
            )

    def _apply_privacy_state(
        self,
        selected: dict[str, ResolvedTag],
        context: ClassificationContext,
    ) -> None:
        if not context.privacy_state:
            return
        key = f"PrivacyState:{context.privacy_state}"
        if key not in selected and self.policy.tag_spec("PrivacyState", context.privacy_state):
            selected[key] = ResolvedTag(
                domain="PrivacyState",
                tag=context.privacy_state,
                source="context.privacy_state",
            )

    def _apply_retention(
        self,
        selected: dict[str, ResolvedTag],
        result: ClassificationResult,
        context: ClassificationContext,
    ) -> None:
        best_days: Optional[int] = None
        best_tag = ""

        for tag in selected.values():
            entry = self.policy.retention_table.get(tag.tag)
            if not entry:
                continue
            days = entry.get("days")
            if days is None:
                continue
            if best_days is None or days > best_days:
                best_days = days
                best_tag = str(entry.get("tag") or "")

        if best_tag and f"RetentionPolicy:{best_tag}" not in selected:
            selected[f"RetentionPolicy:{best_tag}"] = ResolvedTag(
                domain="RetentionPolicy",
                tag=best_tag,
                derived=True,
                source="retention_table",
            )
            result.retention_days = best_days
            result.retention_tag = best_tag

    def _apply_golden_rules(
        self,
        selected: dict[str, ResolvedTag],
        result: ClassificationResult,
    ) -> None:
        for rule in self.policy.golden_rules:
            kind = rule.get("rule")
            if kind == "exactly_one":
                domain = rule["domain"]
                tags = [t for t in selected.values() if t.domain == domain]
                if len(tags) != 1:
                    result.violations.append(
                        f"golden_rule:{rule['id']}: expected exactly one {domain} tag, "
                        f"got {len(tags)}"
                    )
            elif kind == "min_security_for_domain":
                trigger = rule["trigger_domain"]
                min_tag = rule["min_security"]
                if not any(t.domain == trigger for t in selected.values()):
                    continue
                sec_tags = [t for t in selected.values() if t.domain == "DataSecurity"]
                if not sec_tags:
                    result.violations.append(
                        f"golden_rule:{rule['id']}: {trigger} present but no DataSecurity tag"
                    )
                    continue
                level = self.policy.security_level(sec_tags[0].tag)
                min_level = self.policy.security_level(min_tag)
                if level < min_level:
                    result.violations.append(
                        f"golden_rule:{rule['id']}: DataSecurity {sec_tags[0].tag} "
                        f"< required {min_tag}"
                    )
            elif kind == "mutually_exclusive":
                present = [
                    t for t in rule.get("tags", [])
                    if t in selected
                ]
                if len(present) > 1:
                    result.violations.append(
                        f"golden_rule:{rule['id']}: mutually exclusive tags {present}"
                    )
            elif kind == "requires_when":
                when = rule.get("when_tag")
                req_domain = rule.get("requires_domain")
                if when in selected and not any(
                    t.domain == req_domain for t in selected.values()
                ):
                    result.violations.append(
                        f"golden_rule:{rule['id']}: {when} requires a {req_domain} tag"
                    )

    def _validate_required_attributes(
        self,
        selected: dict[str, ResolvedTag],
        result: ClassificationResult,
    ) -> None:
        for tag in selected.values():
            spec = self.policy.tag_spec(tag.domain, tag.tag)
            if not spec:
                result.flags.append(f"unknown_tag:{tag.ref().key()}")
                continue
            for attr, meta in spec.attributes.items():
                if meta.get("required") and attr not in tag.attributes:
                    result.violations.append(
                        f"missing_attribute:{tag.ref().key()}.{attr}"
                    )

    def _route_approval(self, tags: list[ResolvedTag]) -> str:
        """Return the highest-priority approval role for resolved tags."""
        priority = ["SecOps", "Legal", "DPO", "Architecture", "Steward"]
        matched: set[str] = set()
        for tag in tags:
            for role, patterns in self.policy.role_routing.items():
                for pattern in patterns:
                    if self._pattern_matches(pattern, tag):
                        matched.add(role)
        for role in priority:
            if role in matched:
                return role
        return "Steward"

    @staticmethod
    def _pattern_matches(pattern: str, tag: ResolvedTag) -> bool:
        if pattern.endswith(".*"):
            return tag.domain == pattern[:-2]
        if "." in pattern:
            domain, name = pattern.split(".", 1)
            return tag.domain == domain and tag.tag == name
        return tag.tag == pattern
