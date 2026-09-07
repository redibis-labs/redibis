"""IntentPlanner — NL intent → registry-validated PipelineSpec (Phase 2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from redibis.agents.models import PipelineEdge, PipelineNode, PipelineSpec
from redibis.agents.registry import catalog_for_planner, validate_spec
from redibis.config import RAIConfig, RedibisConfig

_PLANNER_SYSTEM = """\
You are a data-governance pipeline planner for redibis.

Given natural-language intent, emit ONLY valid JSON (no markdown) describing a pipeline graph.
Use node kinds exclusively from the supplied catalog. Params must match each node's config_schema.

JSON shape:
{
  "name": "short_slug",
  "goal": "one sentence",
  "nodes": [
    {"kind": "<catalog kind>", "label": "human label", "params": {}}
  ],
  "edges": [
    {"source": 0, "target": 1}
  ]
}

Edge indices refer to positions in the nodes array (0-based). Wire outputs to compatible inputs.
Include approval gate before publish when the intent mentions contracts or publishing.
"""


@dataclass
class PlannerContext:
    """Optional grounding for constrained planning."""

    policy_pack: str = "telecom"
    database: str = ""
    jurisdiction: str = ""
    memory_recipes: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlannerResult:
    spec: PipelineSpec
    valid: bool
    errors: list[str] = field(default_factory=list)
    repaired: bool = False
    rai_report: Optional[dict[str, Any]] = None
    method: str = "unknown"
    provider: str = ""
    model: str = ""
    config_source: str = ""
    fallback_reason: str = ""


class IntentPlanner:
    """Turn NL intent into a validated ``PipelineSpec`` via ``guarded_model_call``."""

    def __init__(
        self,
        *,
        provider: Any = None,
        redibis_config: Optional[RedibisConfig] = None,
        model_call: Optional[Callable[..., tuple[str, Optional[dict]]]] = None,
    ):
        self.provider = provider
        self.config = redibis_config or RedibisConfig.default()
        self._model_call = model_call

    def plan(
        self,
        intent_text: str,
        context: Optional[PlannerContext] = None,
    ) -> PlannerResult:
        ctx = context or PlannerContext()
        catalog = catalog_for_planner()
        user_prompt = json.dumps({
            "intent": intent_text.strip(),
            "policy_pack": ctx.policy_pack,
            "database": ctx.database,
            "jurisdiction": ctx.jurisdiction,
            "memory_recipes": ctx.memory_recipes[:3],
            "node_catalog": catalog,
            **ctx.extra,
        }, indent=2)

        raw = self._call_model(user_prompt)
        spec, parse_errors = self._parse_response(raw, intent_text)
        rai_report = getattr(self, "_last_rai_report", None)
        if parse_errors:
            return PlannerResult(
                spec=spec,
                valid=False,
                errors=parse_errors,
                rai_report=rai_report,
            )

        validation = validate_spec(spec)
        if validation:
            repaired_spec, repair_errors, rai_report = self._repair(
                intent_text,
                spec,
                [e.message for e in validation],
                catalog=catalog,
            )
            if repair_errors:
                return PlannerResult(
                    spec=repaired_spec,
                    valid=False,
                    errors=repair_errors,
                    repaired=True,
                    rai_report=rai_report,
                )
            validation = validate_spec(repaired_spec)
            if validation:
                return PlannerResult(
                    spec=repaired_spec,
                    valid=False,
                    errors=[e.message for e in validation],
                    repaired=True,
                    rai_report=rai_report,
                )
            return PlannerResult(
                spec=repaired_spec,
                valid=True,
                repaired=True,
                rai_report=rai_report,
            )

        return PlannerResult(spec=spec, valid=True, rai_report=rai_report)

    def _call_model(self, user_prompt: str) -> str:
        if self._model_call is not None:
            raw, _ = self._model_call(user_prompt)
            return raw

        if self.provider is None:
            raise ValueError("IntentPlanner requires provider or model_call")

        from redibis.telemetry.llm_evidence import (
            current_llm_evidence_recorder,
            llm_evidence_recorder,
            new_llm_run_id,
        )
        from redibis.telemetry.model_gateway import guarded_model_call

        def _invoke() -> str:
            return self.provider.complete(_PLANNER_SYSTEM, user_prompt, json_mode=True)

        plan_run_id = new_llm_run_id("planner")

        def _call():
            return guarded_model_call(
                _invoke,
                model_id=getattr(self.provider, "model", None) or getattr(self.provider, "name", "planner"),
                provider=self.provider,
                user_prompt=user_prompt,
                system_prompt=_PLANNER_SYSTEM,
                residency=getattr(self.provider, "residency", "") or "local",
                redibis_config=self.config,
                model_role="agent.planner",
                run_id=plan_run_id,
            )

        if current_llm_evidence_recorder() is None:
            with llm_evidence_recorder(
                run_id=plan_run_id,
                table=str(getattr(self.config, "table", "") or ""),
                execution_mode="agentic",
                config=self.config,
            ):
                raw, rai_report = _call()
        else:
            raw, rai_report = _call()
        self._last_rai_report = rai_report
        return raw

    def _repair(
        self,
        intent_text: str,
        spec: PipelineSpec,
        errors: list[str],
        *,
        catalog: list[dict[str, Any]],
    ) -> tuple[PipelineSpec, list[str], Optional[dict[str, Any]]]:
        repair_prompt = json.dumps({
            "intent": intent_text,
            "invalid_spec": spec.to_dict(),
            "validation_errors": errors,
            "node_catalog": catalog,
        }, indent=2)

        if self._model_call is not None:
            raw, _ = self._model_call(repair_prompt)
            rai_report = None
        else:
            from redibis.telemetry.llm_evidence import (
                current_llm_evidence_recorder,
                llm_evidence_recorder,
                new_llm_run_id,
            )
            from redibis.telemetry.model_gateway import guarded_model_call

            def _invoke() -> str:
                return self.provider.complete(
                    _PLANNER_SYSTEM + "\nFix the invalid_spec so validation_errors are resolved.",
                    repair_prompt,
                    json_mode=True,
                )

            repair_run_id = new_llm_run_id("planner-repair")

            def _repair_call():
                return guarded_model_call(
                    _invoke,
                    model_id=getattr(self.provider, "model", None) or "planner-repair",
                    provider=self.provider,
                    user_prompt=repair_prompt,
                    system_prompt=_PLANNER_SYSTEM + "\nFix the invalid_spec so validation_errors are resolved.",
                    residency=getattr(self.provider, "residency", "") or "local",
                    redibis_config=self.config,
                    model_role="agent.planner_repair",
                    run_id=repair_run_id,
                )

            if current_llm_evidence_recorder() is None:
                with llm_evidence_recorder(
                    run_id=repair_run_id,
                    table=str(getattr(self.config, "table", "") or ""),
                    execution_mode="agentic",
                    config=self.config,
                ):
                    raw, rai_report = _repair_call()
            else:
                raw, rai_report = _repair_call()

        repaired, parse_errors = self._parse_response(raw, intent_text)
        return repaired, parse_errors, rai_report

    def _parse_response(
        self,
        raw: str,
        intent_text: str,
    ) -> tuple[PipelineSpec, list[str]]:
        errors: list[str] = []
        try:
            payload = json.loads(_extract_json(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            return PipelineSpec(name="invalid", goal=intent_text), [f"JSON parse failed: {exc}"]

        nodes_payload = payload.get("nodes") or []
        if not nodes_payload:
            errors.append("plan must include at least one node")

        nodes: list[PipelineNode] = []
        for item in nodes_payload:
            kind = str(item.get("kind") or "")
            if not kind:
                errors.append("node missing kind")
                continue
            nodes.append(PipelineNode(
                kind=kind,
                label=str(item.get("label") or kind),
                params=dict(item.get("params") or {}),
            ))

        edges: list[PipelineEdge] = []
        for item in payload.get("edges") or []:
            try:
                src_i = int(item["source"])
                tgt_i = int(item["target"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"invalid edge entry: {item!r}")
                continue
            if src_i < 0 or tgt_i < 0 or src_i >= len(nodes) or tgt_i >= len(nodes):
                errors.append(f"edge index out of range: {item!r}")
                continue
            edges.append(PipelineEdge(source=nodes[src_i].id, target=nodes[tgt_i].id))

        spec = PipelineSpec(
            name=str(payload.get("name") or "planned"),
            goal=str(payload.get("goal") or intent_text),
            nodes=nodes,
            edges=edges,
        )
        return spec, errors


def _extract_json(text: str) -> str:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        return fence.group(1).strip()
    return text


def _extract_table(intent_text: str) -> str:
    """Best-effort table name from NL intent (schema.table or bare identifier)."""
    _STOP = frozenset({
        "the", "a", "an", "this", "that", "my", "your", "our", "data", "dataset", "it",
    })
    dotted = re.search(r"\b([\w][\w-]*\.[\w][\w-]*)\b", intent_text)
    if dotted:
        return dotted.group(1)
    for pat in (
        r"(?:table|for|on)\s+[`'\"]?([\w][\w-]*)[`'\"]?",
        r"\b(onboard|scan|profile)\s+[`'\"]?([\w][\w-]*)[`'\"]?",
    ):
        m = re.search(pat, intent_text, re.IGNORECASE)
        if m:
            name = m.group(m.lastindex or 1)
            if name.lower() not in _STOP:
                return name
    return ""


@dataclass
class _HeuristicFlags:
    profile: bool = True
    pii: bool = True
    classify: bool = True
    quality: bool = True
    mask: bool = False
    gate: bool = False
    publish: bool = False


def _heuristic_flags(intent_text: str) -> _HeuristicFlags:
    text = intent_text.lower()
    flags = _HeuristicFlags()
    flags.pii = any(k in text for k in ("pii", "privacy", "presidio", "sensitive"))
    flags.classify = any(
        k in text for k in ("classify", "classification", "taxonomy", "atlas", "tag")
    )
    flags.quality = any(k in text for k in ("quality", "expectation", "great expectations", " ge "))
    flags.profile = any(
        k in text for k in ("profile", "structural", "onboard", "scan", "sample")
    ) or flags.pii or flags.classify or flags.quality
    flags.mask = any(k in text for k in ("mask", "masking", "de-ident", "anonym", "fpe"))
    flags.publish = any(
        k in text for k in ("publish", "openmetadata", "open metadata", " om ", "datahub", "catalog push")
    )
    flags.gate = flags.publish or any(
        k in text for k in ("approve", "approval", "steward", "gate", "hitl", "human")
    )
    if not any((flags.pii, flags.classify, flags.quality, flags.mask, flags.publish)):
        flags.pii = flags.classify = flags.quality = True
        flags.profile = True
    return flags


def _wire_standard_edges(nodes: list[PipelineNode]) -> list[PipelineEdge]:
    def iof(kind: str) -> int | None:
        for i, node in enumerate(nodes):
            if node.kind == kind:
                return i
        return None

    edges_idx: list[tuple[int, int]] = []
    src, smp, prof, con = iof("source"), iof("sample"), iof("profile"), iof("contract")
    mask, gate, pub = iof("mask"), iof("gate"), iof("publish")

    if src is not None and smp is not None:
        edges_idx.append((src, smp))
    if smp is not None and prof is not None:
        edges_idx.append((smp, prof))
    if smp is not None and con is not None:
        edges_idx.append((smp, con))
    if prof is not None and con is not None:
        edges_idx.append((prof, con))
    if con is not None and mask is not None:
        edges_idx.append((con, mask))
    if con is not None and gate is not None:
        edges_idx.append((con, gate))
    if gate is not None and pub is not None:
        edges_idx.append((gate, pub))
    elif con is not None and pub is not None and gate is None:
        edges_idx.append((con, pub))

    return [PipelineEdge(source=nodes[s].id, target=nodes[t].id) for s, t in edges_idx]


def heuristic_plan(
    intent_text: str,
    context: Optional[PlannerContext] = None,
) -> PlannerResult:
    """Deterministic NL → PipelineSpec (no LLM). Used by copilot when no provider is set."""
    ctx = context or PlannerContext()
    flags = _heuristic_flags(intent_text)
    table = _extract_table(intent_text) or ctx.database or "schema.table"
    slug = re.sub(r"[^\w]+", "_", table.split(".")[-1]).strip("_") or "pipeline"

    node_specs: list[tuple[str, str, dict[str, Any]]] = [
        ("source", "Source", {"engine": "hive", "table": table}),
        ("sample", "Sample", {"strategy": "percent", "amount": 10.0}),
    ]
    if flags.profile:
        node_specs.append(("profile", "Profile", {}))
    node_specs.append((
        "contract",
        "Contract",
        {
            "pii": flags.pii,
            "classify": flags.classify,
            "quality": flags.quality,
            "policy_pack": ctx.policy_pack,
        },
    ))
    if flags.mask:
        node_specs.append(("mask", "Mask", {}))
    if flags.gate:
        node_specs.append(("gate", "Approval Gate", {"role": "Steward"}))
    if flags.publish:
        node_specs.append(("publish", "Publish", {"backend": "openmetadata"}))

    # Contract node requires at least one scan facet enabled.
    for i, (kind, _label, params) in enumerate(node_specs):
        if kind != "contract":
            continue
        if not any(params.get(k) for k in ("pii", "classify", "mask_rules", "quality")):
            node_specs[i] = (kind, _label, {**params, "classify": True})
        break

    nodes = [PipelineNode(kind=k, label=label, params=params) for k, label, params in node_specs]
    spec = PipelineSpec(
        name=slug,
        goal=intent_text.strip()[:500] or f"Governance pipeline for {table}",
        nodes=nodes,
        edges=_wire_standard_edges(nodes),
    )
    validation = validate_spec(spec)
    return PlannerResult(
        spec=spec,
        valid=not validation,
        errors=[e.message for e in validation],
    )


def format_plan_reply(result: PlannerResult, *, method: str = "heuristic") -> str:
    """Human-readable copilot reply with an embeddable pipeline fence for the board UI."""
    spec = result.spec
    flow = " → ".join(n.kind for n in spec.nodes)
    lines = [
        f"Proposed pipeline ({method}):",
        f"Goal: {spec.goal}",
        f"Flow: {flow}",
    ]
    if not result.valid:
        lines.append(f"Validation notes: {'; '.join(result.errors)}")
    else:
        lines.append(
            "Review in Build view, or use Load on board below. "
            "Nothing runs until you execute from the board."
        )
    lines.append(f"\n```redibis-pipeline\n{json.dumps(spec.to_dict(), indent=2)}\n```")
    return "\n".join(lines)


def resolve_planner_provider(
    cfg: RedibisConfig,
    *,
    snapshot: Any = None,
    override_provider: str = "",
    override_model: str = "",
) -> Any | None:
    """Return an enrich provider for the planner role (snapshot-aware)."""
    try:
        from redibis.enrich.capability_routing import (
            RoutingSnapshot,
            build_bindings_from_global,
            resolve_model_binding,
        )
        from redibis.enrich.providers import get_provider
        from redibis.config import load_global_settings_optional

        snap = snapshot
        if isinstance(snap, dict):
            snap = RoutingSnapshot.from_dict(snap)
        if snap is None:
            bindings = build_bindings_from_global(
                load_global_settings_optional(),
                agents_cfg=cfg.agents,
            )
            binding = resolve_model_binding(
                "agent.planner",
                bindings=bindings,
                override_provider=override_provider,
                override_model=override_model,
            )
        else:
            binding = resolve_model_binding(
                "agent.planner",
                snapshot=snap,
                override_provider=override_provider,
                override_model=override_model,
            )
        name = (binding.provider or "").strip()
        if not name:
            # Legacy fallback: agents.planner_provider when no routing binding.
            name = (cfg.agents.planner_provider or "").strip()
            model = (cfg.agents.planner_model or "").strip()
        else:
            model = (binding.model or "").strip() or (cfg.agents.planner_model or "").strip()
        if not name:
            return None
        return get_provider(name, model=model)
    except Exception:
        name = (cfg.agents.planner_provider or "").strip()
        if not name:
            return None
        try:
            from redibis.enrich.providers import get_provider

            return get_provider(name, model=(cfg.agents.planner_model or "").strip())
        except Exception:
            return None


def copilot_plan(
    intent_text: str,
    cfg: RedibisConfig,
    context: Optional[PlannerContext] = None,
) -> PlannerResult:
    """Plan for copilot chat — LLM when configured, else deterministic heuristic."""
    ctx = context or PlannerContext()
    provider = None
    try:
        from redibis.enrich.capability_routing import (
            build_bindings_from_global,
            resolve_model_binding,
        )
        from redibis.config import load_global_settings_optional
        from redibis.enrich.providers import get_provider

        bindings = build_bindings_from_global(
            load_global_settings_optional(),
            agents_cfg=cfg.agents,
        )
        binding = resolve_model_binding("agent.copilot", bindings=bindings)
        if binding.provider:
            provider = get_provider(binding.provider, model=binding.model or "")
    except Exception:
        provider = None
    if provider is None:
        provider = resolve_planner_provider(cfg)
    if provider is not None:
        try:
            result = IntentPlanner(provider=provider, redibis_config=cfg).plan(intent_text, ctx)
            if result.valid:
                return result
        except Exception:
            pass
    return heuristic_plan(intent_text, ctx)
