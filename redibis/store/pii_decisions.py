"""
redibis.store.pii_decisions — the PII decision overlay (single source of truth).

This module is the authoritative record of *which columns are PII and which are
not*, decoupled from the union-merge that drives normal upserts. It exists to
solve a structural problem:

  ``ContractMerger`` unions tags and never deletes omitted fields, so a normal
  ``ContractStore.upsert()`` can never REMOVE the ``pii`` block, ``maskingPolicy``,
  ``classification``, or the ``pii``/``gdpr_personal_data`` tags from a column.
  Stripping PII therefore needs an out-of-band decision that *overrides* the
  merge result.

The overlay is a per-table sidecar in the contracts bucket
(``_meta/pii_decisions/{table}.json`` — the same ``_meta/`` namespace already
used by ``AuthStore``). Every client (approved basket, /v2 manager, share links,
CLI) records decisions into this one object; ``ContractStore.upsert()`` then
*reconciles* each column against it right before writing, so the overlay always
wins regardless of what the merge produced.

Two halves:
  - Pure functions (no I/O): ``strip_pii_from_column``, ``apply_pii_to_column``,
    ``reconcile_pii_columns``, ``recompute_pii_summary``. Operate on contract dicts.
  - ``PiiDecisionStore``: reads/writes the sidecar JSON.

A decision is one of:
  - ``status="not_pii"`` — strip every PII signal from the column (demote to a
    normal business column).
  - ``status="pii"``     — ensure the column carries the PII signal described by
    ``payload`` (a full ODCS column fragment, as produced by
    ``PIIContractWriter`` / ``pii_row_to_fragment``). Used to ADD a PII column
    the scan never flagged.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend


# ── Constants: what makes a column "PII" ─────────────────────────────────────

#: Tags written by PIIContractWriter._build_tags. Removed when demoting.
PII_TAGS: frozenset[str] = frozenset({"pii", "gdpr_personal_data", "contains_arabic"})

#: Column-level keys that carry PII metadata. Removed when demoting.
PII_COLUMN_KEYS: tuple[str, ...] = ("privacy", "pii", "maskingPolicy", "entity_type")

#: customProperties entries (by ``property`` name) that carry PII metadata.
PII_CUSTOM_PROPS: frozenset[str] = frozenset({"pii", "maskingPolicy"})

#: Valid decision statuses.
VALID_STATUSES: frozenset[str] = frozenset({"pii", "not_pii"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_engine_baseline_from_telemetry(telemetry: Optional[dict]) -> dict:
    """Derive a compact engine baseline snapshot from column telemetry.

    Used when a human records a PII decision so training export can compute
    ``corrected_engine`` without a fragile join-time guess. Returns a dict with:

    - ``engine_is_pii``: ``True`` / ``False`` / ``None`` (unknown)
    - ``engine_entity_type``: str or None
    - ``engine_confidence``: float or None
    - ``engine_decision_rule``: str or None
    """
    tel = telemetry or {}
    if not isinstance(tel, dict) or not tel:
        return {
            "engine_is_pii": None,
            "engine_entity_type": None,
            "engine_confidence": None,
            "engine_decision_rule": None,
        }

    entity = str(tel.get("entity_type") or "") or None
    rule = str(tel.get("decision_rule") or "") or None
    conf_raw = tel.get("confidence")
    try:
        conf = float(conf_raw) if conf_raw is not None else None
    except (TypeError, ValueError):
        conf = None

    engines = tel.get("discovery_engines") or []
    engine_is_pii: Optional[bool] = None
    rule_l = (rule or "").lower()
    if "not_pii" in rule_l or "not pii" in rule_l:
        engine_is_pii = False
    elif entity and conf is not None and conf >= 0.5:
        engine_is_pii = True
    elif engines and conf is not None and conf >= 0.5:
        engine_is_pii = True
    elif conf is not None and conf < 0.2 and not entity:
        engine_is_pii = False
    elif entity:
        engine_is_pii = True

    return {
        "engine_is_pii": engine_is_pii,
        "engine_entity_type": entity,
        "engine_confidence": conf,
        "engine_decision_rule": rule,
    }


def _is_pii_classification(value: Any) -> bool:
    """A classification is PII-owned only when it starts with ``pii``.

    Business classifications (``internal``, ``public``, ``confidential`` …) are
    left untouched so demoting PII never clobbers a curated business value.
    """
    return isinstance(value, str) and value.lower().startswith("pii")


# ── Pure column operations ───────────────────────────────────────────────────

def strip_pii_from_column(col: dict) -> bool:
    """Remove every PII signal from a single column dict, in place.

    Returns True if anything changed. Leaves non-PII fields (description,
    businessName, quality, logicalType, business classifications, non-PII tags)
    intact, so the column becomes a normal business column.
    """
    from redibis.contracts.privacy import strip_privacy_from_column

    changed = strip_privacy_from_column(col)

    # classification — only drop PII-owned values
    if _is_pii_classification(col.get("classification")):
        col.pop("classification", None)
        changed = True

    # tags — filter out PII tags only (set-union merge can never do this)
    if "tags" in col:
        kept = [t for t in (col.get("tags") or []) if t not in PII_TAGS]
        if len(kept) != len(col.get("tags") or []):
            changed = True
        if kept:
            col["tags"] = kept
        else:
            col.pop("tags", None)

    return changed


def apply_pii_to_column(col: dict, payload: dict) -> bool:
    """Ensure a column carries the PII signal described by ``payload``.

    Accepts the canonical ``privacy`` block or legacy ``pii``/``maskingPolicy``.
    Returns True if anything changed.
    """
    from redibis.contracts.privacy import apply_privacy_to_column
    return apply_privacy_to_column(col, payload)


def _iter_columns(contract: dict):
    """Yield every column property dict across all schema objects."""
    for schema_obj in contract.get("schema", []) or []:
        for prop in schema_obj.get("properties", []) or []:
            if isinstance(prop, dict):
                yield prop


def reconcile_pii_columns(contract: dict, decisions: dict) -> list[str]:
    """Enforce the decision overlay on a contract dict, in place.

    For every decision:
      - ``not_pii`` → strip PII from the matching column.
      - ``pii``     → apply the decision payload to the matching column.

    Columns absent from the schema are skipped. Returns the list of column names
    actually changed (so callers can audit / recompute).
    """
    if not decisions:
        return []

    by_name: dict[str, dict] = {}
    for prop in _iter_columns(contract):
        name = prop.get("name")
        if name is not None:
            by_name.setdefault(name, prop)

    changed: list[str] = []
    for column, decision in decisions.items():
        col = by_name.get(column)
        if col is None:
            continue
        status = (decision or {}).get("status")
        lifecycle = str((decision or {}).get("lifecycle_state") or "active").lower()
        if lifecycle in ("stale", "superseded"):
            continue
        if status == "not_pii":
            if strip_pii_from_column(col):
                changed.append(column)
        elif status == "pii":
            if apply_pii_to_column(col, (decision or {}).get("payload") or {}):
                changed.append(column)
    return changed


def evaluate_and_mark_drift(table: str, merged_contract: dict, store: "PiiDecisionStore") -> list[str]:
    """Demote fingerprint-mismatched *active* decisions to ``stale`` on write.

    Called from ``ContractStore.upsert()`` — the single choke point every scan
    and merge workflow passes through — so drift is persisted at scan
    completion rather than as a side effect of a reviewer ``GET``. Only
    fingerprinted decisions are compared; legacy decisions without a
    ``fingerprint_key`` are left untouched (nothing to compare against).
    Returns the list of column names newly marked stale.
    """
    from redibis.review.drift import LIFECYCLE_ACTIVE, LIFECYCLE_STALE, evaluate_column_drift
    from redibis.review.fingerprint import ColumnFingerprintSnapshot, fingerprint_from_contract_prop

    decisions = store.get(table)
    if not decisions:
        return []
    by_name = {p.get("name"): p for p in _iter_columns(merged_contract) if p.get("name") is not None}
    stale_cols: list[str] = []
    for column, raw in decisions.items():
        if str(raw.get("lifecycle_state") or LIFECYCLE_ACTIVE) != LIFECYCLE_ACTIVE:
            continue
        if not raw.get("fingerprint_key"):
            continue
        baseline = ColumnFingerprintSnapshot.from_dict(raw)
        prop = by_name.get(column)
        current = fingerprint_from_contract_prop(column, prop) if prop is not None else None
        drift = evaluate_column_drift(baseline, current)
        if drift.state == LIFECYCLE_STALE:
            updated = PiiDecision.from_dict({**raw, "lifecycle_state": LIFECYCLE_STALE})
            store.set(table, updated)
            stale_cols.append(column)
    return stale_cols


def compute_pii_summary(contract: dict, preserve: Optional[dict] = None) -> dict:
    """Derive ``pii_summary`` from column privacy blocks (pure — no contract mutation)."""
    from redibis.contracts.privacy import column_is_pii, col_privacy_classification
    from redibis.pii.sensitivity import highest_sensitivity_level

    columns = list(_iter_columns(contract))
    confirmed = [c for c in columns if column_is_pii(c)]
    security = [
        c for c in columns
        if col_privacy_classification(c) == "security_sensitive"
        or "security_sensitive" in (c.get("tags") or [])
    ]
    classifications = [col_privacy_classification(c) for c in confirmed]
    all_tiers = classifications + ["security_sensitive"] * len(security)
    arabic = [c.get("name") for c in columns
              if "contains_arabic" in (c.get("tags") or [])]

    summary = dict(preserve or {})
    summary.update({
        "total_columns":        len(columns),
        "pii_confirmed":        len(confirmed),
        "pii_columns":          [c.get("name") for c in confirmed],
        "security_sensitive_columns": [c.get("name") for c in security],
        "arabic_aware_columns": arabic,
        "highest_sensitivity":  highest_sensitivity_level(all_tiers),
        "pii_clean":            len(columns) - len(confirmed) - len(security),
    })
    return summary


def recompute_pii_summary(contract: dict) -> dict:
    """Backward-compat: compute summary and attach to contract (prefer metadata store)."""
    prev = contract.pop("pii_summary", None) or {}
    summary = compute_pii_summary(contract, preserve=prev)
    return summary


# ── Sidecar store ─────────────────────────────────────────────────────────────

@dataclass
class PiiDecision:
    """One per-column PII decision in the overlay."""
    column:      str
    status:      str                         # "pii" | "not_pii"
    entity_type: Optional[str] = None        # informational, when status="pii"
    payload:     dict = field(default_factory=dict)   # ODCS fragment, status="pii"
    decided_by:  str = ""                     # "session:<sid>" | "share:<token>" | "cli"
    run_id:      str = ""                     # source run, for traceability
    ts:          str = field(default_factory=_utc_now_iso)
    # Engine baseline at decision time (Phase 1b) — durable disagreement signal.
    engine_is_pii: Optional[bool] = None
    engine_entity_type: Optional[str] = None
    engine_confidence: Optional[float] = None
    engine_decision_rule: Optional[str] = None
    # Fingerprint-gated steward metadata (Evidence Review Ledger)
    fingerprint_key: str = ""
    name_normalized: str = ""
    logical_type: str = ""
    physical_type: str = ""
    format_signature: str = ""
    evidence_digest: str = ""
    lifecycle_state: str = "active"  # active | stale | superseded
    decision_version: int = 1
    reason: str = ""
    # Append-only audit trail of prior decision states for this column (most
    # recent first, capped at HISTORY_LIMIT). Never includes nested history.
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PiiDecision":
        conf = d.get("engine_confidence")
        try:
            conf_f = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf_f = None
        eng = d.get("engine_is_pii")
        if eng is not None and not isinstance(eng, bool):
            eng = bool(eng) if str(eng).lower() in {"true", "1", "yes"} else (
                False if str(eng).lower() in {"false", "0", "no"} else None
            )
        return cls(
            column=d["column"],
            status=d.get("status", "not_pii"),
            entity_type=d.get("entity_type"),
            payload=d.get("payload") or {},
            decided_by=d.get("decided_by", ""),
            run_id=d.get("run_id", ""),
            ts=d.get("ts") or _utc_now_iso(),
            engine_is_pii=eng,
            engine_entity_type=d.get("engine_entity_type"),
            engine_confidence=conf_f,
            engine_decision_rule=d.get("engine_decision_rule"),
            fingerprint_key=str(d.get("fingerprint_key") or ""),
            name_normalized=str(d.get("name_normalized") or ""),
            logical_type=str(d.get("logical_type") or ""),
            physical_type=str(d.get("physical_type") or ""),
            format_signature=str(d.get("format_signature") or ""),
            evidence_digest=str(d.get("evidence_digest") or ""),
            lifecycle_state=str(d.get("lifecycle_state") or "active"),
            decision_version=int(d.get("decision_version") or 1),
            reason=str(d.get("reason") or ""),
            history=list(d.get("history") or []),
        )

    @property
    def corrected_engine(self) -> Optional[bool]:
        """True when stored engine baseline disagrees with the human status."""
        if self.engine_is_pii is None:
            return None
        human_pii = self.status == "pii"
        return bool(self.engine_is_pii) != bool(human_pii)


class PiiDecisionStore:
    """Reads/writes the per-table PII decision overlay under ``_meta/``.

    Storage layout (in the contracts bucket):
        _meta/pii_decisions/{db}.{table}.json
    """

    PREFIX = "_meta/pii_decisions"

    def __init__(self, backend: StorageBackend, bucket: str):
        self.backend = backend
        self.bucket = bucket

    def _key(self, table: str) -> str:
        return f"{self.PREFIX}/{table}.json"

    def get(self, table: str) -> dict[str, dict]:
        """Return ``{column: decision_dict}`` for a table (empty if none)."""
        key = self._key(table)
        if not self.backend.exists(self.bucket, key):
            return {}
        raw = self.backend.get_json(self.bucket, key)
        return raw.get("decisions", {}) or {}

    def list(self, table: str) -> list[dict]:
        """Return decisions as a list, most-recent first."""
        items = list(self.get(table).values())
        items.sort(key=lambda d: d.get("ts", ""), reverse=True)
        return items

    #: Max prior-decision snapshots retained per column (oldest dropped first).
    HISTORY_LIMIT = 25

    def set(self, table: str, decision: PiiDecision) -> dict:
        """Upsert one column decision; returns the decision dict.

        When a decision already exists for this column, its prior snapshot
        (minus its own nested history, to keep the record flat) is pushed onto
        the new decision's ``history`` — an append-only audit trail of every
        status/lifecycle/reviewer change for the column.
        """
        if decision.status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        decisions = self.get(table)
        previous = decisions.get(decision.column)
        new_dict = decision.to_dict()
        if previous is not None:
            prior_snapshot = {k: v for k, v in previous.items() if k != "history"}
            history = list(decision.history or previous.get("history") or [])
            history.insert(0, prior_snapshot)
            new_dict["history"] = history[: self.HISTORY_LIMIT]
        decisions[decision.column] = new_dict
        self._save(table, decisions)
        return new_dict

    def history(self, table: str, column: str) -> list[dict]:
        """Full audit trail for one column: current decision first, then priors."""
        decisions = self.get(table)
        current = decisions.get(column)
        if not current:
            return []
        return [
            {k: v for k, v in current.items() if k != "history"},
            *[h for h in (current.get("history") or [])],
        ]

    def remove(self, table: str, column: str) -> bool:
        """Drop a column decision (stop enforcing it). Returns True if removed.

        Note: removing a ``not_pii`` decision does NOT restore the original PII
        metadata — the contract keeps whatever it currently holds. Enforcement
        simply stops on the next upsert.
        """
        decisions = self.get(table)
        if column in decisions:
            del decisions[column]
            self._save(table, decisions)
            return True
        return False

    def clear(self, table: str) -> None:
        key = self._key(table)
        if self.backend.exists(self.bucket, key):
            self.backend.delete(self.bucket, key)

    def _save(self, table: str, decisions: dict) -> None:
        self.backend.put_json(self.bucket, self._key(table),
                              {"table": table, "decisions": decisions})
