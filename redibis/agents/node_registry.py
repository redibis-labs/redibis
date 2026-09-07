"""Node registry — palette of deterministic tools for the agentic board."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class NodeParamSpec:
    name: str
    param_type: str = "string"
    default: Any = None
    required: bool = False
    description: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeSpec:
    """One palette item — single source of truth for form + prompt fragment."""

    kind: str
    label: str
    tool: str
    description: str = ""
    category: str = "core"
    params: tuple[NodeParamSpec, ...] = ()
    prompt_template: str = ""

    def default_params(self) -> dict[str, Any]:
        return {
            p.name: p.default
            for p in self.params
            if p.default is not None
        }

    def render_prompt_fragment(self, params: dict[str, Any]) -> str:
        ctx = {**self.default_params(), **params}
        try:
            return self.prompt_template.format(**ctx)
        except KeyError:
            return self.prompt_template

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "tool": self.tool,
            "description": self.description,
            "category": self.category,
            "params": [
                {
                    "name": p.name,
                    "type": p.param_type,
                    "default": p.default,
                    "required": p.required,
                    "description": p.description,
                    "choices": list(p.choices),
                }
                for p in self.params
            ],
            "default_params": self.default_params(),
        }


NODE_REGISTRY: dict[str, NodeSpec] = {}


def register_node(spec: NodeSpec) -> None:
    NODE_REGISTRY[spec.kind] = spec


def merged_registry() -> dict[str, NodeSpec]:
    """Built-in nodes + entry-point plugins (built-ins win on name clash)."""
    from redibis.agents.plugins import plugin_registry

    merged = plugin_registry()
    merged.update(NODE_REGISTRY)
    return merged


def get_node(kind: str) -> NodeSpec:
    reg = merged_registry()
    if kind not in reg:
        raise KeyError(f"unknown pipeline node kind: {kind!r}")
    return reg[kind]


def list_nodes(*, category: Optional[str] = None) -> list[NodeSpec]:
    nodes = list(merged_registry().values())
    if category:
        nodes = [n for n in nodes if n.category == category]
    return sorted(nodes, key=lambda n: (n.category, n.label))


def _register_builtins() -> None:
    builtins = [
        NodeSpec(
            kind="source_table",
            label="Source Table",
            tool="MetadataRetriever.list_tables",
            category="source",
            description="Collect external metadata and sampling strategy for a table.",
            params=(
                NodeParamSpec("table", default="", required=True, description="schema.table"),
                NodeParamSpec("engine", default="hive", choices=("hive", "postgres", "oracle", "jdbc")),
                NodeParamSpec("sampling_tier", default="catalog_first", choices=("catalog_first", "pushdown", "sample")),
            ),
            prompt_template=(
                "Connect to source engine `{engine}` and resolve table `{table}`. "
                "Use sampling tier `{sampling_tier}` (catalog → pushdown → sample)."
            ),
        ),
        NodeSpec(
            kind="profile_scan",
            label="Profile Scan",
            tool="Scan.profile",
            category="scan",
            description="Run structural profiling on the sample.",
            params=(
                NodeParamSpec("engine", default="great_expectations", choices=("great_expectations", "open_metadata")),
            ),
            prompt_template="Run profile scan using profiler engine `{engine}`.",
        ),
        NodeSpec(
            kind="quality_scan",
            label="Quality Scan",
            tool="Scan.suggest_quality_rules",
            category="scan",
            description="Generate GE quality rules from the profile.",
            params=(),
            prompt_template="Run quality scan and suggest GE expectations for human review.",
        ),
        NodeSpec(
            kind="pii_scan",
            label="PII Scan",
            tool="Scan.detect_pii",
            category="scan",
            description="Run Presidio + GLiNER PII detection.",
            params=(
                NodeParamSpec("equation_mode", default="balanced", choices=("strict", "balanced", "lenient")),
            ),
            prompt_template="Run PII scan with equation mode `{equation_mode}`.",
        ),
        NodeSpec(
            kind="classify",
            label="Classify",
            tool="ClassificationService.classify_contract",
            category="governance",
            description="Multi-domain classification via policy engine.",
            params=(
                NodeParamSpec("policy_pack", default="telecom"),
                NodeParamSpec("jurisdiction", default="", description="Steward-supplied jurisdiction signal"),
                NodeParamSpec("use_memory", param_type="boolean", default=False),
            ),
            prompt_template=(
                "Classify columns using policy pack `{policy_pack}` "
                "with jurisdiction `{jurisdiction}` (steward-supplied)."
            ),
        ),
        NodeSpec(
            kind="enrich",
            label="Enrich",
            tool="EnrichmentService.enrich",
            category="governance",
            description="LLM enrichment with optional context review; auto-writes active contract.",
            params=(
                NodeParamSpec("use_memory", param_type="boolean", default=True),
                NodeParamSpec("provider", default="", description="llm_providers.json key"),
                NodeParamSpec("model", default="", description="override model id"),
                NodeParamSpec("residency", default="local", choices=("local", "public")),
            ),
            prompt_template=(
                "Enrich contract definitions (provider={provider}, memory={use_memory}). "
                "Suggest-only — steward must merge candidate."
            ),
        ),
        NodeSpec(
            kind="catalog_push",
            label="Catalog Push",
            tool="CatalogPublisher.push",
            category="publish",
            description="Push contract + classifications to Atlas/OpenMetadata/DataHub.",
            params=(
                NodeParamSpec("backend", default="atlas", choices=("atlas", "openmetadata", "datahub")),
                NodeParamSpec("dry_run", param_type="boolean", default=False),
            ),
            prompt_template="Push to catalog backend `{backend}` (dry_run={dry_run}).",
        ),
        NodeSpec(
            kind="batch_loop",
            label="Batch Loop",
            tool="LoopAgent.fan_out",
            category="orchestration",
            description="Fan out pipeline across all tables from MetadataRetriever.",
            params=(
                NodeParamSpec("database", default="", description="Limit to one database/schema"),
            ),
            prompt_template="Loop over tables in database `{database}` (empty = all).",
        ),
        NodeSpec(
            kind="approval_gate",
            label="Approval Gate",
            tool="ApprovalGate.await_human",
            category="governance",
            description="Pause for role-routed human approval before contract write.",
            params=(
                NodeParamSpec("role", default="Steward", choices=("Steward", "Architecture", "SecOps", "DPO", "Legal")),
            ),
            prompt_template="Await approval from role `{role}` before persisting changes.",
        ),
        NodeSpec(
            kind="contract_write",
            label="Write Contract",
            tool="ContractStore.upsert",
            category="publish",
            description="Persist approved partials via the sole contract writer.",
            params=(
                NodeParamSpec("automerge", default="none", choices=("none", "pii", "quality", "both")),
            ),
            prompt_template="Write contract via ContractStore.upsert (automerge={automerge}).",
        ),
        NodeSpec(
            kind="contract_synthesis",
            label="Contract Synthesis",
            tool="ContractSynthesisRunner.run",
            category="governance",
            description=(
                "Build a portable ODCS v3.1 candidate from a v3 base contract plus "
                "requirements and Spark/SQL/DataStage sources. Does not upsert."
            ),
            params=(
                NodeParamSpec(
                    "analysis_mode",
                    default="deterministic",
                    choices=("deterministic", "assisted"),
                ),
                NodeParamSpec(
                    "contract_file",
                    default="",
                    description="Path to base ODCS v3 contract YAML",
                    required=True,
                ),
                NodeParamSpec(
                    "requirements",
                    default="",
                    description="Comma-separated requirement file paths",
                ),
                NodeParamSpec(
                    "sources",
                    default="",
                    description="Comma-separated source/ZIP paths",
                ),
                NodeParamSpec(
                    "output_dir",
                    default="./synthesis_out",
                    description="Portable artifact directory",
                ),
                NodeParamSpec("provider", default="", description="assisted-mode provider"),
            ),
            prompt_template=(
                "Synthesize portable ODCS v3.1 from `{contract_file}` "
                "(mode={analysis_mode}); never upsert active contracts."
            ),
        ),
        NodeSpec(
            kind="deep_scan",
            label="Deep Scan",
            tool="deep_scan.run",
            category="scan",
            description="Multi-producer evidence fan-out with LLM synthesis bundle.",
            params=(
                NodeParamSpec(
                    "producers",
                    default="",
                    description="comma-separated producer ids (empty = catalogue defaults)",
                ),
                NodeParamSpec("build_bundle", param_type="boolean", default=True),
            ),
            prompt_template=(
                "Run deep scan producers `{producers}` and build synthesis bundle={build_bundle}."
            ),
        ),
        NodeSpec(
            kind="pii_tune",
            label="PII Tune",
            tool="pii_tune.propose",
            category="scan",
            description="Run PII scan, build tuning bundle, propose RegexOverrides (approval required).",
            params=(
                NodeParamSpec("build_bundle", param_type="boolean", default=True),
            ),
            prompt_template="Scan for PII evidence and build an LLM tuning bundle for human review.",
        ),
    ]
    for spec in builtins:
        register_node(spec)


_register_builtins()
