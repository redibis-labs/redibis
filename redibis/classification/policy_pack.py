"""Load and validate versioned ClassificationPolicy packs."""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from redibis.config import ConfigError


@dataclass
class TagSpec:
    domain: str
    tag: str
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    order: int = 0
    retention_days: Optional[int] = None
    apply_forbidden: bool = False


@dataclass
class ClassificationPolicy:
    """Declarative taxonomy + golden rules + co-tag matrix."""

    version: str
    name: str
    description: str = ""
    tags: dict[str, TagSpec] = field(default_factory=dict)
    domains: dict[str, list[str]] = field(default_factory=dict)
    golden_rules: list[dict[str, Any]] = field(default_factory=list)
    cotag_matrix: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    security_derivation: dict[str, str] = field(default_factory=dict)
    retention_table: dict[str, dict[str, Any]] = field(default_factory=dict)
    jurisdiction_cotags: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    jurisdiction_inference: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    role_routing: dict[str, list[str]] = field(default_factory=dict)
    entity_type_mapping: dict[str, dict[str, str]] = field(default_factory=dict)
    security_order: dict[str, int] = field(default_factory=dict)
    edge_rules: list[dict[str, Any]] = field(default_factory=list)

    def tag_spec(self, domain: str, tag: str) -> Optional[TagSpec]:
        return self.tags.get(f"{domain}:{tag}")

    def domain_tags(self, domain: str) -> list[str]:
        return list(self.domains.get(domain, []))

    def security_level(self, tag: str) -> int:
        return self.security_order.get(tag, -1)


_PACK_CACHE: dict[str, ClassificationPolicy] = {}
_BUILTIN_PREFIX = "__builtin__:"


def _tag_key(domain: str, tag: str) -> str:
    return f"{domain}:{tag}"


def _normalize_cotag_matrix(raw: dict) -> dict[str, list[dict[str, str]]]:
    """Flatten ``tag: {mandatory: [...]}`` → ``tag: [...]``."""
    out: dict[str, list[dict[str, str]]] = {}
    for tag_name, body in raw.items():
        if isinstance(body, dict) and "mandatory" in body:
            out[tag_name] = list(body["mandatory"] or [])
        elif isinstance(body, list):
            out[tag_name] = body
    return out


def _parse_pack(raw: dict) -> ClassificationPolicy:
    if not isinstance(raw, dict):
        raise ConfigError("classification policy pack must be a mapping")

    version = str(raw.get("version") or "")
    name = str(raw.get("name") or "")
    if not version or not name:
        raise ConfigError("classification policy pack requires version and name")

    tags: dict[str, TagSpec] = {}
    domains: dict[str, list[str]] = {}
    security_order: dict[str, int] = {}

    for domain, body in (raw.get("domains") or {}).items():
        domain_tags: list[str] = []
        tag_defs = (body or {}).get("tags") or {}
        for tag_name, spec in tag_defs.items():
            spec = spec or {}
            key = _tag_key(domain, tag_name)
            ts = TagSpec(
                domain=domain,
                tag=tag_name,
                attributes=dict(spec.get("attributes") or {}),
                order=int(spec.get("order", 0)),
                retention_days=spec.get("retention_days"),
                apply_forbidden=bool(spec.get("apply_forbidden", False)),
            )
            tags[key] = ts
            domain_tags.append(tag_name)
            if domain == "DataSecurity":
                security_order[tag_name] = ts.order
        domains[domain] = domain_tags

    from redibis.classification.edge_rules import validate_edge_rules

    edge_rules = validate_edge_rules(raw.get("edge_rules") or [])

    return ClassificationPolicy(
        version=version,
        name=name,
        description=str(raw.get("description") or ""),
        tags=tags,
        domains=domains,
        golden_rules=list(raw.get("golden_rules") or []),
        cotag_matrix=_normalize_cotag_matrix(raw.get("cotag_matrix") or {}),
        security_derivation=dict(raw.get("security_derivation") or {}),
        retention_table=dict(raw.get("retention_table") or {}),
        jurisdiction_cotags=dict(raw.get("jurisdiction_cotags") or {}),
        jurisdiction_inference=dict(raw.get("jurisdiction_inference") or {}),
        role_routing=dict(raw.get("role_routing") or {}),
        entity_type_mapping=dict(raw.get("entity_type_mapping") or {}),
        security_order=security_order,
        edge_rules=edge_rules,
    )


def load_policy_pack(path: Path) -> ClassificationPolicy:
    """Load a policy pack from a YAML file."""
    if not path.is_file():
        raise ConfigError(f"classification policy pack not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    policy = _parse_pack(raw)
    _PACK_CACHE[policy.name] = policy
    return policy


def get_builtin_pack(name: str = "telecom") -> ClassificationPolicy:
    """Load a packaged policy by name (default: telecom).

    Uses a dedicated cache key so user ``REDIBIS_PACK_DIR`` overlays /
    ``load_policy_pack`` entries cannot shadow the shipped builtin YAML.
    """
    cache_key = f"{_BUILTIN_PREFIX}{name}"
    if cache_key in _PACK_CACHE:
        return _PACK_CACHE[cache_key]
    pkg = importlib.resources.files("redibis.classification.packs")
    path = pkg / f"{name}.yaml"
    with importlib.resources.as_file(path) as real_path:
        policy = load_policy_pack(Path(real_path))
    # load_policy_pack also caches under policy.name — keep a builtin-only key.
    _PACK_CACHE[cache_key] = policy
    return policy


def list_builtin_packs() -> list[str]:
    """Return names of packaged policy packs."""
    pkg = importlib.resources.files("redibis.classification.packs")
    return sorted(
        p.stem for p in pkg.iterdir()
        if p.suffix in (".yaml", ".yml")
    )
