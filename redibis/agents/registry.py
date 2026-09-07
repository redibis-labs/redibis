"""Node registry — typed ports, config schemas, and tool bindings for agentic pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── Port type tokens (handover Part 2) ────────────────────────────────────────

class PortType:
    DATA_REF = "DataRef"
    PROFILE_RESULT = "ProfileResult"
    CONTRACT_DRAFT = "ContractDraft"
    MASK_PLAN = "MaskPlan"
    PUBLISH_RESULT = "PublishResult"
    VOID = "Void"


@dataclass(frozen=True)
class DataRef:
    """Handle to sampled table data (path, table id, row count)."""
    table: str = ""
    sample_path: str = ""
    rows: int = 0


@dataclass(frozen=True)
class ProfileResultRef:
    """Profiling output reference."""
    table: str = ""
    columns: int = 0


@dataclass(frozen=True)
class ContractDraftRef:
    """Partial or draft ODCS contract."""
    table: str = ""
    kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaskPlanRef:
    """Suggested masking plan (suggest-only)."""
    table: str = ""


@dataclass(frozen=True)
class PublishResultRef:
    """Catalog publish outcome."""
    table: str = ""
    backend: str = ""


@dataclass(frozen=True)
class PortSpec:
    name: str
    port_type: str
    required: bool = True


@dataclass(frozen=True)
class ConfigFieldSpec:
    name: str
    field_type: str = "string"
    default: Any = None
    required: bool = False
    description: str = ""
    choices: tuple[str, ...] = ()
    min_value: Optional[float] = None
    max_value: Optional[float] = None


@dataclass(frozen=True)
class NodeRegistryEntry:
    """One palette node — config schema + typed ports + bound tool."""

    type: str
    label: str
    tool_ref: str
    description: str = ""
    category: str = "core"
    config_schema: tuple[ConfigFieldSpec, ...] = ()
    input_ports: tuple[PortSpec, ...] = ()
    output_ports: tuple[PortSpec, ...] = ()
    autonomy_default: str = "suggest-only"

    def default_config(self) -> dict[str, Any]:
        return {
            f.name: f.default
            for f in self.config_schema
            if f.default is not None
        }

    def validate_config(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        merged = {**self.default_config(), **params}
        for spec in self.config_schema:
            if spec.required and spec.name not in merged:
                errors.append(f"missing required param {spec.name!r}")
                continue
            val = merged.get(spec.name)
            if val is None:
                continue
            if spec.choices and str(val) not in spec.choices:
                errors.append(
                    f"param {spec.name!r} must be one of {list(spec.choices)}, got {val!r}"
                )
            if spec.field_type in ("int", "float") and spec.min_value is not None:
                try:
                    num = float(val)
                except (TypeError, ValueError):
                    errors.append(f"param {spec.name!r} must be numeric")
                else:
                    if num < spec.min_value:
                        errors.append(f"param {spec.name!r} must be >= {spec.min_value}")
                    if spec.max_value is not None and num > spec.max_value:
                        errors.append(f"param {spec.name!r} must be <= {spec.max_value}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "label": self.label,
            "tool_ref": self.tool_ref,
            "description": self.description,
            "category": self.category,
            "autonomy_default": self.autonomy_default,
            "config_schema": [
                {
                    "name": f.name,
                    "type": f.field_type,
                    "default": f.default,
                    "required": f.required,
                    "description": f.description,
                    "choices": list(f.choices),
                }
                for f in self.config_schema
            ],
            "input_ports": [
                {"name": p.name, "type": p.port_type, "required": p.required}
                for p in self.input_ports
            ],
            "output_ports": [
                {"name": p.name, "type": p.port_type, "required": p.required}
                for p in self.output_ports
            ],
        }


NODE_REGISTRY: dict[str, NodeRegistryEntry] = {}

# Legacy board kinds (pre-registry) → canonical port vocabulary
KIND_ALIASES: dict[str, str] = {
    "source_table": "source",
    "profile_scan": "profile",
    "quality_scan": "contract",
    "pii_scan": "contract",
    "classify": "contract",
    "enrich": "contract",
    "catalog_push": "publish",
    "contract_write": "publish",
    "approval_gate": "gate",
    "batch_loop": "batch",
}


def register_node(entry: NodeRegistryEntry) -> None:
    NODE_REGISTRY[entry.type] = entry


def get_node(node_type: str) -> NodeRegistryEntry:
    canonical = KIND_ALIASES.get(node_type, node_type)
    if canonical not in NODE_REGISTRY:
        raise KeyError(f"unknown pipeline node type: {node_type!r}")
    return NODE_REGISTRY[canonical]


def list_nodes(*, category: Optional[str] = None) -> list[NodeRegistryEntry]:
    nodes = list(NODE_REGISTRY.values())
    if category:
        nodes = [n for n in nodes if n.category == category]
    return sorted(nodes, key=lambda n: (n.category, n.label))


def resolve_node_type(kind: str) -> str:
    return KIND_ALIASES.get(kind, kind)


def _port_compatible(output_type: str, input_type: str) -> bool:
    if output_type == input_type:
        return True
    # Contract draft is accepted where profile is expected downstream of composite contract node
    if output_type == PortType.CONTRACT_DRAFT and input_type == PortType.PROFILE_RESULT:
        return False
    return False


def _register_builtins() -> None:
    builtins = [
        NodeRegistryEntry(
            type="source",
            label="Source",
            tool_ref="MetadataRetriever.list_tables",
            category="source",
            description=(
                "ALWAYS the first node. Picks where tables come from. Use engine=hive/oracle/"
                "postgres/jdbc for external catalogs, or engine=folder for uploaded CSV samples. "
                "Outputs a DataRef that every downstream node needs."
            ),
            config_schema=(
                ConfigFieldSpec(
                    "engine", default="hive",
                    choices=("hive", "postgres", "oracle", "jdbc", "folder"),
                    description="Source system; 'folder' = local uploaded CSV samples.",
                ),
                ConfigFieldSpec("table", default="", description="schema.table (external) or folder path (local). Leave blank for whole-schema batch."),
            ),
            input_ports=(),
            output_ports=(PortSpec("data", PortType.DATA_REF),),
            autonomy_default="auto",
        ),
        NodeRegistryEntry(
            type="sample",
            label="Sample",
            tool_ref="TableSampler.sample",
            category="source",
            description="Sample strategy: percent, all, partition, N columns.",
            config_schema=(
                ConfigFieldSpec(
                    "strategy",
                    default="percent",
                    choices=("percent", "all", "day_partition", "n_columns", "partition_percent"),
                ),
                ConfigFieldSpec("amount", field_type="float", default=10.0, min_value=0.01, max_value=100.0),
            ),
            input_ports=(PortSpec("data", PortType.DATA_REF),),
            output_ports=(PortSpec("data", PortType.DATA_REF),),
            autonomy_default="auto",
        ),
        NodeRegistryEntry(
            type="profile",
            label="Profile",
            tool_ref="Scan.profile",
            category="scan",
            description="Structural profiling (GE / OpenMetadata backends).",
            config_schema=(
                ConfigFieldSpec(
                    "engine",
                    default="great_expectations",
                    choices=("great_expectations", "open_metadata"),
                ),
            ),
            input_ports=(PortSpec("data", PortType.DATA_REF, required=True),),
            output_ports=(PortSpec("profile", PortType.PROFILE_RESULT),),
            autonomy_default="auto",
        ),
        NodeRegistryEntry(
            type="contract",
            label="Contract",
            tool_ref="Scan.run",
            category="governance",
            description=(
                "Builds the contract. Enable the sub-flags the intent asks for: pii (detect PII), "
                "classify (multi-tag classification via the policy pack), quality (data-quality "
                "rules), mask_rules (annotate masking strategy), enrich (LLM business definitions). "
                "Profiling runs automatically. Requires BOTH a DataRef and a ProfileResult upstream "
                "— wire sample AND profile into it. At least one sub-flag must be on."
            ),
            config_schema=(
                ConfigFieldSpec("pii", field_type="boolean", default=True, description="Detect PII columns."),
                ConfigFieldSpec("classify", field_type="boolean", default=True, description="Multi-tag classification from the policy pack."),
                ConfigFieldSpec("mask_rules", field_type="boolean", default=False, description="Annotate a masking strategy per PII column (enforced externally)."),
                ConfigFieldSpec("quality", field_type="boolean", default=True, description="Generate data-quality rules."),
                ConfigFieldSpec("enrich", field_type="boolean", default=False, description="LLM-suggest business definitions (grounded on past approvals)."),
                ConfigFieldSpec("policy_pack", default="telecom", description="Classification policy pack name."),
            ),
            input_ports=(
                PortSpec("data", PortType.DATA_REF, required=True),
                PortSpec("profile", PortType.PROFILE_RESULT, required=True),
            ),
            output_ports=(PortSpec("draft", PortType.CONTRACT_DRAFT),),
            autonomy_default="suggest-only",
        ),
        NodeRegistryEntry(
            type="mask",
            label="Mask",
            tool_ref="MaskingEngine.suggest",
            category="governance",
            description="Suggest masking plan only — steward adjusts on masking page.",
            config_schema=(),
            input_ports=(PortSpec("draft", PortType.CONTRACT_DRAFT, required=True),),
            output_ports=(PortSpec("plan", PortType.MASK_PLAN),),
            autonomy_default="suggest-only",
        ),
        NodeRegistryEntry(
            type="publish",
            label="Publish",
            tool_ref="CatalogPublisher.push",
            category="publish",
            description=(
                "Publishes the contract + classifications to a catalog. Use ONLY after an approval "
                "gate (it's an external write). backend=openmetadata today; atlas/datahub when ready."
            ),
            config_schema=(
                ConfigFieldSpec("backend", default="openmetadata", choices=("openmetadata", "atlas", "datahub")),
                ConfigFieldSpec("dry_run", field_type="boolean", default=False),
            ),
            input_ports=(PortSpec("draft", PortType.CONTRACT_DRAFT, required=True),),
            output_ports=(PortSpec("result", PortType.PUBLISH_RESULT),),
            autonomy_default="gate",
        ),
        NodeRegistryEntry(
            type="gate",
            label="Approval Gate",
            tool_ref="ApprovalGate.await_human",
            category="governance",
            description=(
                "Insert before any publish or contract write. Pauses for the named role to approve "
                "(Steward by default; DPO for biometric/cross-border, SecOps for lawful intercept)."
            ),
            config_schema=(
                ConfigFieldSpec(
                    "role",
                    default="Steward",
                    choices=("Steward", "Architecture", "SecOps", "DPO", "Legal"),
                ),
            ),
            input_ports=(PortSpec("draft", PortType.CONTRACT_DRAFT, required=True),),
            output_ports=(PortSpec("draft", PortType.CONTRACT_DRAFT),),
            autonomy_default="gate",
        ),
        NodeRegistryEntry(
            type="batch",
            label="Batch",
            tool_ref="LoopAgent.fan_out",
            category="orchestration",
            description="Fan out pipeline across tables from MetadataRetriever.",
            config_schema=(
                ConfigFieldSpec("database", default="", description="Limit to one database/schema"),
            ),
            input_ports=(),
            output_ports=(PortSpec("data", PortType.DATA_REF),),
            autonomy_default="auto",
        ),
    ]
    for entry in builtins:
        register_node(entry)


_register_builtins()


@dataclass
class ValidationError:
    """Node-anchored structural validation message."""

    node_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"node_id": self.node_id, "message": self.message}


def validate_spec(spec: Any) -> list[ValidationError]:
    """
    Check port compatibility and per-node config against the registry.

    Returns an empty list when the graph is structurally sound.
    """
    from redibis.agents.models import PipelineSpec

    if not isinstance(spec, PipelineSpec):
        raise TypeError(f"expected PipelineSpec, got {type(spec).__name__}")

    errors: list[ValidationError] = []
    node_by_id = {n.id: n for n in spec.nodes}

    for node in spec.nodes:
        try:
            entry = get_node(node.kind)
        except KeyError:
            errors.append(ValidationError(node.id, f"unknown node kind {node.kind!r}"))
            continue
        for msg in entry.validate_config(node.params):
            errors.append(ValidationError(node.id, msg))

    # Track available output port types per node (by port name → type)
    available: dict[str, dict[str, str]] = {}
    for node in spec.nodes:
        try:
            entry = get_node(node.kind)
        except KeyError:
            continue
        available[node.id] = {p.name: p.port_type for p in entry.output_ports}

    # Per-node: direct upstream output types must satisfy required inputs
    parents: dict[str, list[str]] = {n.id: [] for n in spec.nodes}
    for edge in spec.edges:
        parents.setdefault(edge.target, []).append(edge.source)

    for node in spec.nodes:
        try:
            tgt_entry = get_node(node.kind)
        except KeyError:
            continue
        upstream_types: set[str] = set()
        for pid in parents.get(node.id, []):
            pnode = node_by_id.get(pid)
            if not pnode:
                continue
            try:
                p_entry = get_node(pnode.kind)
            except KeyError:
                continue
            upstream_types.update(p.port_type for p in p_entry.output_ports)

        for inp in tgt_entry.input_ports:
            if not inp.required:
                continue
            if inp.port_type not in upstream_types:
                errors.append(ValidationError(
                    node.id,
                    (
                        f"missing required input {inp.name!r} ({inp.port_type}) "
                        f"for {node.kind!r}; upstream provides {sorted(upstream_types) or 'nothing'}"
                    ),
                ))

    for edge in spec.edges:
        src = node_by_id.get(edge.source)
        tgt = node_by_id.get(edge.target)
        if not src:
            errors.append(ValidationError(edge.target, f"edge source {edge.source!r} not found"))
            continue
        if not tgt:
            errors.append(ValidationError(edge.source, f"edge target {edge.target!r} not found"))
            continue
        try:
            src_entry = get_node(src.kind)
            tgt_entry = get_node(tgt.kind)
        except KeyError:
            continue

        src_out = {p.port_type for p in src_entry.output_ports}
        if not src_out:
            errors.append(ValidationError(
                src.id,
                f"node {src.kind!r} has no outputs but is wired to {tgt.kind!r}",
            ))
            continue

        tgt_required = [p for p in tgt_entry.input_ports if p.required]
        if not tgt_required:
            continue

        matched = False
        for out_type in src_out:
            for inp in tgt_required:
                if _port_compatible(out_type, inp.port_type):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            errors.append(ValidationError(
                tgt.id,
                (
                    f"incompatible wiring: {src.kind!r} outputs {sorted(src_out)} "
                    f"but {tgt.kind!r} requires "
                    f"{[p.port_type for p in tgt_required]}"
                ),
            ))

    # Contract node must enable at least one sub-flag
    for node in spec.nodes:
        if resolve_node_type(node.kind) != "contract":
            continue
        flags = ("pii", "classify", "mask_rules", "quality")
        if not any(node.params.get(f, entry_default(node, f)) for f in flags):
            errors.append(ValidationError(
                node.id,
                "contract node requires at least one of: pii, classify, mask_rules, quality",
            ))

    return errors


def entry_default(node: Any, field_name: str) -> Any:
    try:
        entry = get_node(node.kind)
    except KeyError:
        return False
    for spec in entry.config_schema:
        if spec.name == field_name:
            return spec.default
    return False


def catalog_for_planner() -> list[dict[str, Any]]:
    """Serialize registry for constrained LLM planning."""
    return [entry.to_dict() for entry in list_nodes()]


def palette_entries() -> list[dict[str, Any]]:
    """UI palette entries — config_schema as param specs for config cards."""
    entries: list[dict[str, Any]] = []
    for entry in list_nodes():
        entries.append({
            "kind": entry.type,
            "label": entry.label,
            "tool": entry.tool_ref,
            "description": entry.description,
            "category": entry.category,
            "autonomy_default": entry.autonomy_default,
            "default_params": entry.default_config(),
            "params": [
                {
                    "name": f.name,
                    "type": f.field_type,
                    "default": f.default,
                    "required": f.required,
                    "description": f.description,
                    "choices": list(f.choices),
                }
                for f in entry.config_schema
            ],
            "input_ports": [p.port_type for p in entry.input_ports],
            "output_ports": [p.port_type for p in entry.output_ports],
        })
    return entries
