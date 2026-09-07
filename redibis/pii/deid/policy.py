"""De-identification policy — general rule + per-entity overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Union

STRATEGIES = ("redact", "mask", "hash", "fpe", "fake", "passthrough")
DEID_API_VERSION = "redibis.io/deid-policy/v1"
PathLike = Union[str, Path]


@dataclass(frozen=True)
class EntityRule:
    entity_type: str  # "*" = general/default
    strategy: str = "redact"
    params: Mapping[str, Any] = field(default_factory=dict)
    min_score: float = 0.0
    replacement: str = ""

    def to_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "strategy": self.strategy,
            "params": dict(self.params),
            "min_score": self.min_score,
            "replacement": self.replacement,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EntityRule":
        return cls(
            entity_type=str(d.get("entity_type") or "*"),
            strategy=str(d.get("strategy") or "redact"),
            params=dict(d.get("params") or {}),
            min_score=float(d.get("min_score") or 0.0),
            replacement=str(d.get("replacement") or ""),
        )


@dataclass(frozen=True)
class DeidPolicy:
    id: str
    version: str = "1.0.0"
    default: EntityRule = field(
        default_factory=lambda: EntityRule(entity_type="*", strategy="redact")
    )
    overrides: tuple[EntityRule, ...] = ()
    on_overlap: str = "priority"
    unknown_entity: str = "redact"
    locale: str = "en"
    checksum: str = ""
    api_version: str = DEID_API_VERSION

    def to_dict(self) -> dict:
        return {
            "apiVersion": self.api_version or DEID_API_VERSION,
            "id": self.id,
            "version": self.version,
            "default": self.default.to_dict(),
            "overrides": [o.to_dict() for o in self.overrides],
            "on_overlap": self.on_overlap,
            "unknown_entity": self.unknown_entity,
            "locale": self.locale,
            "checksum": self.checksum,
        }

    def to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_dict(cls, d: dict) -> "DeidPolicy":
        api = str(d.get("apiVersion") or d.get("api_version") or DEID_API_VERSION)
        if api and api != DEID_API_VERSION:
            # Accept unknown versions with a soft check — still require mapping shape.
            pass
        default_raw = d.get("default") or {"entity_type": "*", "strategy": "redact"}
        if isinstance(default_raw, str):
            default = EntityRule(entity_type="*", strategy=default_raw)
        else:
            default = EntityRule.from_dict(dict(default_raw))
            if default.entity_type != "*":
                default = EntityRule(
                    entity_type="*",
                    strategy=default.strategy,
                    params=default.params,
                    min_score=default.min_score,
                    replacement=default.replacement,
                )
        overrides = tuple(
            EntityRule.from_dict(o) for o in (d.get("overrides") or []) if isinstance(o, dict)
        )
        return cls(
            id=str(d.get("id") or "unnamed"),
            version=str(d.get("version") or "1.0.0"),
            default=default,
            overrides=overrides,
            on_overlap=str(d.get("on_overlap") or "priority"),
            unknown_entity=str(d.get("unknown_entity") or "redact"),
            locale=str(d.get("locale") or "en"),
            checksum=str(d.get("checksum") or ""),
            api_version=api or DEID_API_VERSION,
        )

    @classmethod
    def from_yaml(cls, text_or_path: PathLike) -> "DeidPolicy":
        """Load from a YAML string **or** a filesystem path."""
        import yaml

        path = Path(str(text_or_path))
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
        else:
            raw = str(text_or_path)
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError("DeidPolicy YAML must be a mapping")
        return cls.from_dict(data)

    @classmethod
    def redact_all(cls, policy_id: str = "full-redact") -> "DeidPolicy":
        return cls(
            id=policy_id,
            default=EntityRule(entity_type="*", strategy="redact"),
            unknown_entity="redact",
        )


def resolve_rule(policy: DeidPolicy, entity_type: str, score: float) -> EntityRule:
    """Per-entity override (if score ≥ min_score) else default, else unknown_entity."""
    et = (entity_type or "").upper()
    for rule in policy.overrides:
        if rule.entity_type.upper() == et and score >= rule.min_score:
            return rule
    if policy.default and policy.default.strategy:
        return policy.default
    return EntityRule(entity_type=et or "*", strategy=policy.unknown_entity or "redact")


def suggest_policy_from_detections(
    detections: list,
    *,
    policy_id: str = "suggested",
    locale: str = "en",
) -> DeidPolicy:
    """Pre-fill a DeidPolicy using masking.suggest_rule for each entity type."""
    from redibis.masking.plan import suggest_rule

    seen: dict[str, EntityRule] = {}
    for d in detections:
        et = getattr(d, "entity_type", None) or (d.get("entity_type") if isinstance(d, dict) else None)
        if not et or et in seen:
            continue
        col_rule = suggest_rule(et.lower(), et, detected=True, default_locale=locale)
        params = dict(col_rule.params or {})
        replacement = ""
        if col_rule.strategy == "redact":
            replacement = f"[{et}]"
            params.setdefault("replacement", replacement)
        seen[et] = EntityRule(
            entity_type=et,
            strategy=col_rule.strategy,
            params=params,
            replacement=replacement,
        )
    return DeidPolicy(
        id=policy_id,
        default=EntityRule(entity_type="*", strategy="redact", replacement="[REDACTED]"),
        overrides=tuple(seen.values()),
        locale=locale,
        unknown_entity="redact",
    )
