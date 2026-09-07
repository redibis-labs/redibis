"""
redibis.behavior.learning
==========================
Behavior Policy Learning Loop — Phase 9.

Tracks human outcomes for policy-influenced verdicts, computes per-rule
precision signals, and generates narrowing suggestions and draft proposals
from repeated corrections.

Design invariants
-----------------
1. NO raw values are stored. Only value-free labels: policy/rule/SHA,
   verdict hashes (context_hash), original verdict class, policy verdict
   class, human result, and timestamps.
2. Rules are NEVER automatically disabled or rewritten. ``suggest_narrow_or_retire``
   returns ``Suggestion`` objects for human review — not mutations.
3. ``draft_from_repeated_corrections`` returns an Optional dict (a draft
   document) that requires human approval before activation. It is NOT
   auto-activated and is NOT passed to ``BehaviorPolicyStore.activate()``.
4. Signals are estimated, not ground-truth P/R — field names make this clear.
   ``estimated_precision_delta`` is not claimed to be full precision/recall.
5. This module has no dependency on CLI, webapp, LangGraph, or CopilotKit.
6. Persistence is JSONL — one record per line, append-only, under an
   ``outcomes/`` directory sibling to the policy store base directory.

Storage layout
--------------
    {base_dir}/outcomes/
        {policy_id}/
            {rule_id}.jsonl          # one OutcomeLabel per line

Outcome labels are immutable once written (append-only, no update/delete).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence


# ---------------------------------------------------------------------------
# Value-free label model
# ---------------------------------------------------------------------------

@dataclass
class OutcomeLabel:
    """
    Value-free record of a human outcome for a policy-influenced verdict.

    Fields
    ------
    policy_id:        The policy that influenced the verdict.
    rule_id:          The specific rule within that policy.
    policy_sha256:    Content SHA of the policy at evaluation time.
    original_verdict: Serialized original engine verdict class/bool/label.
                      MUST be a class label, never a raw value.
    policy_verdict:   Serialized policy-modified verdict class/bool/label.
    human_result:     Human outcome: "confirmed" | "reversed" | "unknown".
    context_hash:     SHA256 of the value-free BehaviorContext fact map.
                      No raw values in context_hash input.
    engine:           Engine name (pii, quality, classification, profiling, masking).
    table:            Table identifier (physical name).
    column:           Column name (may be None for table-level outcomes).
    ts:               ISO-8601 UTC timestamp.
    """
    policy_id: str
    rule_id: str
    policy_sha256: str
    original_verdict: str
    policy_verdict: str
    human_result: str          # "confirmed" | "reversed" | "unknown"
    context_hash: str
    engine: str
    table: str
    column: Optional[str] = None
    ts: str = field(default_factory=lambda: _utc_now())

    def __post_init__(self) -> None:
        valid_results = {"confirmed", "reversed", "unknown"}
        if self.human_result not in valid_results:
            raise ValueError(
                f"human_result must be one of {valid_results}, got {self.human_result!r}"
            )
        if not self.policy_id or not self.rule_id:
            raise ValueError("policy_id and rule_id must be non-empty")
        if not self.context_hash:
            raise ValueError("context_hash must be non-empty")


# ---------------------------------------------------------------------------
# Signal and suggestion models
# ---------------------------------------------------------------------------

@dataclass
class RuleSignal:
    """
    Aggregate precision signals for a single rule.

    ``estimated_precision_delta`` is an estimate based on available labels
    — it does NOT claim full precision/recall with ground-truth coverage.
    It represents (confirmations - reversals) / applications, normalized
    to [-1, 1].

    ``unknown_conflict_rate`` is the fraction of outcomes that are "unknown"
    or arrived with conflicting human decisions.
    """
    policy_id: str
    rule_id: str
    policy_sha256: str
    engine: str
    applications: int
    confirmations: int
    reversals: int
    unknown_count: int
    unknown_conflict_rate: float
    estimated_precision_delta: float
    domain_distribution: dict[str, int]    # table → count


@dataclass
class Suggestion:
    """
    A human-review suggestion produced by ``suggest_narrow_or_retire``.

    Suggestions are NOT automated mutations. They are delivered to a human
    operator through a review queue or dashboard.

    ``kind``: "narrow_scope" | "review_needed" | "consider_retirement"
    ``reason``: human-readable explanation.
    ``supporting_signals``: key signal fields for audit context.
    """
    policy_id: str
    rule_id: str
    kind: str
    reason: str
    supporting_signals: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _outcomes_path(base_dir: Path, policy_id: str, rule_id: str) -> Path:
    from redibis.behavior.ids import validate_policy_id, validate_rule_id

    safe_policy = validate_policy_id(policy_id)
    safe_rule = validate_rule_id(rule_id)
    base = Path(base_dir).resolve()
    dest = (base / "outcomes" / safe_policy / f"{safe_rule}.jsonl").resolve()
    if base not in dest.parents:
        raise ValueError(f"outcome path escapes base: {policy_id!r}/{rule_id!r}")
    return dest


def make_context_hash(facts: dict) -> str:
    """
    Create a value-free context hash from a fact map.

    Hashes the JSON-serialized sorted fact map. If fact values might contain
    raw column content, the caller MUST strip them before calling this.
    Use only aggregate/structural facts (scores, rates, types, counts).
    """
    canonical = json.dumps(
        {k: v for k, v in sorted(facts.items())},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# T9.1 — Record outcome label
# ---------------------------------------------------------------------------

def record_outcome_label(
    base_dir: str | Path,
    label: OutcomeLabel,
) -> Path:
    """
    Persist a value-free outcome label as an append-only JSONL record.

    Returns the path written to.

    The label is stored as a single JSON line under:
        {base_dir}/outcomes/{policy_id}/{rule_id}.jsonl

    Fields containing raw values must NEVER be passed in ``label``.
    ``context_hash`` must already be computed from a value-free fact map.
    """
    dest = _outcomes_path(Path(base_dir), label.policy_id, label.rule_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    record = asdict(label)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return dest


def load_outcome_labels(
    base_dir: str | Path,
    policy_id: Optional[str] = None,
    rule_id: Optional[str] = None,
) -> list[OutcomeLabel]:
    """
    Load stored outcome labels, optionally filtered by policy/rule.

    Returns an empty list if no labels are found.
    """
    from redibis.behavior.ids import validate_policy_id, validate_rule_id

    base = Path(base_dir) / "outcomes"
    if not base.exists():
        return []

    results: list[OutcomeLabel] = []

    if policy_id:
        try:
            policy_dirs = [base / validate_policy_id(policy_id)]
        except ValueError:
            return []
    else:
        policy_dirs = [d for d in base.iterdir() if d.is_dir()]

    for policy_dir in policy_dirs:
        if not policy_dir.exists() or not policy_dir.is_dir():
            continue
        if rule_id:
            try:
                pattern = f"{validate_rule_id(rule_id)}.jsonl"
            except ValueError:
                continue
        else:
            pattern = "*.jsonl"
        for jsonl_file in sorted(policy_dir.glob(pattern)):
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    results.append(OutcomeLabel(**d))
                except Exception:
                    continue

    return results


# ---------------------------------------------------------------------------
# T9.2 — Compute per-rule signals
# ---------------------------------------------------------------------------

def compute_rule_signals(
    store_or_path: str | Path,
    policy_id: Optional[str] = None,
) -> list[RuleSignal]:
    """
    Compute precision signals per (policy_id, rule_id) pair.

    ``store_or_path``: base directory where the outcome JSONL files live.
    ``policy_id``: if provided, restrict to this policy; otherwise all.

    Returns a list of ``RuleSignal`` — one per observed (policy_id, rule_id).

    Signal fields
    -------------
    - applications:              total outcome records for this rule.
    - confirmations:             human_result == "confirmed".
    - reversals:                 human_result == "reversed".
    - unknown_count:             human_result == "unknown".
    - unknown_conflict_rate:     unknown_count / applications.
    - estimated_precision_delta: (confirmations - reversals) / applications,
                                 bounded to [-1, 1]. ESTIMATE ONLY — not
                                 full precision/recall with ground-truth.
    - domain_distribution:       per-table application counts.
    """
    labels = load_outcome_labels(store_or_path, policy_id=policy_id)
    if not labels:
        return []

    # Group by (policy_id, rule_id)
    groups: dict[tuple[str, str], list[OutcomeLabel]] = {}
    for lbl in labels:
        key = (lbl.policy_id, lbl.rule_id)
        groups.setdefault(key, []).append(lbl)

    signals: list[RuleSignal] = []
    for (pid, rid), group in sorted(groups.items()):
        applications = len(group)
        confirmations = sum(1 for l in group if l.human_result == "confirmed")
        reversals = sum(1 for l in group if l.human_result == "reversed")
        unknown_count = sum(1 for l in group if l.human_result == "unknown")
        unknown_conflict_rate = unknown_count / applications if applications else 0.0

        if applications > 0:
            estimated_precision_delta = (confirmations - reversals) / applications
        else:
            estimated_precision_delta = 0.0
        estimated_precision_delta = max(-1.0, min(1.0, estimated_precision_delta))

        domain_distribution: dict[str, int] = {}
        for lbl in group:
            domain_distribution[lbl.table] = domain_distribution.get(lbl.table, 0) + 1

        # Use most recent SHA
        policy_sha256 = group[-1].policy_sha256
        engine = group[-1].engine

        signals.append(RuleSignal(
            policy_id=pid,
            rule_id=rid,
            policy_sha256=policy_sha256,
            engine=engine,
            applications=applications,
            confirmations=confirmations,
            reversals=reversals,
            unknown_count=unknown_count,
            unknown_conflict_rate=unknown_conflict_rate,
            estimated_precision_delta=estimated_precision_delta,
            domain_distribution=domain_distribution,
        ))

    return signals


# ---------------------------------------------------------------------------
# T9.3 — Suggest narrowing or retirement
# ---------------------------------------------------------------------------

_HIGH_REVERSAL_RATE_THRESHOLD = 0.4
_HIGH_UNKNOWN_RATE_THRESHOLD  = 0.5
_MIN_APPLICATIONS_FOR_SIGNAL  = 5
_BROAD_SCOPE_TABLE_THRESHOLD  = 5


def suggest_narrow_or_retire(signals: Sequence[RuleSignal]) -> list[Suggestion]:
    """
    Produce human-review suggestions for rules with adverse signals.

    Rules are NEVER automatically disabled or rewritten. Suggestions are
    for human review only.

    Thresholds (configurable in a later phase):
    - reversal_rate >= 0.40  → "consider_retirement" or "review_needed"
    - unknown_rate  >= 0.50  → "review_needed"
    - tables > 5 and reversal_rate > 0 → "narrow_scope"
    - Minimum applications for any signal: 5

    Returns an empty list if no signals cross the thresholds.
    """
    suggestions: list[Suggestion] = []

    for sig in signals:
        if sig.applications < _MIN_APPLICATIONS_FOR_SIGNAL:
            continue

        reversal_rate = sig.reversals / sig.applications
        table_count = len(sig.domain_distribution)

        if reversal_rate >= _HIGH_REVERSAL_RATE_THRESHOLD:
            if reversal_rate >= 0.60:
                kind = "consider_retirement"
                reason = (
                    f"Rule '{sig.rule_id}' (policy '{sig.policy_id}') has a high reversal "
                    f"rate of {reversal_rate:.0%} across {sig.applications} labelled outcomes. "
                    "Consider retiring or substantially rewriting with narrower scope and "
                    "evidence guards."
                )
            else:
                kind = "review_needed"
                reason = (
                    f"Rule '{sig.rule_id}' (policy '{sig.policy_id}') has reversal rate "
                    f"{reversal_rate:.0%} ({sig.reversals}/{sig.applications}). "
                    "Review the rule condition and scope."
                )
            suggestions.append(Suggestion(
                policy_id=sig.policy_id,
                rule_id=sig.rule_id,
                kind=kind,
                reason=reason,
                supporting_signals={
                    "applications": sig.applications,
                    "reversals": sig.reversals,
                    "confirmations": sig.confirmations,
                    "reversal_rate": round(reversal_rate, 4),
                    "estimated_precision_delta": round(sig.estimated_precision_delta, 4),
                    "table_count": table_count,
                },
            ))
            continue  # Don't double-emit for same rule

        if sig.unknown_conflict_rate >= _HIGH_UNKNOWN_RATE_THRESHOLD:
            suggestions.append(Suggestion(
                policy_id=sig.policy_id,
                rule_id=sig.rule_id,
                kind="review_needed",
                reason=(
                    f"Rule '{sig.rule_id}' (policy '{sig.policy_id}') has a high unknown/conflict "
                    f"rate of {sig.unknown_conflict_rate:.0%} across {sig.applications} outcomes. "
                    "The rule may be triggering in ambiguous contexts."
                ),
                supporting_signals={
                    "applications": sig.applications,
                    "unknown_count": sig.unknown_count,
                    "unknown_conflict_rate": round(sig.unknown_conflict_rate, 4),
                },
            ))
            continue

        if table_count > _BROAD_SCOPE_TABLE_THRESHOLD and reversal_rate > 0.0:
            suggestions.append(Suggestion(
                policy_id=sig.policy_id,
                rule_id=sig.rule_id,
                kind="narrow_scope",
                reason=(
                    f"Rule '{sig.rule_id}' (policy '{sig.policy_id}') is active across "
                    f"{table_count} tables with some reversals ({sig.reversals}). "
                    "Consider narrowing the scope to tables with confirmed outcomes."
                ),
                supporting_signals={
                    "table_count": table_count,
                    "applications": sig.applications,
                    "reversals": sig.reversals,
                    "domain_distribution": sig.domain_distribution,
                },
            ))

    return suggestions


# ---------------------------------------------------------------------------
# T9.4 — Draft from repeated corrections
# ---------------------------------------------------------------------------

def draft_from_repeated_corrections(
    corrections: Sequence[dict],
    *,
    engine: str,
    stage: str = "post_verdict",
    min_repetitions: int = 3,
    policy_id: Optional[str] = None,
    description: str = "",
) -> Optional[dict]:
    """
    Generate a draft policy document from repeated compatible corrections.

    ``corrections`` is a list of dicts with at minimum:
        column, original_verdict, desired_verdict, reason, (optional) table

    Only corrections repeated at least ``min_repetitions`` times for the
    same (column, original_verdict, desired_verdict) pattern are included.

    Returns a draft document dict OR None if no pattern meets the threshold.

    IMPORTANT: The returned draft has ``lifecycle.requires_human_approval: true``
    and must go through validation, simulation, and human approval before
    activation. It is NEVER auto-activated.

    Source statistics are embedded in ``lifecycle.source_stats`` for audit.
    """
    if not corrections:
        return None

    # Group corrections by (column, original_verdict, desired_verdict).
    # Keep original types (bool/str) for draft emission — do not stringify bools.
    pattern_counts: dict[tuple, list[dict]] = {}
    for c in corrections:
        col = str(c.get("column") or "")
        orig_raw = c.get("original_verdict")
        desired_raw = c.get("desired_verdict")
        if not col or orig_raw is None or desired_raw is None:
            continue
        key = (col, orig_raw, desired_raw)
        pattern_counts.setdefault(key, []).append(c)

    qualifying: list[tuple[tuple, list[dict]]] = [
        (k, v) for k, v in pattern_counts.items() if len(v) >= min_repetitions
    ]

    if not qualifying:
        return None

    from redibis.behavior.agent_tools import tool_propose_draft

    # Build correction example list from qualifying patterns
    examples: list[dict] = []
    tables_seen: set[str] = set()
    total_corrections = 0

    for (col, orig, desired), instances in qualifying:
        representative = instances[0]
        table = str(representative.get("table") or "")
        if table:
            tables_seen.add(table)
        reasons = list({str(i.get("reason") or "repeated correction") for i in instances})
        examples.append({
            "column": col,
            "table": table,
            "original_verdict": orig,
            "desired_verdict": desired,
            "reason": reasons[0] if reasons else "repeated correction",
        })
        total_corrections += len(instances)

    source_stats = {
        "total_corrections": total_corrections,
        "qualifying_patterns": len(qualifying),
        "min_repetitions": min_repetitions,
        "tables_observed": len(tables_seen),
    }

    draft = tool_propose_draft(
        examples,
        engine=engine,
        stage=stage,
        policy_id=policy_id,
        description=description or (
            f"Draft from {total_corrections} repeated corrections "
            f"({len(qualifying)} patterns, min_repetitions={min_repetitions})"
        ),
        source_stats=source_stats,
    )
    return draft
