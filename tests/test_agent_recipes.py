"""Golden agentic recipes must validate against the node registry.

A recipe that fails `validate_spec` would teach the IntentPlanner an invalid pattern,
so this is the guard the handover calls out.
"""

from __future__ import annotations

from redibis.agents.recipes import (
    RECIPES,
    _assert_recipes_valid,
    list_recipes,
    recipes_for_intent,
)


def test_all_recipes_pass_registry_validation():
    problems = _assert_recipes_valid()
    assert problems == [], "invalid golden recipes:\n" + "\n".join(problems)


def test_recipe_index_is_complete():
    idx = list_recipes()
    assert len(idx) == len(RECIPES)
    for r in idx:
        assert r["id"] and r["name"] and r["intent"] and r["tags"]


def test_recipes_for_intent_ranks_relevant_first():
    out = recipes_for_intent("mask the PII in these CSV files", k=3)
    assert out, "planner must always get at least one few-shot recipe"
    assert len(out) <= 3
    # the masking recipe should rank first for a masking intent
    assert "mask" in out[0]["plan"]["name"] or any(
        n["kind"] == "mask" for n in out[0]["plan"]["nodes"]
    )
    # shape matches the planner's emitted JSON
    for ex in out:
        assert "intent" in ex and "plan" in ex
        assert ex["plan"]["nodes"] and "edges" in ex["plan"]


def test_recipes_for_intent_always_returns_examples_on_no_match():
    out = recipes_for_intent("zzz totally unrelated gibberish", k=2)
    assert 1 <= len(out) <= 2
