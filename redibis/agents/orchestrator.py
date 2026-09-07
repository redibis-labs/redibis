"""Orchestrator — Intent → Plan → Execute → Validate (Phase 2 control loop)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.agents.capability_guard import decide_codegen, match_native
from redibis.agents.models import PipelineNode, PipelineSpec
from redibis.agents.planner import (
    IntentPlanner,
    PlannerContext,
    PlannerResult,
    _extract_table,
    _heuristic_flags,
    copilot_plan,
    heuristic_plan,
    resolve_planner_provider,
)
from redibis.agents.recipes import recipes_for_intent
from redibis.agents.registry import catalog_for_planner, validate_spec
from redibis.agents.review_queue import _autonomy_for_kind
from redibis.agents.run_models import AgentRun, RunStatus
from redibis.agents.tool_runner import ToolContext
from redibis.config import RedibisConfig


class ClarificationRequired(Exception):
    """Raised when ``Orchestrator.run`` needs user input before executing."""

    def __init__(self, question: str, profile: "IntentProfile"):
        super().__init__(question)
        self.question = question
        self.profile = profile


def clarification_for_profile(
    profile: IntentProfile,
    prompt: str,
    *,
    clarification: str = "",
) -> Optional[str]:
    """Return an inline clarifying question, or None when execution can proceed."""
    if clarification.strip():
        return None
    if profile.ambiguous:
        return (
            "Which workflow should I run — profile, PII scan, quality, contract, enrich, "
            "or the full bundle? Reply with one word or 'all'."
        )
    extracted = _extract_table(prompt)
    tables = list(profile.tables or [])
    if not tables and not extracted:
        return "Which table should I use? (format: database.table)"
    if not extracted and tables == ["schema.table"]:
        return "Which table should I use? (format: database.table)"
    return None


_EXTERNAL_TARGET_RE = re.compile(
    r"\b(gemini|cloud|external|vertex|openai|anthropic|gpt|claude)\b",
    re.IGNORECASE,
)


def detect_external_target(text: str) -> bool:
    """True when the prompt explicitly names an external/cloud LLM provider."""
    return bool(_EXTERNAL_TARGET_RE.search(text or ""))


def apply_clarification_to_profile(
    profile: IntentProfile,
    clarification: str,
) -> IntentProfile:
    """Refine an ambiguous profile from the user's inline clarification answer."""
    text = (clarification or "").strip()
    if not text:
        return profile

    lower = text.lower()
    tables = list(profile.tables or [])
    extracted = _extract_table(text)
    if extracted:
        tables = [extracted]
    elif tables == ["schema.table"]:
        tables = []

    intents: list[str] | None = None
    if re.search(r"\b(all|full|bundle|everything)\b", lower):
        intents = ["all", "profile", "pii", "classify", "quality"]
    elif re.search(r"\benrich\b", lower):
        intents = ["profile", "pii", "classify", "quality", "contract", "enrich"]
    elif re.search(r"\bcontract\b", lower):
        intents = ["profile", "pii", "classify", "quality", "contract"]
    elif re.search(r"\b(pii|privacy|presidio)\b", lower):
        intents = ["profile", "pii"]
    elif re.search(r"\bquality\b", lower) or "great expectations" in lower:
        intents = ["profile", "quality"]
    elif re.search(r"\bprofile\b", lower):
        intents = ["profile"]
    elif re.search(r"\bmask\b", lower):
        intents = ["profile", "pii", "classify", "quality", "contract", "mask"]
    elif re.search(r"\bpublish\b", lower) or re.search(r"\bcatalog\b", lower):
        intents = ["profile", "pii", "classify", "quality", "contract", "publish"]

    target = profile.target
    if detect_external_target(text):
        target = "external"
    elif re.search(r"\blocal\b", lower):
        target = "local"

    primary_kind = "contract"
    resolved_intents = intents if intents is not None else list(profile.intents)
    if "enrich" in resolved_intents:
        primary_kind = "enrich"
    elif "publish" in resolved_intents:
        primary_kind = "publish"

    return IntentProfile(
        intents=resolved_intents,
        tables=tables or list(profile.tables),
        target=target,
        autonomy=_autonomy_for_kind(primary_kind),
        native_path=profile.native_path,
        codegen_recommended=profile.codegen_recommended,
        ambiguous=False,
        raw_prompt=profile.raw_prompt,
    )


@dataclass
class IntentProfile:
    """Formal intent taxonomy for the orchestrator loop."""

    intents: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    target: str = "local"  # local | external
    autonomy: str = "suggest-only"
    native_path: bool = True
    codegen_recommended: bool = False
    ambiguous: bool = False
    raw_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intents": list(self.intents),
            "tables": list(self.tables),
            "target": self.target,
            "autonomy": self.autonomy,
            "native_path": self.native_path,
            "codegen_recommended": self.codegen_recommended,
            "ambiguous": self.ambiguous,
        }


