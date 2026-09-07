"""
redibis.behavior.reducer
=========================
Capability-aware patch validator and reducer.

The reducer is the enforcement boundary between policy proposals
and actual engine behavior. It:
  1. Rejects operations unsupported by the adapter/stage.
  2. Rejects operations that violate value schemas or bounds.
  3. Applies authority precedence (higher authority wins).
  4. Detects incompatible scalar operations (same path, different values).
  5. Routes incompatible scalar verdict/security operations to review.
  6. Combines compatible additive operations deterministically.
  7. Returns proposed, applied, and blocked patches.
  8. Consults supplied human-decision authority to block demoted columns.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from redibis.behavior.models import (
    Authority,
    BehaviorCapabilities,
    BehaviorPatch,
    BlockedOperation,
    PatchOperation,
)


# ---------------------------------------------------------------------------
# Capability checking
# ---------------------------------------------------------------------------

def _is_operation_allowed(
    op: PatchOperation, capabilities: BehaviorCapabilities
) -> tuple[bool, str]:
    """
    Return (allowed, reason).

    Checks:
    - Is the path in allowed_patch_operations?
    - Is the path immutable?
    - Value constraint validation.
    """
    path = op.path

    if path in capabilities.immutable_paths:
        return False, f"path {path!r} is immutable for this adapter/stage"

    if capabilities.allowed_patch_operations and path not in capabilities.allowed_patch_operations:
        # Also check prefix-based membership (e.g. "verdict.*")
        short = path.split(".")[0]
        if f"{short}.*" not in capabilities.allowed_patch_operations:
            return False, f"operation path {path!r} is not in adapter capabilities"

    # Value constraints
    constraints = capabilities.value_constraints.get(path)
    if constraints and op.value is not None:
        min_val = constraints.get("minimum")
        max_val = constraints.get("maximum")
        if isinstance(op.value, (int, float)):
            v = float(op.value)
            if min_val is not None and v < float(min_val):
                return False, f"value {v} is below minimum {min_val} for {path!r}"
            if max_val is not None and v > float(max_val):
                return False, f"value {v} exceeds maximum {max_val} for {path!r}"

    return True, ""


# ---------------------------------------------------------------------------
# Authority ordering
# ---------------------------------------------------------------------------

def _higher_authority(a: Authority, b: Authority) -> Authority:
    return a if a.value >= b.value else b


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _operations_conflict(a: PatchOperation, b: PatchOperation) -> bool:
    """Two set-operations on the same path with different values conflict."""
    if a.op != "set" or b.op != "set":
        return False
    return a.path == b.path and a.value != b.value


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------

class PatchReducer:
    """
    Validates and reduces a proposed BehaviorPatch.

    Parameters
    ----------
    capabilities:
        Adapter-declared allowed operations and constraints.
    human_decisions:
        Optional mapping of path → Authority for human overlay decisions.
        E.g. {"verdict.entity": Authority.HUMAN_DECISION} means a human
        has made a durable decision on this field that policy cannot override.
    """

    def __init__(
        self,
        capabilities: BehaviorCapabilities,
        human_decisions: Optional[Mapping[str, Authority]] = None,
    ) -> None:
        self._caps = capabilities
        self._human = human_decisions or {}

    def reduce(
        self,
        proposed: BehaviorPatch,
    ) -> tuple[BehaviorPatch, BehaviorPatch, tuple[BlockedOperation, ...]]:
        """
        Validate and reduce a proposed patch.

        Returns
        -------
        (applied_patch, rejected_patch, blocked_operations)
        """
        applied_ops: list[PatchOperation] = []
        blocked_ops: list[BlockedOperation] = []

        # Track highest authority per path (for conflict resolution)
        path_authority: dict[str, tuple[Authority, PatchOperation]] = {}

        for op in proposed.operations:
            # 1. Capability check
            allowed, reason = _is_operation_allowed(op, self._caps)
            if not allowed:
                blocked_ops.append(BlockedOperation(
                    operation=op,
                    blocked_by="capability",
                    explanation=reason,
                ))
                continue

            # 2. Human decision overlay check
            human_auth = self._human.get(op.path)
            if human_auth is not None and human_auth.value > op.authority.value:
                blocked_ops.append(BlockedOperation(
                    operation=op,
                    blocked_by=human_auth.name,
                    explanation=(
                        f"Path {op.path!r} is protected by a durable human decision "
                        f"(authority={human_auth.name}); policy authority "
                        f"({op.authority.name}) cannot override it."
                    ),
                ))
                continue

            # 3. Conflict detection (same path, different value)
            existing = path_authority.get(op.path)
            if existing is not None:
                exist_auth, exist_op = existing
                if _operations_conflict(op, exist_op):
                    # Higher authority wins; lower authority routes to review
                    if op.authority.value > exist_auth.value:
                        # New op wins; block the existing op (retrospectively)
                        applied_ops = [o for o in applied_ops if o is not exist_op]
                        blocked_ops.append(BlockedOperation(
                            operation=exist_op,
                            blocked_by="conflict_resolution",
                            explanation=(
                                f"Operation on {op.path!r} from {exist_op.rule_id!r} "
                                f"overridden by higher-authority operation from {op.rule_id!r}"
                            ),
                        ))
                        applied_ops.append(op)
                        path_authority[op.path] = (op.authority, op)
                    elif op.authority.value < exist_auth.value:
                        blocked_ops.append(BlockedOperation(
                            operation=op,
                            blocked_by="conflict_resolution",
                            explanation=(
                                f"Operation on {op.path!r} from {op.rule_id!r} "
                                "blocked by a higher-authority operation that already applied."
                            ),
                        ))
                    else:
                        # Same authority — route to review (v1: REVIEW policy)
                        blocked_ops.append(BlockedOperation(
                            operation=op,
                            blocked_by="same_authority_conflict",
                            explanation=(
                                f"Incompatible scalar operations on {op.path!r} at "
                                f"the same authority level ({op.authority.name}); "
                                "routed to review per v1 conflict policy."
                            ),
                        ))
                        # Mark patch as requiring review
                        proposed = BehaviorPatch(
                            operations=proposed.operations,
                            require_review=True,
                            review_role=proposed.review_role or "steward",
                            reasons=proposed.reasons,
                        )
                else:
                    # Same path, same value → idempotent additive (just skip duplicate)
                    if op.value != exist_op.value:
                        applied_ops.append(op)
                        # Update to the higher authority
                        if op.authority.value > exist_auth.value:
                            path_authority[op.path] = (op.authority, op)
                    # else: identical, skip
            else:
                applied_ops.append(op)
                path_authority[op.path] = (op.authority, op)

        applied = BehaviorPatch(
            operations=tuple(applied_ops),
            require_review=proposed.require_review,
            review_role=proposed.review_role,
            reasons=proposed.reasons,
        )
        rejected = BehaviorPatch(
            operations=tuple(op.operation for op in blocked_ops),
        )

        return applied, rejected, tuple(blocked_ops)
