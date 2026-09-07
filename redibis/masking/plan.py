"""
redibis.masking.plan — the MaskingPlan object model + PII-driven auto-suggest.

A ``MaskingPlan`` is a saved, reusable artifact: the per-column rules + locale
travel across tables/sessions, while ``run_id`` / ``seed`` / ``key_refs`` are
minted fresh on each apply (see engine.py). The plan is auto-suggested from a
PII scan and freely overridable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


STRATEGIES = ("passthrough", "redact", "mask", "hash", "encrypt", "fpe", "fake")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ColumnMaskRule:
    column: str
    include_in_scan: bool = True          # the active-column-list toggle
    strategy: str = "passthrough"
    detected_entity: Optional[str] = None  # carried from the PII scan
    deterministic: bool = True             # same input → same output (joins survive)
    params: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ColumnMaskRule":
        params = dict(d.get("params") or {})
        if "locale" in params:
            from redibis.masking import transforms as T
            params["locale"] = T.canonicalize_faker_locale(params.get("locale"))
        return cls(
            column=d["column"],
            include_in_scan=bool(d.get("include_in_scan", True)),
            strategy=d.get("strategy", "passthrough"),
            detected_entity=d.get("detected_entity"),
            deterministic=bool(d.get("deterministic", True)),
            params=params,
            note=d.get("note", ""),
        )


@dataclass
class MaskingPlan:
    schema_table: str
    plan_id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:12]}")
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    seed: Optional[str] = None
    key_refs: dict = field(default_factory=dict)   # name → metadata (never key material)
    default_locale: str = "default"                 # default(auto) | ar | en(legacy)
    require_authenticated_crypto: bool = True       # fail closed without ff3 / AES-GCM
    columns: list[ColumnMaskRule] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    source: str = "manual"                          # auto-from-pii | manual | saved:<name>
    name: Optional[str] = None                      # set when saved as a reusable config

    # ── Access ──────────────────────────────────────────────────────────────

    def rule_for(self, column: str) -> Optional[ColumnMaskRule]:
        return next((c for c in self.columns if c.column == column), None)

    def name_column(self) -> Optional[str]:
        """The first column faked as a name — used to derive email local-parts."""
        return next((c.column for c in self.columns
                     if c.strategy == "fake" and c.params.get("kind") == "name"), None)

    def scan_columns(self) -> list[str]:
        return [c.column for c in self.columns if c.include_in_scan]

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        d["columns"] = [c.to_dict() for c in self.columns]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MaskingPlan":
        from redibis.masking import transforms as T
        return cls(
            schema_table=d.get("schema_table", ""),
            plan_id=d.get("plan_id") or f"plan_{uuid.uuid4().hex[:12]}",
            session_id=d.get("session_id"),
            run_id=d.get("run_id"),
            seed=d.get("seed"),
            key_refs=dict(d.get("key_refs") or {}),
            default_locale=T.canonicalize_faker_locale(d.get("default_locale")),
            require_authenticated_crypto=bool(d.get("require_authenticated_crypto", True)),
            columns=[ColumnMaskRule.from_dict(c) for c in (d.get("columns") or [])],
            created_at=d.get("created_at", _utc_now_iso()),
            updated_at=d.get("updated_at", _utc_now_iso()),
            source=d.get("source", "manual"),
            name=d.get("name"),
        )

    def to_yaml(self) -> str:
        import yaml
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> "MaskingPlan":
        import yaml
        return cls.from_dict(yaml.safe_load(text) or {})


# ─────────────────────────────────────────────────────────────────────────────
# Auto-suggestion from PII detection
# ─────────────────────────────────────────────────────────────────────────────

def _norm_entity(entity: Optional[str]) -> str:
    return (entity or "").strip().upper()


def _default_fpe_mode() -> str:
    from redibis.masking import transforms as T
    return T.default_fpe_mode()


def adapt_plan_runtime(plan: MaskingPlan) -> bool:
    """Downgrade FPE rules to keystream when ff3 is not installed. Returns True if changed."""
    from redibis.masking import transforms as T
    if T._HAS_FF3:
        return False
    changed = False
    for rule in plan.columns:
        if rule.strategy != "fpe":
            continue
        p = dict(rule.params or {})
        if p.get("mode", "ff3") in (None, "ff3"):
            p["mode"] = "keystream"
            rule.params = p
            changed = True
    return changed


def sync_plan_from_pii(plan: MaskingPlan, columns: list[str],
                      detections: Optional[list[dict]] = None,
                      *, default_locale: Optional[str] = None) -> bool:
    """Align saved rules with the current dataframe + PII detections. Returns True if changed."""
    locale = default_locale or plan.default_locale or "default"
    by_col: dict[str, dict] = {}
    for d in detections or []:
        col = d.get("column")
        if col:
            by_col[col] = d
    existing = {r.column: r for r in plan.columns}
    changed = False
    new_rules: list[ColumnMaskRule] = []
    for col in columns:
        d = by_col.get(col)
        detected = bool((d or {}).get("detected"))
        entity = (d or {}).get("entity_type")
        if col in existing:
            rule = existing[col]
            if detected and entity:
                suggested = suggest_rule(col, entity, detected=True, default_locale=locale)
                if rule.strategy == "passthrough" and suggested.strategy != "passthrough":
                    rule.strategy = suggested.strategy
                    rule.params = dict(suggested.params)
                    rule.detected_entity = suggested.detected_entity
                    changed = True
                elif not rule.detected_entity and suggested.detected_entity:
                    rule.detected_entity = suggested.detected_entity
                    changed = True
            new_rules.append(rule)
        else:
            new_rules.append(
                suggest_rule(col, entity, detected=detected, default_locale=locale))
            changed = True
    if len(new_rules) != len(plan.columns):
        changed = True
    plan.columns = new_rules
    return changed


def suggest_rule(column: str, entity: Optional[str], *, detected: bool,
                 default_locale: str = "default") -> ColumnMaskRule:
    """Map a detected entity to a sensible masking rule (overridable)."""
    from redibis.masking import transforms as T
    from redibis.pii.sensitivity import classify_sensitivity
    
    e = _norm_entity(entity)
    rule = ColumnMaskRule(column=column, detected_entity=entity or None,
                          deterministic=True)
    locale = T.canonicalize_faker_locale(default_locale)
    if not detected or not e:
        rule.strategy = "passthrough"
        return rule

    cls = classify_sensitivity(entity)
    if cls == "pii_sensitive":
        if e in ("NATIONAL_ID", "SSN", "ID", "ID_NUMBER", "PASSPORT", "CREDIT_CARD", "IBAN", "IBAN_CODE", "BANK_ACCOUNT", "CARD", "EG_NATIONAL_ID", "EG_TAX_ID"):
            rule.strategy = "fpe"
            rule.params = {"mode": _default_fpe_mode(), "alphabet": "digits", "key_ref": "k1"}
        else:
            rule.strategy = "hash"
            rule.params = {"algo": "sha256", "hmac_key_ref": "k1", "truncate": 12}
    elif cls == "pii_personal":
        if e in ("EMAIL", "EMAIL_ADDRESS"):
            rule.strategy = "fake"
            rule.params = {"kind": "email", "preserve": {"domain": True}}
        elif e in ("PHONE", "PHONE_NUMBER"):
            rule.strategy = "fake"
            rule.params = {"kind": "phone", "preserve": {"format": True, "country_code": True}}
        elif e in ("PERSON", "NAME", "PER"):
            rule.strategy = "fake"
            rule.params = {"kind": "name", "locale": locale, "preserve": {"gender": True}}
        elif e in ("ADDRESS", "LOCATION", "GPE", "LOC", "SOCIAL_PROFILE_URL"):
            rule.strategy = "fake"
            rule.params = {"kind": "address", "locale": locale}
        elif e in ("DATE", "DATE_TIME", "DOB", "BIRTHDATE"):
            rule.strategy = "fake"
            rule.params = {"kind": "date", "jitter_days": 30}
        elif e in ("COMPANY", "ORG", "ORGANIZATION"):
            rule.strategy = "fake"
            rule.params = {"kind": "company", "locale": locale}
        else:
            rule.strategy = "hash"
            rule.params = {"algo": "sha256", "hmac_key_ref": "k1", "truncate": 12}
    elif cls == "pii_indirect":
        rule.strategy = "fpe"
        rule.deterministic = True
        rule.params = {"mode": _default_fpe_mode(), "alphabet": "digits", "key_ref": "k1"}
    elif cls == "security_sensitive":
        rule.strategy = "redact"
        rule.params = {}
    else:
        rule.strategy = "passthrough"

    _emit_mask_decision(column, rule, detected=detected, entity=entity)
    return rule


def _emit_mask_decision(
    column: str,
    rule: ColumnMaskRule,
    *,
    detected: bool,
    entity: Optional[str],
) -> None:
    from redibis.obs import DecisionRecord, decision

    decision(
        DecisionRecord(
            stage="masking",
            fn="masking.plan.suggest_rule",
            table="",
            column=column,
            verdict=rule.strategy,
            confidence=1.0 if detected else 0.0,
            rule=f"entity={entity or 'none'} → {rule.strategy}",
            inputs={
                "strategy": rule.strategy,
                "entity_type": entity or "",
                "engine": "suggest_rule",
            },
        )
    )


def auto_suggest_plan(schema_table: str, columns: list[str],
                      detections: Optional[list[dict]] = None,
                      *, default_locale: str = "default",
                      session_id: Optional[str] = None) -> MaskingPlan:
    """Build a full plan: one rule per column, pre-filled from PII detections."""
    from redibis.masking import transforms as T
    locale = T.canonicalize_faker_locale(default_locale)
    by_col = {}
    for d in (detections or []):
        col = d.get("column")
        if col is not None:
            by_col[col] = d
    rules = []
    for col in columns:
        d = by_col.get(col)
        rule = suggest_rule(col, (d or {}).get("entity_type"),
                            detected=bool((d or {}).get("detected")),
                            default_locale=locale)
        rules.append(rule)
    return MaskingPlan(
        schema_table=schema_table, session_id=session_id,
        default_locale=locale, columns=rules,
        source="auto-from-pii" if detections else "manual",
    )