def detect_intent(
    prompt: str,
    *,
    cfg: Optional[RedibisConfig] = None,
    tables: Optional[list[str]] = None,
) -> IntentProfile:
    """Classify NL prompt into governance intents (heuristic-first, LLM optional)."""
    cfg = cfg or RedibisConfig.default()
    text = prompt.strip()
    lower = text.lower()
    flags = _heuristic_flags(text)

    intents: list[str] = []
    if flags.profile:
        intents.append("profile")
    if flags.pii:
        intents.append("pii")
    if flags.classify:
        intents.append("classify")
    if flags.quality:
        intents.append("quality")
    if flags.mask:
        intents.append("mask")
    if flags.publish:
        intents.append("publish")
    if any(k in lower for k in ("enrich", "llm", "describe", "document", "business definition")):
        intents.append("enrich")
    if any(k in lower for k in ("contract", "odcs")):
        intents.append("contract")

    special = [i for i in intents if i in ("enrich", "contract", "mask", "publish")]
    if not intents or (len(intents) >= 4 and "all" not in intents):
        intents = ["all", "profile", "pii", "classify", "quality"]
        for s in special:
            if s not in intents:
                intents.append(s)

    resolved_tables = list(tables or [])
    extracted = _extract_table(text)
    if extracted and extracted not in resolved_tables:
        resolved_tables.append(extracted)

    target = "external" if detect_external_target(text) else "local"

    native_hits = match_native(text)
    codegen = decide_codegen(text)

    primary_kind = "contract"
    if "enrich" in intents:
        primary_kind = "enrich"
    elif "publish" in intents:
        primary_kind = "publish"
    autonomy = _autonomy_for_kind(primary_kind)

    ambiguous = sum((
        int(flags.pii),
        int(flags.quality),
        int(flags.mask),
        int(flags.publish),
        int("enrich" in intents),
    )) >= 3 and not extracted

    profile = IntentProfile(
        intents=intents,
        tables=resolved_tables,
        target=target,
        autonomy=autonomy,
        native_path=bool(native_hits) or not codegen.generate,
        codegen_recommended=codegen.generate and not native_hits,
        ambiguous=ambiguous,
        raw_prompt=text,
    )

    if ambiguous and resolve_planner_provider(cfg) is not None:
        profile.ambiguous = True  # planner LLM may refine; heuristics still primary

    return profile


def apply_intent_to_spec(
    spec: PipelineSpec,
    profile: IntentProfile,
    *,
    enrich_provider: str = "",
    enrich_model: str = "",
) -> PipelineSpec:
    """Map IntentProfile onto node params (target, enrich, phone engine, dry-run)."""
    nodes: list[PipelineNode] = []
    for node in spec.nodes:
        params = dict(node.params)
        if node.kind in ("enrich", "contract"):
            if profile.target == "external":
                params.setdefault("target", "external")
                params.setdefault("residency", "external")
            else:
                params.setdefault("target", "local")
            if "enrich" in profile.intents:
                params["enrich"] = True
                if enrich_provider:
                    params.setdefault("provider", enrich_provider)
                if enrich_model:
                    params.setdefault("model", enrich_model)
        if node.kind in ("pii_scan", "contract") and "pii" in profile.intents:
            params.setdefault("phone_engine", True)
        if node.kind == "mask" or (node.kind == "contract" and "mask" in profile.intents):
            params.setdefault("plan", "auto")
        if node.kind in ("catalog_push", "publish"):
            if profile.autonomy == "suggest-only":
                params.setdefault("dry_run", True)
        nodes.append(PipelineNode(
            id=node.id,
            kind=node.kind,
            label=node.label,
            params=params,
            position=node.position,
        ))
    return PipelineSpec(
        name=spec.name,
        goal=spec.goal,
        nodes=nodes,
        edges=list(spec.edges),
    )


