"""
redibis.agents.recipes
======================
Curated golden ``intent → pipeline`` recipes.

Two jobs:
- **Few-shot grounding** for the ``IntentPlanner`` — the planner injects the top-k most
  relevant recipes (``PlannerContext.memory_recipes``) so a smaller/on-prem model maps NL
  intent to a valid PipelineSpec accurately.
- **Starting points** for the composer ("start from a recipe").

Each recipe's ``plan`` uses the SAME index-based JSON shape the planner emits (nodes with
``kind``/``label``/``params``, edges with integer ``source``/``target`` positions) and is
built to pass ``registry.validate_spec`` — so they are honest exemplars, not aspirational.

Node kinds and params come from ``redibis.agents.registry`` (the node catalog). Keep these
in sync with the registry; a tiny self-check (``_assert_recipes_valid``) is exercised by the
test suite.
"""

from __future__ import annotations

from typing import Any

# ── Golden recipes ────────────────────────────────────────────────────────────
# Linear-ish governance graphs. Note: a `contract` node needs BOTH a DataRef and a
# ProfileResult upstream, so `sample` and `profile` both feed it (two edges in).

RECIPES: list[dict[str, Any]] = [
    {
        "id": "onboard_schema_to_om",
        "name": "Onboard a schema → classify, contract, publish to OpenMetadata",
        "tags": ["onboard", "classify", "contract", "publish", "openmetadata", "schema", "hive", "batch"],
        "intent": "Onboard all tables in the schema: profile, detect PII, classify, build contracts, and publish to OpenMetadata.",
        "plan": {
            "name": "onboard_schema",
            "goal": "Classify and contract every table in a schema, then publish.",
            "nodes": [
                {"kind": "source", "label": "Source", "params": {"engine": "hive"}},
                {"kind": "sample", "label": "Sample", "params": {"strategy": "percent", "amount": 10.0}},
                {"kind": "profile", "label": "Profile", "params": {"engine": "great_expectations"}},
                {"kind": "contract", "label": "Contract", "params": {"pii": True, "classify": True, "quality": True}},
                {"kind": "gate", "label": "Approval", "params": {"role": "Steward"}},
                {"kind": "publish", "label": "Publish", "params": {"backend": "openmetadata"}},
            ],
            "edges": [
                {"source": 0, "target": 1},
                {"source": 1, "target": 2},
                {"source": 1, "target": 3},
                {"source": 2, "target": 3},
                {"source": 3, "target": 4},
                {"source": 4, "target": 5},
            ],
        },
    },
    {
        "id": "mask_local_csv_pii",
        "name": "Mask PII in local CSVs",
        "tags": ["mask", "masking", "pii", "local", "folder", "csv", "files"],
        "intent": "Detect PII in the uploaded CSV files and recommend a masking rule for each PII column.",
        "plan": {
            "name": "mask_local_pii",
            "goal": "Detect PII in local files and suggest masking.",
            "nodes": [
                {"kind": "source", "label": "Source", "params": {"engine": "folder"}},
                {"kind": "sample", "label": "Sample", "params": {"strategy": "all"}},
                {"kind": "profile", "label": "Profile", "params": {"engine": "great_expectations"}},
                {"kind": "contract", "label": "Contract", "params": {"pii": True, "mask_rules": True, "classify": False, "quality": False}},
                {"kind": "mask", "label": "Mask", "params": {}},
            ],
            "edges": [
                {"source": 0, "target": 1},
                {"source": 1, "target": 2},
                {"source": 1, "target": 3},
                {"source": 2, "target": 3},
                {"source": 3, "target": 4},
            ],
        },
    },
    {
        "id": "classify_oracle_schema",
        "name": "Classify an Oracle schema (no publish)",
        "tags": ["classify", "classification", "oracle", "jdbc", "tags", "contract"],
        "intent": "Connect to the Oracle schema, classify every table with the policy pack, and build contracts — do not publish yet.",
        "plan": {
            "name": "classify_oracle",
            "goal": "Classify and contract an Oracle schema for steward review.",
            "nodes": [
                {"kind": "source", "label": "Source", "params": {"engine": "oracle"}},
                {"kind": "sample", "label": "Sample", "params": {"strategy": "percent", "amount": 5.0}},
                {"kind": "profile", "label": "Profile", "params": {"engine": "open_metadata"}},
                {"kind": "contract", "label": "Contract", "params": {"pii": True, "classify": True, "quality": False}},
            ],
            "edges": [
                {"source": 0, "target": 1},
                {"source": 1, "target": 2},
                {"source": 1, "target": 3},
                {"source": 2, "target": 3},
            ],
        },
    },
    {
        "id": "profile_only_understand",
        "name": "Profile only — understand the data",
        "tags": ["profile", "understand", "explore", "stats", "dashboard", "no contract"],
        "intent": "Just profile the table so I can understand its shape and stats — no contract, no PII.",
        "plan": {
            "name": "profile_only",
            "goal": "Structural profiling for data understanding.",
            "nodes": [
                {"kind": "source", "label": "Source", "params": {"engine": "hive"}},
                {"kind": "sample", "label": "Sample", "params": {"strategy": "percent", "amount": 10.0}},
                {"kind": "profile", "label": "Profile", "params": {"engine": "open_metadata"}},
            ],
            "edges": [
                {"source": 0, "target": 1},
                {"source": 1, "target": 2},
            ],
        },
    },
    {
        "id": "quality_rules_postgres",
        "name": "Suggest quality rules for a Postgres table",
        "tags": ["quality", "rules", "postgres", "jdbc", "expectations", "contract"],
        "intent": "Profile the Postgres table and propose data-quality rules in the contract.",
        "plan": {
            "name": "quality_postgres",
            "goal": "Generate quality rules for a Postgres table.",
            "nodes": [
                {"kind": "source", "label": "Source", "params": {"engine": "postgres"}},
                {"kind": "sample", "label": "Sample", "params": {"strategy": "percent", "amount": 10.0}},
                {"kind": "profile", "label": "Profile", "params": {"engine": "great_expectations"}},
                {"kind": "contract", "label": "Contract", "params": {"quality": True, "pii": False, "classify": False}},
            ],
            "edges": [
                {"source": 0, "target": 1},
                {"source": 1, "target": 2},
                {"source": 1, "target": 3},
                {"source": 2, "target": 3},
            ],
        },
    },
]


