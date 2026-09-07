"""Pure catalog reconciler — no I/O, no backend imports.

Produces a ``ReconcilePlan`` from desired assertions, remote state, ledger,
coverage, and suppressions. All catalog sinks share this function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from redibis.services.catalog.assertions import (
    Assertion,
    AssertionKey,
    Authority,
    CoverageRecord,
    CoverageStatus,
    Facet,
    LedgerEntry,
    Suppression,
    value_hash,
)


class ReconcileMode(str, Enum):
    NORMAL = "normal"  # human always wins; conflicts reported, not written
    ENFORCE = "enforce"  # Redibis wins; displaced values archived


@dataclass
class ReconcilePolicy:
    """Ownership / publish policy. Docstring of the ownership matrix lives here.

    | Facet | Redibis may write | Notes |
    |---|---|---|
    | TABLE/COLUMN_DESCRIPTION, COLUMN_DISPLAY_NAME | only if empty or Redibis-owned | never overwrite a steward |
    | PII_TAG / POLICY_TAG / ENTITY_TAG | yes, exclusive | Redibis owns PII.* / Redibis.* / RedibisPolicy.* |
    | GLOSSARY_LINK | yes, additive | never remove a human-added term link |
    | QUALITY_TEST | yes, exclusive for redibis_* cases | never touch other suites |
    """

    publish_threshold: float = 0.60  # below this: do not publish at all
    confirm_threshold: float = 0.85  # at/above: Confirmed, else Suggested
    delete_after_negatives: int = 2  # consecutive EVALUATED negatives to delete
    redibis_exclusive_facets: frozenset[Facet] = frozenset(
        {
            Facet.PII_TAG,
            Facet.POLICY_TAG,
            Facet.ENTITY_TAG,
            Facet.QUALITY_TEST,
        }
    )
    additive_facets: frozenset[Facet] = frozenset({Facet.GLOSSARY_LINK})
    clear_suppressions: bool = False  # only meaningful with ENFORCE


@dataclass
class IntentOp:
    op: Literal["add", "update", "remove", "demote", "promote"]
    key: AssertionKey
    value: Any = None
    prior: Any = None
    state: str = "confirmed"
    displaces: Optional[Authority] = None
    reason: str = ""


@dataclass
class Conflict:
    key: AssertionKey
    desired: Any
    remote: Any
    remote_authority: Authority
    reason: str


@dataclass
class ReconcilePlan:
    backend: str
    asset_fqn: str
    ops: list[IntentOp] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    skipped_uncovered: list[AssertionKey] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def _key_sort_tuple(key: AssertionKey) -> tuple:
    facet = key.facet.value if isinstance(key.facet, Facet) else str(key.facet)
    return (key.asset_fqn, key.column_path, facet, key.value_key)


def _is_expired(sup: Suppression, *, now: datetime) -> bool:
    if sup.expires_at is None:
        return False
    exp = sup.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= now


def _summarize(plan: ReconcilePlan) -> dict[str, int]:
    counts = {
        "adds": 0,
        "updates": 0,
        "removes": 0,
        "demotes": 0,
        "promotes": 0,
        "conflicts": len(plan.conflicts),
        "uncovered": len(plan.skipped_uncovered),
    }
    for op in plan.ops:
        if op.op == "add":
            counts["adds"] += 1
        elif op.op == "update":
            counts["updates"] += 1
        elif op.op == "remove":
            counts["removes"] += 1
        elif op.op == "demote":
            counts["demotes"] += 1
        elif op.op == "promote":
            counts["promotes"] += 1
    return counts


def reconcile(
    *,
    backend: str,
    asset_fqn: str,
    desired: list[Assertion],
    remote: list[Assertion],
    ledger: list[LedgerEntry],
    coverage: list[CoverageRecord],
    suppressions: list[Suppression],
    mode: ReconcileMode = ReconcileMode.NORMAL,
    policy: Optional[ReconcilePolicy] = None,
    now: Optional[datetime] = None,
) -> ReconcilePlan:
    """Compute a reconcile plan. Pure — no I/O.

    Critical order: coverage → suppression → ledger ownership → negative
    (demote/remove) → confidence floor → authority gate → add/update.
    """
    policy = policy or ReconcilePolicy()
    now = now or datetime.now(timezone.utc)
    plan = ReconcilePlan(backend=backend, asset_fqn=asset_fqn)

    covered = {
        (c.asset_fqn, c.column_path, c.facet)
        for c in coverage
        if c.status is CoverageStatus.EVALUATED
    }

    desired_by_key: dict[AssertionKey, Assertion] = {a.key: a for a in desired}
    remote_by_key: dict[AssertionKey, Assertion] = {a.key: a for a in remote}
    ledger_by_key: dict[AssertionKey, LedgerEntry] = {e.key: e for e in ledger}

    suppression_by_key: dict[AssertionKey, Suppression] = {}
    for s in suppressions:
        if _is_expired(s, now=now):
            continue
        suppression_by_key[s.key] = s

    candidate_keys = set(desired_by_key) | set(ledger_by_key)

    for key in sorted(candidate_keys, key=_key_sort_tuple):
        # ── 1. Coverage gate. Never delete or modify outside evaluated scope. ──
        if (key.asset_fqn, key.column_path, key.facet) not in covered:
            plan.skipped_uncovered.append(key)
            continue

        d = desired_by_key.get(key)
        r = remote_by_key.get(key)
        led = ledger_by_key.get(key)
        sup = suppression_by_key.get(key)

        # ── 2. Suppression gate. Human veto outranks a positive assertion. ──
        if sup and not (mode is ReconcileMode.ENFORCE and policy.clear_suppressions):
            if led:
                plan.ops.append(
                    IntentOp(op="remove", key=key, prior=r.value if r else None, reason="suppressed")
                )
            continue

        # ── 3. Ownership of the remote value. LEDGER FIRST, never labelType. ──
        if led is not None and r is not None and led.value_hash == value_hash(r.value):
            remote_auth: Optional[Authority] = Authority.REDIBIS
        elif r is None:
            remote_auth = None
        else:
            remote_auth = r.authority  # from sink read

        # ── 4. Negative: Redibis no longer asserts this fact. ──
        if d is None:
            if led is None:
                continue  # never ours — leave it alone
            if r is None:
                # Already absent remotely — drop ledger (world matches intent).
                plan.ops.append(
                    IntentOp(
                        op="remove",
                        key=key,
                        reason="already absent remotely; dropping ledger entry",
                    )
                )
                continue
            if remote_auth is not Authority.REDIBIS and mode is ReconcileMode.NORMAL:
                plan.conflicts.append(
                    Conflict(
                        key=key,
                        desired=None,
                        remote=r.value,
                        remote_authority=remote_auth or Authority.UNKNOWN,
                        reason="ledger says ours but remote value diverged",
                    )
                )
                continue
            if (
                led.state == "confirmed"
                and led.negative_streak + 1 < policy.delete_after_negatives
            ):
                plan.ops.append(
                    IntentOp(
                        op="demote",
                        key=key,
                        value=r.value,
                        prior=r.value,
                        state="suggested",
                        reason=f"negative streak {led.negative_streak + 1}",
                    )
                )
            else:
                plan.ops.append(
                    IntentOp(
                        op="remove",
                        key=key,
                        prior=r.value,
                        reason="no longer asserted",
                    )
                )
            continue

        # ── 5. Confidence floor. ──
        if d.confidence < policy.publish_threshold:
            if led:
                plan.ops.append(
                    IntentOp(
                        op="remove",
                        key=key,
                        prior=r.value if r else None,
                        reason="below publish threshold",
                    )
                )
            continue
        target_state = (
            "confirmed" if d.confidence >= policy.confirm_threshold else "suggested"
        )

        # Already correct — no-op regardless of who owns the remote value.
        if (
            r is not None
            and value_hash(r.value) == value_hash(d.value)
            and (led is None or led.state == target_state)
        ):
            continue

        # ── 6. Authority gate. ──
        if remote_auth is Authority.HUMAN and mode is ReconcileMode.NORMAL:
            if key.facet in policy.additive_facets:
                pass  # additive facets never conflict
            else:
                plan.conflicts.append(
                    Conflict(
                        key=key,
                        desired=d.value,
                        remote=r.value if r else None,
                        remote_authority=Authority.HUMAN,
                        reason="human-owned",
                    )
                )
                continue
        # Deliberate: connector-owned facets are never writable, even under ENFORCE.
        # --enforce overrides *human* authority only. Physical schema stays the
        # connector's (ownership matrix, ReconcilePolicy docstring).
        if (
            remote_auth in (Authority.EXTERNAL, Authority.PROPAGATED)
            and key.facet not in policy.redibis_exclusive_facets
        ):
            plan.conflicts.append(
                Conflict(
                    key=key,
                    desired=d.value,
                    remote=r.value if r else None,
                    remote_authority=remote_auth,
                    reason="connector-owned",
                )
            )
            continue

        # ── 7. Emit. ──
        if r is None:
            plan.ops.append(
                IntentOp(
                    op="add",
                    key=key,
                    value=d.value,
                    state=target_state,
                    reason="desired absent remotely",
                )
            )
        elif value_hash(r.value) != value_hash(d.value) or (
            led is not None and led.state != target_state
        ):
            # State-only change may be promote/demote; value change is update.
            if (
                led is not None
                and value_hash(r.value) == value_hash(d.value)
                and led.state != target_state
            ):
                if target_state == "confirmed" and led.state == "suggested":
                    op_name: Literal["add", "update", "remove", "demote", "promote"] = (
                        "promote"
                    )
                elif target_state == "suggested" and led.state == "confirmed":
                    op_name = "demote"
                else:
                    op_name = "update"
            else:
                op_name = "update"
            plan.ops.append(
                IntentOp(
                    op=op_name,
                    key=key,
                    value=d.value,
                    prior=r.value,
                    state=target_state,
                    displaces=Authority.HUMAN if remote_auth is Authority.HUMAN else None,
                    reason="value or state changed",
                )
            )
        # else: no-op, already correct

    plan.summary = _summarize(plan)
    return plan


def render_reconcile_plan(plan: ReconcilePlan, *, table: str = "") -> str:
    """Human-readable dry-run diff for a ``ReconcilePlan``."""
    arrow_table = table or plan.asset_fqn
    lines = [f"{plan.backend} ← {arrow_table}  →  {plan.asset_fqn}", ""]

    def _col_label(key: AssertionKey) -> str:
        if not key.column_path:
            return "(table)"
        # Prefer short table.column when table looks like schema.table
        short = arrow_table.split(".")[-1] if arrow_table else ""
        if short and key.column_path:
            return f"{short}.{key.column_path}"
        return key.column_path

    def _facet_label(key: AssertionKey) -> str:
        return key.facet.value if isinstance(key.facet, Facet) else str(key.facet)

    def _value_label(key: AssertionKey, value: Any) -> str:
        if key.value_key:
            return key.value_key
        if value is None:
            return ""
        text = str(value)
        return text if len(text) <= 48 else text[:45] + "..."

    for op in plan.ops:
        facet = _facet_label(op.key)
        col = _col_label(op.key)
        val = _value_label(op.key, op.value)
        state_cap = (op.state or "confirmed").capitalize()
        if op.op == "add":
            lines.append(
                f"  + {facet:<14} {col:<24} {val:<22} (conf —, {state_cap})"
            )
        elif op.op == "remove":
            reason = op.reason or "removed"
            lines.append(f"  - {facet:<14} {col:<24} {val or op.key.value_key:<22} ({reason})")
        elif op.op == "demote":
            lines.append(
                f"  ~ {facet:<14} {col:<24} {val or op.key.value_key:<22} "
                f"Confirmed → Suggested"
            )
            if op.reason:
                lines.append(f"                                                                 ({op.reason})")
        elif op.op == "promote":
            lines.append(
                f"  ~ {facet:<14} {col:<24} {val or op.key.value_key:<22} "
                f"Suggested → Confirmed"
            )
        elif op.op == "update":
            lines.append(
                f"  ~ {facet:<14} {col:<24} {val:<22} ({state_cap})"
            )
            if op.displaces is Authority.HUMAN:
                lines.append(
                    "                                                                 "
                    "(displaces human)"
                )

    for conflict in plan.conflicts:
        facet = _facet_label(conflict.key)
        col = _col_label(conflict.key)
        lines.append(
            f"  ! CONFLICT       {col:<24} {facet:<22} {conflict.reason}, skipped"
        )

    for key in plan.skipped_uncovered:
        facet = _facet_label(key)
        col = _col_label(key)
        lines.append(
            f"  ⊘ UNCOVERED      {col:<24} {facet:<22} (sampling skipped)"
        )

    s = plan.summary or _summarize(plan)
    overrides = sum(1 for op in plan.ops if op.displaces is Authority.HUMAN)
    lines.append("")
    lines.append(
        f"  {s.get('adds', 0)} adds · {s.get('demotes', 0)} demote · "
        f"{s.get('removes', 0)} remove · {s.get('conflicts', 0)} conflict · "
        f"{s.get('uncovered', 0)} uncovered · {overrides} human overrides"
    )
    return "\n".join(lines)