class Orchestrator:
    """Single control loop: intent → plan → execute → validate → report."""

    def __init__(
        self,
        *,
        lineage: Any,
        tool_ctx: ToolContext,
        config: Optional[RedibisConfig] = None,
    ):
        self.lineage = lineage
        self.tool_ctx = tool_ctx
        self.config = config or tool_ctx._redibis_config()

    def plan(
        self,
        prompt: str,
        profile: IntentProfile,
        *,
        context: Optional[PlannerContext] = None,
    ) -> PlannerResult:
        ctx = context or PlannerContext(
            database=profile.tables[0] if profile.tables else "",
            memory_recipes=recipes_for_intent(prompt),
        )
        provider = resolve_planner_provider(self.config)
        fallback_reason = ""
        attempted_llm = False
        if provider is not None and not profile.ambiguous:
            attempted_llm = True
            try:
                result = IntentPlanner(provider=provider, redibis_config=self.config).plan(prompt, ctx)
                if result.valid:
                    result.method = "llm"
                    result.provider = str(getattr(provider, "name", "") or "")
                    result.model = str(getattr(provider, "model", "") or "")
                    return result
                fallback_reason = "LLM planner returned an invalid pipeline"
            except Exception as exc:
                fallback_reason = f"LLM planner unavailable: {type(exc).__name__}"
        elif provider is not None and profile.ambiguous:
            fallback_reason = "Ambiguous intent requires deterministic clarification"

        result = heuristic_plan(prompt, ctx)
        result.method = "heuristic_fallback" if attempted_llm else "heuristic"
        result.fallback_reason = fallback_reason
        if attempted_llm:
            result.provider = str(getattr(provider, "name", "") or "")
            result.model = str(getattr(provider, "model", "") or "")
        if result.valid:
            return result

        max_repairs = max(0, int(getattr(self.config.agents, "max_plan_repairs", 2) or 2))
        if provider is None or max_repairs == 0:
            return result

        planner = IntentPlanner(provider=provider, redibis_config=self.config)
        for _ in range(max_repairs):
            validation = validate_spec(result.spec)
            if not validation:
                result.valid = True
                result.method = "llm_repair"
                result.provider = str(getattr(provider, "name", "") or "")
                result.model = str(getattr(provider, "model", "") or "")
                return result
            repaired, errors, _rai = planner._repair(
                prompt,
                result.spec,
                [e.message for e in validation],
                catalog=catalog_for_planner(),
            )
            if errors:
                break
            result = PlannerResult(spec=repaired, valid=False, repaired=True, errors=[])
            validation = validate_spec(repaired)
            if not validation:
                result.valid = True
                result.method = "llm_repair"
                result.provider = str(getattr(provider, "name", "") or "")
                result.model = str(getattr(provider, "model", "") or "")
                result.rai_report = _rai
                return result
            result.errors = [e.message for e in validation]
        return result

    def execute(
        self,
        spec: PipelineSpec,
        tables: list[str],
        *,
        dry_run: bool = False,
        run_id: Optional[str] = None,
    ) -> AgentRun:
        from redibis.agents.pipeline_executor import PipelineExecutor

        if len(tables) == 1:
            executor = PipelineExecutor(self.lineage, tool_ctx=self.tool_ctx)
            return executor.execute(spec, tables[0], dry_run=dry_run, run_id=run_id)

        from redibis.agents.batch_executor import BatchExecutor

        batch = BatchExecutor(self.lineage, tool_ctx=self.tool_ctx)
        return batch.run(spec, tables=tables, dry_run=dry_run, run_id=run_id)

    def run(
        self,
        prompt: str,
        *,
        tables: Optional[list[str]] = None,
        dry_run: bool = False,
        run_id: Optional[str] = None,
        clarification: str = "",
    ) -> tuple[IntentProfile, PlannerResult, AgentRun]:
        profile = detect_intent(prompt, cfg=self.config, tables=tables)
        if clarification.strip():
            profile = apply_clarification_to_profile(profile, clarification)
        question = clarification_for_profile(profile, prompt, clarification=clarification)
        if question:
            raise ClarificationRequired(question, profile)
        plan_prompt = prompt
        if clarification.strip():
            plan_prompt = f"{prompt}\n\nUser clarification: {clarification.strip()}"
        plan_result = self.plan(plan_prompt, profile)
        llm = self.config.pii.llm
        spec = apply_intent_to_spec(
            plan_result.spec,
            profile,
            enrich_provider=llm.provider if llm.enabled else "",
            enrich_model=llm.model_name if llm.enabled else "",
        )

        run_tables = profile.tables or tables or []
        if not run_tables:
            slug = re.sub(r"[^\w.]+", ".", _extract_table(prompt) or "schema.table")
            run_tables = [slug]

        agent_run = self.execute(spec, run_tables, dry_run=dry_run, run_id=run_id)
        summary = agent_run.ledger.reconcile(agent_run.steps)
        agent_run.batch_meta = {
            **dict(agent_run.batch_meta or {}),
            "intent": profile.to_dict(),
            "ledger_summary": summary,
        }
        if agent_run.status == RunStatus.RUNNING:
            agent_run.status = RunStatus.COMPLETED
        self.lineage.save(agent_run)
        return profile, plan_result, agent_run
