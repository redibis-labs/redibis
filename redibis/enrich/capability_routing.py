"""Central LiteLLM capability / model-role routing.

Every production and diagnostic model call should resolve through
``resolve_model_binding`` and execute through ``guarded_model_call``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Stable role ids — reserved even when a call site is still deterministic.
MODEL_ROLES: tuple[str, ...] = (
    "agent.planner",
    "agent.planner_repair",
    "agent.router",
    "agent.copilot",
    "contract.enrichment",
    "contract.enrichment_repair",
    "pii.refiner",
    "pii.text_refiner",
    "gateway.toxicity",
    "gateway.prompt_injection",
    "codegen.generator",
    "codegen.judge",
    "behavior.draft",
    "classification.judge",
    "pack.evaluator",
    "provider.probe",
)

_LOCAL_ONLY_ROLES = frozenset({
    "codegen.generator",
    "codegen.judge",
})

_DEFAULT_INHERIT: dict[str, str] = {
    "agent.planner_repair": "agent.planner",
    "agent.copilot": "agent.planner",
    "contract.enrichment_repair": "contract.enrichment",
}


@dataclass(frozen=True)
class ModelRoleBinding:
    """Resolved provider/model for one model role."""

    role: str
    enabled: bool = True
    provider: str = ""
    model: str = ""
    inherit: str = ""
    residency_policy: str = "any"  # local_only | private_or_local | any | masked_external
    fallback: str = "skip"  # heuristic | template | skip | fail
    source: str = "built_in"
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoutingSnapshot:
    """Immutable routing bound to a run at creation time."""

    revision: int
    checksum: str
    roles: dict[str, ModelRoleBinding]
    provider_registry_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "checksum": self.checksum,
            "provider_registry_digest": self.provider_registry_digest,
            "roles": {k: v.to_dict() for k, v in self.roles.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoutingSnapshot":
        roles_raw = raw.get("roles") or {}
        roles: dict[str, ModelRoleBinding] = {}
        for key, value in roles_raw.items():
            if not isinstance(value, dict):
                continue
            roles[str(key)] = ModelRoleBinding(
                role=str(value.get("role") or key),
                enabled=bool(value.get("enabled", True)),
                provider=str(value.get("provider") or ""),
                model=str(value.get("model") or ""),
                inherit=str(value.get("inherit") or ""),
                residency_policy=str(value.get("residency_policy") or "any"),
                fallback=str(value.get("fallback") or "skip"),
                source=str(value.get("source") or "snapshot"),
                params=dict(value.get("params") or {}),
            )
        return cls(
            revision=int(raw.get("revision") or 0),
            checksum=str(raw.get("checksum") or ""),
            roles=roles,
            provider_registry_digest=str(raw.get("provider_registry_digest") or ""),
        )


class RoutingError(ValueError):
    """Invalid capability routing configuration."""


def known_roles() -> list[str]:
    return list(MODEL_ROLES)


def default_binding(role: str) -> ModelRoleBinding:
    if role not in MODEL_ROLES:
        raise RoutingError(f"Unknown model role {role!r}")
    inherit = _DEFAULT_INHERIT.get(role, "")
    residency = "local_only" if role in _LOCAL_ONLY_ROLES else "any"
    fallback = {
        "agent.planner": "heuristic",
        "agent.planner_repair": "heuristic",
        "agent.router": "heuristic",
        "agent.copilot": "heuristic",
        "codegen.generator": "template",
        "codegen.judge": "skip",
        "contract.enrichment": "fail",
        "pii.refiner": "skip",
        "provider.probe": "fail",
        "pack.evaluator": "fail",
    }.get(role, "skip")
    return ModelRoleBinding(
        role=role,
        enabled=role not in {
            "agent.router",
            "codegen.judge",
            "behavior.draft",
            "classification.judge",
        },
        inherit=inherit,
        residency_policy=residency,
        fallback=fallback,
        source="built_in",
    )


def _normalize_roles(raw_roles: Optional[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, value in (raw_roles or {}).items():
        role = str(key).strip()
        if role not in MODEL_ROLES:
            raise RoutingError(f"Unknown model role {role!r}")
        if not isinstance(value, dict):
            raise RoutingError(f"Role {role!r} binding must be an object")
        out[role] = dict(value)
    return out


def _detect_inherit_cycles(roles: dict[str, ModelRoleBinding]) -> None:
    for start in roles:
        seen: set[str] = set()
        cur = start
        while True:
            if cur in seen:
                raise RoutingError(f"Inheritance cycle involving {start!r}")
            seen.add(cur)
            nxt = (roles.get(cur) or default_binding(cur)).inherit
            if not nxt or nxt == "default":
                break
            if nxt not in MODEL_ROLES and nxt not in roles:
                raise RoutingError(f"Role {cur!r} inherits unknown {nxt!r}")
            cur = nxt


def _legacy_role_maps(
    *,
    llm_defaults: Optional[dict[str, Any]] = None,
    agentic_defaults: Optional[dict[str, Any]] = None,
    pii_llm: Optional[dict[str, Any]] = None,
    agents_cfg: Any = None,
    codegen_local_provider: str = "",
) -> dict[str, dict[str, Any]]:
    """Map legacy config surfaces onto role overrides (compat period)."""
    overrides: dict[str, dict[str, Any]] = {}
    llm = llm_defaults if isinstance(llm_defaults, dict) else {}
    agentic = agentic_defaults if isinstance(agentic_defaults, dict) else {}
    pii = pii_llm if isinstance(pii_llm, dict) else {}

    if llm.get("provider"):
        overrides["contract.enrichment"] = {
            "provider": str(llm.get("provider") or "").strip(),
            "model": str(llm.get("model") or "").strip(),
            "enabled": bool(llm.get("enabled", True)),
            "source": "legacy.llm_defaults",
        }
        overrides["provider.probe"] = dict(overrides["contract.enrichment"])
        overrides["pack.evaluator"] = dict(overrides["contract.enrichment"])

    planner_provider = str(agentic.get("planner_provider") or "").strip()
    if not planner_provider and agents_cfg is not None:
        planner_provider = str(getattr(agents_cfg, "planner_provider", "") or "").strip()
    planner_model = str(agentic.get("planner_model") or "").strip()
    if not planner_model and agents_cfg is not None:
        planner_model = str(getattr(agents_cfg, "planner_model", "") or "").strip()
    mode = str(agentic.get("planner_mode") or "auto").strip().lower()
    if mode == "heuristic":
        overrides["agent.planner"] = {
            "provider": "",
            "model": "",
            "enabled": True,
            "fallback": "heuristic",
            "source": "legacy.agentic_defaults",
        }
    elif planner_provider:
        overrides["agent.planner"] = {
            "provider": planner_provider,
            "model": planner_model,
            "enabled": True,
            "source": "legacy.agentic_defaults",
        }
    elif mode == "auto" and llm.get("provider"):
        overrides["agent.planner"] = {
            "provider": str(llm.get("provider") or "").strip(),
            "model": str(llm.get("model") or "").strip(),
            "enabled": True,
            "source": "legacy.llm_defaults",
        }

    if pii.get("enabled") and pii.get("provider"):
        overrides["pii.refiner"] = {
            "provider": str(pii.get("provider") or "").strip(),
            "model": str(pii.get("model_name") or pii.get("model") or "").strip(),
            "enabled": True,
            "source": "legacy.pii.llm",
        }
        # ``pii.llm`` historically also feeds the free-text refiner
        # (``LlmTextRefiner`` / Text Gateway "Use LLM refiner"). Route it to
        # its own role so residency policy and Settings capability rows can
        # target free-text scanning independently of the column refiner.
        overrides["pii.text_refiner"] = dict(overrides["pii.refiner"])

    local = (codegen_local_provider or "").strip()
    if local:
        overrides["codegen.generator"] = {
            "provider": local,
            "model": "",
            "enabled": True,
            "residency_policy": "local_only",
            "fallback": "template",
            "source": "legacy.CODEGEN_LOCAL_PROVIDER",
        }
    return overrides


def build_role_bindings(
    *,
    role_overrides: Optional[dict[str, Any]] = None,
    default_provider: str = "",
    default_model: str = "",
    llm_defaults: Optional[dict[str, Any]] = None,
    agentic_defaults: Optional[dict[str, Any]] = None,
    pii_llm: Optional[dict[str, Any]] = None,
    agents_cfg: Any = None,
    codegen_local_provider: str = "",
) -> dict[str, ModelRoleBinding]:
    """Materialize effective bindings for every known role."""
    legacy = _legacy_role_maps(
        llm_defaults=llm_defaults,
        agentic_defaults=agentic_defaults,
        pii_llm=pii_llm,
        agents_cfg=agents_cfg,
        codegen_local_provider=codegen_local_provider,
    )
    explicit = _normalize_roles(role_overrides)
    merged: dict[str, dict[str, Any]] = {**legacy, **explicit}

    root_provider = default_provider or str((llm_defaults or {}).get("provider") or "").strip()
    root_model = default_model or str((llm_defaults or {}).get("model") or "").strip()

    roles: dict[str, ModelRoleBinding] = {}
    for role in MODEL_ROLES:
        base = default_binding(role)
        patch = merged.get(role) or {}
        provider = str(patch.get("provider") or "").strip()
        model = str(patch.get("model") or "").strip()
        inherit = str(patch.get("inherit") if "inherit" in patch else base.inherit).strip()
        if not provider and not inherit and root_provider and role not in {
            "agent.router",
            "codegen.judge",
            "behavior.draft",
            "classification.judge",
        }:
            # Root default fills empty roles that are enabled by default.
            if base.enabled:
                provider = root_provider
                model = model or root_model
                inherit = ""
        roles[role] = ModelRoleBinding(
            role=role,
            enabled=bool(patch.get("enabled", base.enabled)),
            provider=provider,
            model=model,
            inherit=inherit,
            residency_policy=str(
                patch.get("residency_policy") or base.residency_policy
            ).strip() or base.residency_policy,
            fallback=str(patch.get("fallback") or base.fallback).strip() or base.fallback,
            source=str(patch.get("source") or ("configured" if patch else base.source)),
            params=dict(patch.get("params") or base.params),
        )
    _detect_inherit_cycles(roles)
    return roles


def resolve_model_binding(
    role: str,
    *,
    bindings: Optional[dict[str, ModelRoleBinding]] = None,
    snapshot: Optional[RoutingSnapshot] = None,
    override_provider: str = "",
    override_model: str = "",
) -> ModelRoleBinding:
    """Resolve one role, honoring inherit chains and run snapshots."""
    if role not in MODEL_ROLES:
        raise RoutingError(f"Unknown model role {role!r}")

    table = (
        snapshot.roles if snapshot is not None
        else (bindings or {r: default_binding(r) for r in MODEL_ROLES})
    )
    if override_provider:
        base = table.get(role) or default_binding(role)
        return ModelRoleBinding(
            role=role,
            enabled=True,
            provider=override_provider.strip(),
            model=(override_model or "").strip(),
            inherit="",
            residency_policy=base.residency_policy,
            fallback=base.fallback,
            source="request",
            params=dict(base.params),
        )

    seen: set[str] = set()
    cur = role
    while True:
        if cur in seen:
            raise RoutingError(f"Inheritance cycle involving {role!r}")
        seen.add(cur)
        binding = table.get(cur) or default_binding(cur)
        if binding.provider:
            if cur == role:
                return binding
            return ModelRoleBinding(
                role=role,
                enabled=binding.enabled,
                provider=binding.provider,
                model=binding.model,
                inherit=binding.role if binding.role != role else "",
                residency_policy=(
                    (table.get(role) or default_binding(role)).residency_policy
                ),
                fallback=(table.get(role) or default_binding(role)).fallback,
                source=f"inherit:{binding.source}",
                params=dict((table.get(role) or default_binding(role)).params or binding.params),
            )
        nxt = binding.inherit
        if not nxt or nxt == "default":
            # Fall through to empty binding with role policy.
            root = table.get(role) or default_binding(role)
            return ModelRoleBinding(
                role=role,
                enabled=root.enabled,
                provider="",
                model="",
                inherit=root.inherit,
                residency_policy=root.residency_policy,
                fallback=root.fallback,
                source=root.source,
                params=dict(root.params),
            )
        cur = nxt


def assert_residency_allowed(binding: ModelRoleBinding, provider_residency: str) -> None:
    """Fail closed when a binding's residency policy rejects the provider."""
    policy = (binding.residency_policy or "any").strip().lower()
    residency = (provider_residency or "").strip().lower() or "local"
    if policy == "any":
        return
    if policy == "local_only" and residency != "local":
        raise RoutingError(
            f"Role {binding.role!r} requires local residency; got {residency!r}"
        )
    if policy == "private_or_local" and residency not in {"local", "private"}:
        raise RoutingError(
            f"Role {binding.role!r} requires private/local residency; got {residency!r}"
        )
    if policy == "masked_external" and residency == "public":
        # Allowed only with masked attestation at call time; residency itself ok.
        return


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def checksum_roles(roles: dict[str, ModelRoleBinding]) -> str:
    body = {k: roles[k].to_dict() for k in sorted(roles)}
    digest = hashlib.sha256(_stable_json(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def provider_registry_digest(providers: Optional[dict[str, Any]] = None) -> str:
    """Digest of provider registry metadata (no secrets)."""
    if not providers:
        try:
            from redibis.enrich.providers import load_provider_configs

            providers = load_provider_configs()
        except Exception:
            providers = {}
    redacted: dict[str, Any] = {}
    for name, cfg in sorted((providers or {}).items()):
        if not isinstance(cfg, dict):
            continue
        redacted[str(name)] = {
            "litellm_model": cfg.get("litellm_model") or "",
            "model_prefix": cfg.get("model_prefix") or "",
            "api_base": cfg.get("api_base") or "",
            "api_key_env": cfg.get("api_key_env") or "",
            "residency": cfg.get("residency") or "",
            "supports_json": bool(cfg.get("supports_json", True)),
        }
    digest = hashlib.sha256(_stable_json(redacted).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def llm_block_from_global(gs: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract the editable ``llm`` block from global settings (compat shapes)."""
    raw = gs if isinstance(gs, dict) else {}
    llm = raw.get("llm")
    if isinstance(llm, dict):
        return dict(llm)
    # Nested settings.llm from the design envelope.
    settings = raw.get("settings")
    if isinstance(settings, dict) and isinstance(settings.get("llm"), dict):
        return dict(settings["llm"])
    return {}


def role_overrides_from_llm_block(llm: Optional[dict[str, Any]]) -> dict[str, Any]:
    block = llm if isinstance(llm, dict) else {}
    roles = block.get("roles")
    return dict(roles) if isinstance(roles, dict) else {}


def default_from_llm_block(llm: Optional[dict[str, Any]]) -> tuple[str, str]:
    block = llm if isinstance(llm, dict) else {}
    default = block.get("default") if isinstance(block.get("default"), dict) else {}
    return (
        str(default.get("provider") or "").strip(),
        str(default.get("model") or "").strip(),
    )


def validate_role_overrides(role_overrides: Optional[dict[str, Any]]) -> None:
    """Validate role override objects without requiring full legacy context."""
    build_role_bindings(role_overrides=role_overrides or {})


def build_bindings_from_global(
    gs: Optional[dict[str, Any]] = None,
    *,
    agents_cfg: Any = None,
    codegen_local_provider: str = "",
) -> dict[str, ModelRoleBinding]:
    """Build effective role bindings from a global_settings dict (+ optional YAML agents)."""
    raw = gs if isinstance(gs, dict) else {}
    llm = llm_block_from_global(raw)
    default_provider, default_model = default_from_llm_block(llm)
    pii = raw.get("pii") if isinstance(raw.get("pii"), dict) else {}
    pii_llm = pii.get("llm") if isinstance(pii.get("llm"), dict) else None
    return build_role_bindings(
        role_overrides=role_overrides_from_llm_block(llm),
        default_provider=default_provider,
        default_model=default_model,
        llm_defaults=raw.get("llm_defaults") if isinstance(raw.get("llm_defaults"), dict) else None,
        agentic_defaults=(
            raw.get("agentic_defaults") if isinstance(raw.get("agentic_defaults"), dict) else None
        ),
        pii_llm=pii_llm,
        agents_cfg=agents_cfg,
        codegen_local_provider=codegen_local_provider,
    )


def capture_routing_snapshot(
    gs: Optional[dict[str, Any]] = None,
    *,
    agents_cfg: Any = None,
    codegen_local_provider: str = "",
    providers: Optional[dict[str, Any]] = None,
) -> RoutingSnapshot:
    """Freeze current effective bindings for a new run."""
    raw = gs if isinstance(gs, dict) else {}
    revision = int(raw.get("revision") or 0)
    roles = build_bindings_from_global(
        raw,
        agents_cfg=agents_cfg,
        codegen_local_provider=codegen_local_provider,
    )
    return RoutingSnapshot(
        revision=revision,
        checksum=checksum_roles(roles),
        roles=roles,
        provider_registry_digest=provider_registry_digest(providers),
    )


def routes_public_envelope(gs: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Versioned editable envelope for GET /api/llm/routes."""
    raw = dict(gs) if isinstance(gs, dict) else {}
    llm = llm_block_from_global(raw)
    if "default" not in llm:
        legacy = raw.get("llm_defaults") if isinstance(raw.get("llm_defaults"), dict) else {}
        llm["default"] = {
            "provider": str(legacy.get("provider") or "").strip(),
            "model": str(legacy.get("model") or "").strip(),
        }
    if "roles" not in llm or not isinstance(llm.get("roles"), dict):
        llm["roles"] = {}
    revision = int(raw.get("revision") or 0)
    updated_at = str(raw.get("updated_at") or "")
    checksum = str(raw.get("checksum") or "")
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "revision": revision,
        "updated_at": updated_at,
        "checksum": checksum,
        "note": "Applies to new runs only; in-flight runs keep their routing snapshot.",
        "settings": {"llm": llm},
    }


def apply_routes_put(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    expected_revision: Optional[int] = None,
) -> dict[str, Any]:
    """Merge a routes PUT into global settings; bump revision; sync legacy defaults."""
    cur = dict(current or {})
    cur_rev = int(cur.get("revision") or 0)
    if expected_revision is not None and int(expected_revision) != cur_rev:
        raise RoutingError(
            f"revision mismatch: expected {expected_revision}, current {cur_rev}"
        )

    settings = incoming.get("settings") if isinstance(incoming.get("settings"), dict) else {}
    llm_in = settings.get("llm") if isinstance(settings.get("llm"), dict) else incoming.get("llm")
    if not isinstance(llm_in, dict):
        raise RoutingError("settings.llm object is required")

    default = llm_in.get("default") if isinstance(llm_in.get("default"), dict) else {}
    roles_in = llm_in.get("roles") if isinstance(llm_in.get("roles"), dict) else {}
    validate_role_overrides(roles_in)

    llm_block = {
        "default": {
            "provider": str(default.get("provider") or "").strip(),
            "model": str(default.get("model") or "").strip(),
        },
        "roles": dict(roles_in),
    }
    # Keep legacy keys in sync so older UI/CLI paths keep working.
    cur["llm"] = llm_block
    cur["llm_defaults"] = {
        **(cur.get("llm_defaults") if isinstance(cur.get("llm_defaults"), dict) else {}),
        "provider": llm_block["default"]["provider"],
        "model": llm_block["default"]["model"],
        "enabled": bool(
            (cur.get("llm_defaults") or {}).get("enabled", True)
            if isinstance(cur.get("llm_defaults"), dict)
            else True
        ),
    }
    planner = roles_in.get("agent.planner") if isinstance(roles_in.get("agent.planner"), dict) else {}
    if planner:
        agentic = dict(cur.get("agentic_defaults") or {}) if isinstance(cur.get("agentic_defaults"), dict) else {}
        if planner.get("provider"):
            agentic["planner_mode"] = "llm"
            agentic["planner_provider"] = str(planner.get("provider") or "").strip()
            agentic["planner_model"] = str(planner.get("model") or "").strip()
        elif str(planner.get("fallback") or "").strip().lower() == "heuristic":
            agentic["planner_mode"] = "heuristic"
            agentic["planner_provider"] = ""
            agentic["planner_model"] = ""
        cur["agentic_defaults"] = agentic

    new_rev = cur_rev + 1
    cur["schema_version"] = int(incoming.get("schema_version") or cur.get("schema_version") or 1)
    cur["revision"] = new_rev
    cur["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Checksum of the editable llm block (not full bindings, which include legacy).
    cur["checksum"] = (
        "sha256:"
        + hashlib.sha256(_stable_json(llm_block).encode("utf-8")).hexdigest()
    )
    return cur


def runtime_routes_matrix(
    gs: Optional[dict[str, Any]] = None,
    *,
    agents_cfg: Any = None,
    codegen_local_provider: str = "",
    credential_status: Optional[dict[str, bool]] = None,
) -> dict[str, Any]:
    """Redacted effective matrix for GET /api/llm/routes/runtime."""
    bindings = build_bindings_from_global(
        gs,
        agents_cfg=agents_cfg,
        codegen_local_provider=codegen_local_provider,
    )
    creds = credential_status or {}
    roles_out: dict[str, Any] = {}
    for role, binding in bindings.items():
        roles_out[role] = {
            **binding.to_dict(),
            "credential_ready": bool(creds.get(binding.provider)) if binding.provider else True,
        }
    raw = gs if isinstance(gs, dict) else {}
    return {
        "revision": int(raw.get("revision") or 0),
        "checksum": str(raw.get("checksum") or checksum_roles(bindings)),
        "roles": roles_out,
        "note": "Effective bindings for new runs; in-flight runs use their snapshot.",
    }


def get_provider_for_role(
    role: str,
    *,
    snapshot: Optional[RoutingSnapshot] = None,
    gs: Optional[dict[str, Any]] = None,
    agents_cfg: Any = None,
    override_provider: str = "",
    override_model: str = "",
    api_key: Optional[str] = None,
    endpoint_url: Optional[str] = None,
) -> tuple[Any, ModelRoleBinding]:
    """Resolve a role to an ``EnrichmentProvider`` instance (or raise ``RoutingError``)."""
    from redibis.enrich.providers import get_provider, get_provider_config

    if snapshot is not None:
        binding = resolve_model_binding(
            role,
            snapshot=snapshot,
            override_provider=override_provider,
            override_model=override_model,
        )
    else:
        bindings = build_bindings_from_global(gs, agents_cfg=agents_cfg)
        binding = resolve_model_binding(
            role,
            bindings=bindings,
            override_provider=override_provider,
            override_model=override_model,
        )
    if not binding.enabled:
        raise RoutingError(f"Role {role!r} is disabled")
    if not binding.provider:
        raise RoutingError(f"No provider bound for role {role!r}")
    cfg = get_provider_config(binding.provider)
    assert_residency_allowed(binding, str(cfg.get("residency") or "local"))
    provider = get_provider(
        binding.provider,
        model=binding.model or "",
        api_key=api_key,
        endpoint_url=endpoint_url,
    )
    return provider, binding