def list_recipes() -> list[dict[str, Any]]:
    """Lightweight recipe index for the composer's 'start from a recipe' picker."""
    return [{"id": r["id"], "name": r["name"], "intent": r["intent"], "tags": r["tags"]} for r in RECIPES]


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    return next((r for r in RECIPES if r["id"] == recipe_id), None)


def _score(recipe: dict[str, Any], words: set[str]) -> int:
    tags = {t.lower() for t in recipe.get("tags", [])}
    overlap = len(tags & words)
    # small boost when the recipe's intent shares words with the query
    intent_words = set(recipe.get("intent", "").lower().split())
    overlap += len(intent_words & words) // 4
    return overlap


def recipes_for_intent(intent: str, k: int = 3) -> list[dict[str, Any]]:
    """Return the top-k most relevant recipes as planner few-shot examples.

    Shape matches the planner's emitted JSON: ``{"intent": ..., "plan": {...}}``.
    Falls back to the first ``k`` recipes when nothing matches (always give examples).
    """
    words = {w.strip(".,:;()").lower() for w in (intent or "").split() if w}
    scored = sorted(RECIPES, key=lambda r: _score(r, words), reverse=True)
    top = [r for r in scored if _score(r, words) > 0][:k] or RECIPES[:k]
    return [{"intent": r["intent"], "plan": r["plan"]} for r in top]


def _assert_recipes_valid() -> list[str]:
    """Build each recipe into a PipelineSpec and run registry validation (test hook)."""
    from redibis.agents.models import PipelineEdge, PipelineNode, PipelineSpec
    from redibis.agents.registry import validate_spec

    problems: list[str] = []
    for r in RECIPES:
        plan = r["plan"]
        nodes = [PipelineNode(kind=n["kind"], label=n.get("label", n["kind"]), params=dict(n.get("params", {})))
                 for n in plan["nodes"]]
        edges = [PipelineEdge(source=nodes[e["source"]].id, target=nodes[e["target"]].id)
                 for e in plan.get("edges", [])]
        spec = PipelineSpec(name=plan.get("name", r["id"]), goal=plan.get("goal", ""), nodes=nodes, edges=edges)
        errs = validate_spec(spec)
        if errs:
            problems.append(f"{r['id']}: " + "; ".join(e.message for e in errs))
    return problems
