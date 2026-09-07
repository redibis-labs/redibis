"""Load / save DeidPolicy documents under pack ``masking/policies/``.

Column masking plans stay at ``masking/plans/``. Text de-id policies use a
sibling folder so the two document types never collide.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from redibis.pii.deid.policy import DEID_API_VERSION, DeidPolicy

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def policies_dir(root: Path) -> Path:
    return Path(root) / "masking" / "policies"


def validate_deid_policy_document(doc: Any, *, relpath: str = "") -> dict[str, Any]:
    """Validate + sanitize a DeidPolicy mapping (no secrets)."""
    if not isinstance(doc, dict):
        raise ValueError(f"{relpath or 'document'} must be a mapping")
    for forbidden in ("seed", "keys", "run_keys", "master_key", "master_key_hex", "aes_key", "fpe_key"):
        if doc.get(forbidden) not in (None, "", {}, []):
            raise ValueError(f"forbidden secret field in deid policy: {forbidden}")
    policy = DeidPolicy.from_dict(doc)
    out = policy.to_dict()
    out["apiVersion"] = out.get("apiVersion") or DEID_API_VERSION
    return out


def load_deid_policies(root: Path) -> dict[str, DeidPolicy]:
    """Load all ``masking/policies/*.yaml`` under ``root``."""
    d = policies_dir(root)
    out: dict[str, DeidPolicy] = {}
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            sanitized = validate_deid_policy_document(raw, relpath=str(path))
            pol = DeidPolicy.from_dict(sanitized)
            out[pol.id] = pol
        except Exception:
            continue
    return out


def save_deid_policy(root: Path, policy: DeidPolicy) -> Path:
    """Write a policy YAML under ``masking/policies/{id}.yaml``. Returns path."""
    if not _SAFE_ID.match(policy.id):
        raise ValueError(f"invalid policy id: {policy.id!r}")
    d = policies_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{policy.id}.yaml"
    sanitized = validate_deid_policy_document(policy.to_dict(), relpath=str(path))
    # Strip checksum noise on write unless set
    path.write_text(
        yaml.safe_dump(sanitized, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def default_deid_policies_root() -> Path:
    """Prefer pack stack assets, else local configs/packs/deid."""
    import os

    env = os.environ.get("REDIBIS_DEID_POLICIES_DIR")
    if env:
        return Path(env).expanduser()
    try:
        from redibis.pack.stack import PackStackStore, default_stack_dir

        root = default_stack_dir()
        # Prefer shared assets root for the whole stack
        shared = root / "assets" / "_shared"
        return shared
    except Exception:
        return Path(os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")).expanduser() / "packs" / "deid"
