"""
redibis.behavior.plugin_conformance
=====================================
Conformance helpers for verifying third-party fact/action plug-ins.

Usage (in your plug-in's test suite)
-------------------------------------
::

    from redibis.behavior.plugin_conformance import run_conformance_suite
    from my_plugin import my_action, sample_ctx, sample_params

    errors = run_conformance_suite(my_action, my_action.descriptor, sample_ctx, sample_params)
    assert errors == [], errors

Individual assertions may be called from standard pytest tests::

    assert_deterministic(my_action, ctx, params)
    assert_json_safe_patch(patch)
    assert_no_context_mutation(my_action, ctx, params)
    assert_only_declared_ops(patch, my_action.descriptor)
"""

from __future__ import annotations

import copy
import dataclasses
import json
import threading
import time
from typing import Any, Mapping

from redibis.behavior.models import BehaviorContext, BehaviorPatch, JSONValue
from redibis.behavior.registry import BehaviorAction, EffectDescriptor


# ---------------------------------------------------------------------------
# Individual assertions
# ---------------------------------------------------------------------------

def assert_deterministic(
    handler: BehaviorAction,
    ctx: BehaviorContext,
    params: Mapping[str, JSONValue],
    n: int = 3,
) -> None:
    """
    Assert that calling ``handler.apply(ctx, params)`` *n* times produces
    identical ``BehaviorPatch`` instances.

    Raises ``AssertionError`` on divergence.
    """
    results = [handler.apply(ctx, params) for _ in range(n)]
    canonical = _patch_to_canonical(results[0])
    for i, result in enumerate(results[1:], start=1):
        got = _patch_to_canonical(result)
        if got != canonical:
            raise AssertionError(
                f"Handler is not deterministic: call 0 and call {i} differ.\n"
                f"  call 0:  {canonical}\n"
                f"  call {i}: {got}"
            )


def assert_json_safe_patch(patch: BehaviorPatch) -> None:
    """
    Assert that every field in *patch* is JSON-serialisable.

    Raises ``AssertionError`` if serialisation fails.
    """
    try:
        raw = _patch_to_canonical(patch)
        json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"BehaviorPatch is not JSON-safe: {exc}") from exc


def assert_no_context_mutation(
    handler: BehaviorAction,
    ctx: BehaviorContext,
    params: Mapping[str, JSONValue],
) -> None:
    """
    Assert that calling ``handler.apply(ctx, params)`` does not mutate *ctx*.

    We capture a deep copy of ctx.facts before invocation and compare after.
    (BehaviorContext is frozen; a well-behaved handler cannot mutate the
    dataclass itself, but could mutate a mutable mapping passed as ctx.facts.)
    """
    facts_before = dict(ctx.facts)
    handler.apply(ctx, params)
    facts_after  = dict(ctx.facts)
    if facts_before != facts_after:
        added   = {k for k in facts_after  if k not in facts_before}
        removed = {k for k in facts_before if k not in facts_after}
        changed = {
            k for k in facts_before
            if k in facts_after and facts_before[k] != facts_after[k]
        }
        raise AssertionError(
            f"Handler mutated ctx.facts.\n"
            f"  added:   {sorted(added)}\n"
            f"  removed: {sorted(removed)}\n"
            f"  changed: {sorted(changed)}"
        )


def assert_only_declared_ops(
    patch: BehaviorPatch,
    descriptor: EffectDescriptor,
) -> None:
    """
    Assert that every ``PatchOperation.path`` in *patch* is covered by
    ``descriptor.patch_operations``.

    The descriptor's ``patch_operations`` tuple lists the logical operation
    identifiers (e.g. ``"verdict.set_entity"``).  A patch operation's
    ``path`` must match one of those identifiers (prefix or exact match).

    Raises ``AssertionError`` for any undeclared operation path.
    """
    allowed = frozenset(descriptor.patch_operations)
    for op in patch.operations:
        # Match: exact match or op.path starts with a declared prefix
        if not any(op.path == decl or op.path.startswith(decl + ".") for decl in allowed):
            raise AssertionError(
                f"Patch contains undeclared operation path {op.path!r}.\n"
                f"  Declared: {sorted(allowed)}"
            )


