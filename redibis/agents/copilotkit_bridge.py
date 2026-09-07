"""CopilotKit / AG-UI bridge — optional LangGraph chat endpoint for the agent board.

Mounted at ``/api/copilotkit/agent`` when ``agents.copilotkit_enabled`` is true and
``ag-ui-langgraph`` is installed (``pip install redibis[agents,copilotkit]``).

The open-core board keeps its REST + React Flow UI; this endpoint lets a CopilotKit
or AG-UI client attach without holding domain logic.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from redibis.config import RedibisConfig

logger = logging.getLogger("redibis.agents.copilotkit")


def copilotkit_available() -> bool:
    try:
        import ag_ui_langgraph  # noqa: F401
        return True
    except ImportError:
        return False


def is_planning_intent(text: str) -> bool:
    """True when the user message looks like a pipeline-planning request."""
    t = text.lower().strip()
    if not t:
        return False
    triggers = (
        "plan", "pipeline", "onboard", "workflow", "graph", "build me", "create a",
        "design a", "classify", "profile", "publish", "mask", "pii", "quality",
    )
    return any(k in t for k in triggers)


def chat_guard_blocks(user_text: str, effective_cfg: RedibisConfig) -> Optional[str]:
    """Reuse the Text Gateway prompt-injection guard on chat input.

    Module-level (not nested in :func:`build_governance_graph`) so it can be
    unit-tested without importing ``langgraph``/``langchain_core``. Heuristic
    check is free and always runs; the ``gateway.prompt_injection``
    capability role additionally judges it when an operator has bound a
    model to that role. Returns a refusal message when blocked, else ``None``.
    """
    if not user_text.strip():
        return None
    try:
        from redibis.pii.text_guards import check_prompt_injection, strongest_action

        gw_cfg = getattr(effective_cfg, "text_gateway", None)
        result = check_prompt_injection(
            user_text,
            redibis_config=effective_cfg,
            use_llm=bool(getattr(gw_cfg, "prompt_injection_llm_enabled", False)),
            heuristic_enabled=bool(getattr(gw_cfg, "heuristic_prompt_injection", True)),
        )
        action, reasons = strongest_action(
            {"prompt_injection": result},
            thresholds=getattr(gw_cfg, "thresholds", None),
        )
        if action == "block":
            logger.warning("copilotkit chat input blocked by guard: %s", reasons)
            return (
                "I can't act on that message — it looks like an attempt to "
                "override my instructions (prompt injection). Rephrase your "
                "governance question or pipeline request."
            )
    except Exception:
        logger.debug("copilotkit prompt-injection guard skipped", exc_info=True)
    return None


def build_governance_graph(
    cfg: RedibisConfig,
    *,
    config_loader: Optional[Callable[[], RedibisConfig]] = None,
):
    """Minimal LangGraph chat graph for governance Q&A (catalog-aware)."""
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    from redibis.agents.planner import PlannerContext, copilot_plan, format_plan_reply
    from redibis.agents.registry import catalog_for_planner

    catalog = catalog_for_planner()
    kind_sample = ", ".join(
        sorted({str(n.get("type") or n.get("kind") or "") for n in catalog if isinstance(n, dict)})[:10]
    )

    def _last_user_text(messages: list[Any]) -> str:
        for msg in reversed(messages):
            role = getattr(msg, "type", None) or getattr(msg, "role", None)
            if role in ("human", "user") or msg.__class__.__name__ == "HumanMessage":
                return str(getattr(msg, "content", "") or "")
        return ""

    def _respond(state: MessagesState) -> dict[str, Any]:
        user = _last_user_text(state.get("messages") or [])
        effective_cfg = config_loader() if config_loader is not None else cfg
        refusal = chat_guard_blocks(user, effective_cfg)
        if not effective_cfg.agents.enabled:
            text = (
                "Agent execution is disabled. An operator must enable "
                "`agents.enabled` before the governance copilot can plan or run work."
            )
        elif refusal is not None:
            text = refusal
        elif not user.strip():
            text = (
                "I am the redibis governance copilot. Describe a pipeline goal "
                "(profile, PII, classify, publish) or ask about node kinds."
            )
        elif is_planning_intent(user):
            result = copilot_plan(
                user,
                effective_cfg,
                PlannerContext(policy_pack="telecom"),
            )
            method = "llm" if (effective_cfg.agents.planner_provider or "").strip() else "heuristic"
            text = format_plan_reply(result, method=method)
        else:
            text = (
                f"Intent noted: {user[:500]}\n\n"
                f"Catalog includes: {kind_sample}.\n"
                "Ask me to **plan** a pipeline (e.g. onboard telco_cdw — classify, publish), "
                "or use **Plan** on the board with an LLM provider. "
                "Nothing irreversible runs without your approval."
            )
        return {"messages": [AIMessage(content=text)]}

    graph = StateGraph(MessagesState)
    graph.add_node("respond", _respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)

    from langgraph.checkpoint.memory import MemorySaver

    return graph.compile(checkpointer=MemorySaver())


_MOUNTED_APP_ID: int | None = None


def mount_copilotkit_routes(
    app: Any,
    cfg: Optional[RedibisConfig] = None,
    *,
    config_loader: Optional[Callable[[], RedibisConfig]] = None,
) -> bool:
    """Register AG-UI SSE endpoint on the FastAPI app. Returns True if mounted."""
    global _MOUNTED_APP_ID
    cfg = cfg or RedibisConfig.default()
    if not (cfg.agents.enabled and cfg.agents.copilotkit_enabled):
        return False
    if not copilotkit_available():
        return False
    app_id = id(app)
    if _MOUNTED_APP_ID == app_id:
        return True

    from ag_ui_langgraph import LangGraphAgent, add_langgraph_fastapi_endpoint

    agent = LangGraphAgent(
        name="redibis-governance",
        graph=build_governance_graph(cfg, config_loader=config_loader),
        description="Data governance pipeline copilot (propose-only)",
    )
    add_langgraph_fastapi_endpoint(app, agent, "/api/copilotkit/agent")
    _MOUNTED_APP_ID = app_id
    return True
