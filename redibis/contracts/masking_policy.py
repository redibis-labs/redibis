"""
redibis.contracts.masking_policy
================================
Builds the per-column ``maskingPolicy`` block, including role-based overrides.

The contract declares a *default* treatment per column plus, for each known
role, what that role sees. Roles + their behaviour are data-driven from a JSON
config so they can be retuned without code changes.

Config resolution (first match wins), mirroring enrich/providers.py:
    1. an explicit dict / path passed in (settings override "on the fly")
    2. REDIBIS_MASKING_ROLES env var → path to a JSON file
    3. ./masking_roles.json in the current working directory
    4. the packaged masking_roles_default.json

Config schema::

    {
      "roles": {
        "admin":        {"pii_personal": "none",    "pii_sensitive": "none"},
        "data_science": {"pii_personal": "default", "pii_sensitive": "hash"}
      }
    }

Per-role value per classification:
    "none"     → reveal the raw value (full access)
    "default"  → apply the column's suggested default strategy
    <strategy> → a concrete strategy (mask | hash | fpe | fake | redact | encrypt)

Only declarative policy is emitted — strategy names + role behaviour. NEVER key
material (architecture invariant #11).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

from redibis.models import NON_PII_ENTITIES, canonical_entity
from redibis.pii.sensitivity import classify_sensitivity

_DEFAULT_FILE = Path(__file__).with_name("masking_roles_default.json")
_ENV_VAR = "REDIBIS_MASKING_ROLES"
_CWD_FILE = "masking_roles.json"

#: classification used when a column's own classification has no entry in config
_FALLBACK_CLASS = "pii_personal"

_MASKING_DEFAULTS: dict[str, str] = {
    "EG_NATIONAL_ID": "fpe", "NATIONAL_ID": "fpe", "SSN": "fpe",
    "PASSPORT": "fpe", "EG_TAX_ID": "fpe",
    "CREDIT_CARD": "fpe", "IBAN_CODE": "fpe", "BANK_ACCOUNT": "fpe",
    "EMAIL": "fake", "EMAIL_ADDRESS": "fake",
    "PHONE": "fake", "PHONE_NUMBER": "fake", "MSISDN": "fake",
    "PERSON": "fake", "NAME": "fake", "PER": "fake",
    "ADDRESS": "fake", "LOCATION": "fake", "GPE": "fake", "LOC": "fake",
    "DATE": "fake", "DATE_TIME": "fake", "DOB": "fake",
    # NOTE: COMPANY/ORG/ORGANIZATION are intentionally absent -- an
    # organization name classifies "internal" (non-PII, see
    # ``redibis.models.NON_PII_ENTITIES``) and must stay passthrough below,
    # never masked as if it were personal data.
}

_REVERSIBLE_STRATEGIES: frozenset[str] = frozenset({"fpe", "encrypt"})


def suggest_masking_default(
    entity_type: Optional[str],
    *,
    detected: bool = True,
    classification: Optional[str] = None,
) -> dict:
    """Recommend a declarative masking treatment for a detected entity.

    Tier-first (mirrors ``masking.plan.suggest_rule``):
      security_sensitive       → redact
      pii_indirect             → fpe (preserve joins)
      pii_sensitive/personal   → entity-specific defaults
      internal, known entity   → passthrough (non-PII, e.g. ORGANIZATION —
                                  nothing to mask even when "detected")
      internal, unknown entity → hash (legacy safety for untyped/unregistered
                                  detections; ``classify_sensitivity`` fails
                                  safe to "internal" for these too, but they
                                  carry no registered non-PII entity type to
                                  vouch for them)
    """
    if not detected:
        return {"default": "passthrough", "reversible": False}

    cls = classification or classify_sensitivity(entity_type)
    et_key = (entity_type or "").strip().upper()
    mapped = _MASKING_DEFAULTS.get(et_key)

    if cls == "security_sensitive":
        return {"default": "redact", "reversible": False}
    if cls == "pii_indirect":
        return {"default": "fpe", "reversible": True}

    if cls in ("pii_sensitive", "pii_personal"):
        strategy = mapped or "hash"
        return {"default": strategy, "reversible": strategy in _REVERSIBLE_STRATEGIES}

    if cls == "internal" and canonical_entity(entity_type) in NON_PII_ENTITIES:
        return {"default": "passthrough", "reversible": False}

    # Untyped / unregistered entity still flagged detected — pseudonymize safely.
    return {"default": "hash", "reversible": False}


def _read_roles(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    roles = data.get("roles", {}) if isinstance(data, dict) else {}
    return {k: v for k, v in roles.items()
            if isinstance(v, dict) and not k.startswith("_")}


def _config_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.getenv(_ENV_VAR)
    if env:
        return Path(env)
    cwd = Path.cwd() / _CWD_FILE
    if cwd.exists():
        return cwd
    return _DEFAULT_FILE


def resolve_role_config(source: Union[str, Path, dict, None] = None) -> dict:
    """
    Resolve the ``{role: {classification: behaviour}}`` mapping.

    ``source`` may be a dict (used directly — the settings override), a path
    string, or None (env var → CWD file → packaged default). The packaged
    defaults are always the base layer; a user file/dict overrides by role name.
    """
    base = _read_roles(_DEFAULT_FILE)
    if isinstance(source, dict):
        roles = source.get("roles", source)
        base.update({k: v for k, v in roles.items() if isinstance(v, dict)})
        return base
    path = _config_path(str(source) if source is not None else None)
    if path != _DEFAULT_FILE and path.exists():
        base.update(_read_roles(path))
    return base


def build_masking_policy(
    entity_type: Optional[str],
    *,
    detected: bool,
    classification: Optional[str] = None,
    role_config: Optional[dict] = None,
) -> Optional[dict]:
    """
    Build a column ``maskingPolicy`` block, or None for non-PII columns.

    Returns::

        {
          "default": "fpe",
          "reversible": True,
          "roles": {"admin": "none", "data_science": "hash"}
        }
    """
    if not detected:
        return None

    cls = classification or classify_sensitivity(entity_type)
    base = suggest_masking_default(entity_type, detected=True, classification=cls)
    roles_cfg = role_config if role_config is not None else resolve_role_config()

    roles: dict[str, str] = {}
    for role, by_class in roles_cfg.items():
        behaviour = by_class.get(cls, by_class.get(_FALLBACK_CLASS))
        if behaviour is None:
            continue
        roles[role] = base["default"] if behaviour == "default" else behaviour

    policy = {"default": base["default"], "reversible": base["reversible"]}
    if roles:
        policy["roles"] = roles
    return policy