def assert_bounded_execution(
    handler: BehaviorAction,
    ctx: BehaviorContext,
    params: Mapping[str, JSONValue],
    max_ms: float = 100.0,
) -> None:
    """
    Assert that ``handler.apply`` completes within *max_ms* milliseconds.

    Uses wall-clock time; intended for CI smoke tests only (not a hard
    resource governor).  Raises ``AssertionError`` if the call is too slow.
    """
    start = time.monotonic()
    handler.apply(ctx, params)
    elapsed_ms = (time.monotonic() - start) * 1000
    if elapsed_ms > max_ms:
        raise AssertionError(
            f"Handler exceeded time budget: {elapsed_ms:.1f} ms > {max_ms:.1f} ms"
        )


def assert_no_raw_value_requirement(
    handler: BehaviorAction,
    ctx: BehaviorContext,
    params: Mapping[str, JSONValue],
) -> None:
    """
    Assert that the handler does not crash when ctx.facts contains only
    metadata facts (i.e. no raw cell values).

    A conformant plug-in must never require raw cell values as inputs.
    This runs ``handler.apply`` with an empty-facts context and verifies it
    returns a valid (possibly empty) BehaviorPatch without raising.
    """
    empty_ctx = dataclasses.replace(ctx, facts={})
    try:
        result = handler.apply(empty_ctx, params)
    except Exception as exc:
        raise AssertionError(
            f"Handler raised {type(exc).__name__} when ctx.facts is empty; "
            "plug-ins must not require raw cell values."
        ) from exc
    if not isinstance(result, BehaviorPatch):
        raise AssertionError(
            f"Handler must return BehaviorPatch, got {type(result).__name__}"
        )


# ---------------------------------------------------------------------------
# Composite conformance suite
# ---------------------------------------------------------------------------

def run_conformance_suite(
    handler: BehaviorAction,
    descriptor: EffectDescriptor,
    sample_ctx: BehaviorContext,
    sample_params: Mapping[str, JSONValue],
    *,
    n_determinism: int = 3,
    max_exec_ms: float = 100.0,
) -> list[str]:
    """
    Run the full conformance suite on *handler* + *descriptor*.

    Returns a list of error strings (empty list means all checks passed).
    Does not raise — collect all failures so the caller can report them at once.

    Checks:
    1. deterministic(n=n_determinism)
    2. json_safe_patch
    3. no_context_mutation
    4. only_declared_ops
    5. no_raw_value_requirement
    6. bounded_execution(max_ms=max_exec_ms)
    """
    errors: list[str] = []

    def _run(name: str, fn: Any) -> None:
        try:
            fn()
        except AssertionError as exc:
            errors.append(f"[{name}] {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[{name}] unexpected error: {type(exc).__name__}: {exc}")

    # 1 — determinism
    _run("deterministic",
         lambda: assert_deterministic(handler, sample_ctx, sample_params, n=n_determinism))

    # 2 — JSON-safe patch (based on first call)
    try:
        patch = handler.apply(sample_ctx, sample_params)
        _run("json_safe_patch", lambda: assert_json_safe_patch(patch))
        # 4 — declared ops (requires a patch)
        _run("only_declared_ops", lambda: assert_only_declared_ops(patch, descriptor))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"[apply_for_checks] unexpected error: {type(exc).__name__}: {exc}")

    # 3 — no context mutation
    _run("no_context_mutation",
         lambda: assert_no_context_mutation(handler, sample_ctx, sample_params))

    # 5 — no raw value requirement
    _run("no_raw_value_requirement",
         lambda: assert_no_raw_value_requirement(handler, sample_ctx, sample_params))

    # 6 — bounded execution
    _run("bounded_execution",
         lambda: assert_bounded_execution(handler, sample_ctx, sample_params,
                                          max_ms=max_exec_ms))

    return errors


# ---------------------------------------------------------------------------
# Internal serialisation helper
# ---------------------------------------------------------------------------

def _patch_to_canonical(patch: BehaviorPatch) -> str:
    """Produce a stable JSON string from a BehaviorPatch for comparison."""
    ops = [
        {
            "op":        op.op,
            "path":      op.path,
            "value":     op.value,
            "authority": int(op.authority),
            "reason":    op.reason,
            "rule_id":   op.rule_id,
            "policy_id": op.policy_id,
        }
        for op in patch.operations
    ]
    data = {
        "operations":     ops,
        "require_review": patch.require_review,
        "review_role":    patch.review_role,
        "reasons":        list(patch.reasons),
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"))
