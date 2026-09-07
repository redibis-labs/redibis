"""Pack section handlers — behavior drafts, quality rulesets, masking plans."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from redibis.masking.plan import MaskingPlan
from redibis.pack.errors import PackValidationError
from redibis.pack.models import LoadedPack

# Keys that must never travel in a pack masking template (invariant 11).
_FORBIDDEN_MASKING_FIELDS = frozenset(
    {
        "seed",
        "keys",
        "run_keys",
        "master_key",
        "aes_key",
        "fpe_key",
        "hmac_key",
    }
)


def _behavior_relpaths(pack: LoadedPack) -> list[str]:
    return sorted(
        rel
        for rel in pack.files
        if rel.startswith("behavior/") and rel.endswith((".yaml", ".yml"))
    )


def _quality_relpaths(pack: LoadedPack) -> list[str]:
    return sorted(
        rel
        for rel in pack.files
        if rel.startswith("quality/rulesets/") and rel.endswith((".yaml", ".yml"))
    )


def _masking_relpaths(pack: LoadedPack) -> list[str]:
    return sorted(
        rel
        for rel in pack.files
        if rel.startswith("masking/plans/") and rel.endswith((".yaml", ".yml"))
    )


def validate_masking_plan_document(doc: Any, *, relpath: str) -> dict[str, Any]:
    """Reject secrets loudly; return a sanitized template dict."""
    if not isinstance(doc, dict):
        raise PackValidationError(
            f"{relpath} must be a mapping",
            errors=[f"{relpath} must be a mapping"],
        )
    errors = [
        f"excluded masking field not allowed in pack: {relpath}:{key}"
        for key in doc
        if key in _FORBIDDEN_MASKING_FIELDS and doc.get(key) not in (None, "", {}, [])
    ]
    # Nested secret-ish keys under params are allowed (e.g. mask char); only top-level.
    if errors:
        raise PackValidationError(
            "masking plan contains forbidden secrets",
            errors=errors,
        )
    # Validate shape via MaskingPlan (strips nothing silently beyond defaults).
    plan = MaskingPlan.from_dict(doc)
    sanitized = plan.to_dict()
    sanitized["seed"] = None
    sanitized["run_id"] = None
    sanitized["session_id"] = None
    sanitized["key_refs"] = {}
    sanitized["source"] = sanitized.get("source") or "pack"
    return sanitized


def validate_quality_ruleset_document(doc: Any, *, relpath: str) -> dict[str, Any]:
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise PackValidationError(
            f"{relpath} must be a mapping",
            errors=[f"{relpath} must be a mapping"],
        )
    return copy.deepcopy(doc)


def assets_dir(stack_root: Path, identity: str) -> Path:
    safe = identity.replace("/", "_")
    return stack_root / "assets" / safe


def apply_prompt_assets(
    pack: LoadedPack,
    *,
    replace: bool = True,
) -> dict[str, Any]:
    """Write pack enrichment prompts into the live Settings-backed prompt store."""
    from redibis.enrich.prompt_store import get_enrich_prompt_store

    payload: dict[str, bytes] = {}
    for rel, item in pack.files.items():
        if rel.startswith("assets/prompts/") and rel.endswith(".md"):
            payload[rel] = item.data
    if not payload:
        return {"updated": [], "count": 0}
    return get_enrich_prompt_store().import_pack_files(payload, replace=replace)


def stash_named_documents(
    pack: LoadedPack,
    *,
    stack_root: Path,
) -> dict[str, Any]:
    """Write quality rulesets + masking plans under the pack stack assets dir."""
    root = assets_dir(stack_root, pack.manifest.identity)
    quality_out: dict[str, str] = {}
    masking_out: dict[str, str] = {}

    for rel in _quality_relpaths(pack):
        name = Path(rel).stem
        doc = validate_quality_ruleset_document(pack.yaml(rel), relpath=rel)
        dest = root / "quality" / "rulesets" / f"{name}.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            yaml.safe_dump(doc, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        quality_out[name] = str(dest)

    for rel in _masking_relpaths(pack):
        name = Path(rel).stem
        doc = validate_masking_plan_document(pack.yaml(rel), relpath=rel)
        dest = root / "masking" / "plans" / f"{name}.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            yaml.safe_dump(doc, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        masking_out[name] = str(dest)

    return {
        "quality_rulesets": quality_out,
        "masking_plans": masking_out,
        "assets_root": str(root) if (quality_out or masking_out) else None,
    }


def load_stashed_named_documents(
    stack_root: Path,
    identity: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load stashed quality/masking YAML for one pack identity."""
    root = assets_dir(stack_root, identity)
    quality: dict[str, Any] = {}
    masking: dict[str, Any] = {}
    qdir = root / "quality" / "rulesets"
    if qdir.is_dir():
        for path in sorted(qdir.glob("*.yaml")) + sorted(qdir.glob("*.yml")):
            quality[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mdir = root / "masking" / "plans"
    if mdir.is_dir():
        for path in sorted(mdir.glob("*.yaml")) + sorted(mdir.glob("*.yml")):
            masking[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return quality, masking


def import_behavior_policies(
    pack: LoadedPack,
    *,
    policy_store_dir: Optional[Path] = None,
    actor: str = "rdbpack-import",
    activate: bool = False,
) -> dict[str, Any]:
    """Create behavior policies from pack files as inactive drafts.

    When ``activate`` is True, approve + activate each imported version
    (simulation gate disabled for pack-driven activation).
    """
    from redibis.services.behavior_service import (
        BehaviorPolicyService,
        BehaviorServiceError,
    )

    rels = _behavior_relpaths(pack)
    if not rels:
        return {
            "drafts": [],
            "activated": [],
            "errors": [],
            "count": 0,
        }

    kwargs: dict[str, Any] = {
        "require_approval_for_activation": False,
        "require_simulation_for_activation": False,
    }
    if policy_store_dir is not None:
        kwargs["base_dir"] = policy_store_dir
    svc = BehaviorPolicyService(**kwargs)

    drafts: list[dict[str, Any]] = []
    activated: list[dict[str, Any]] = []
    errors: list[str] = []

    for rel in rels:
        raw = pack.yaml(rel)
        if not isinstance(raw, Mapping):
            errors.append(f"{rel}: behavior document must be a mapping")
            continue
        doc = dict(raw)
        try:
            created = svc.create(
                doc,
                actor=actor,
                note=f"imported from pack {pack.manifest.identity} ({rel})",
            )
        except BehaviorServiceError as exc:
            # Immutable (id, version) already present — treat as idempotent draft.
            msg = str(exc)
            if "already exists" in msg.lower() or "immutable" in msg.lower():
                mid = (doc.get("metadata") or {}).get("id")
                ver = (doc.get("metadata") or {}).get("version")
                drafts.append(
                    {
                        "id": mid,
                        "version": ver,
                        "status": "draft",
                        "relpath": rel,
                        "idempotent": True,
                    }
                )
                if activate and mid and ver:
                    try:
                        act = svc.activate(
                            str(mid),
                            str(ver),
                            actor=actor,
                            role="pack-import",
                            note=f"activate from pack {pack.manifest.identity}",
                            skip_approval_check=True,
                        )
                        activated.append(act)
                    except BehaviorServiceError as act_exc:
                        errors.append(f"{rel}: activate failed: {act_exc}")
                continue
            errors.append(f"{rel}: {exc}")
            continue

        created["relpath"] = rel
        drafts.append(created)
        if activate:
            try:
                # Ensure approved flag for audit trail even when gate is off.
                try:
                    svc.approve(
                        created["id"],
                        created["version"],
                        actor=actor,
                        role="pack-import",
                        note="auto-approve for pack --activate",
                    )
                except BehaviorServiceError:
                    pass
                act = svc.activate(
                    created["id"],
                    created["version"],
                    actor=actor,
                    role="pack-import",
                    note=f"activate from pack {pack.manifest.identity}",
                    skip_approval_check=True,
                )
                activated.append(act)
            except BehaviorServiceError as exc:
                errors.append(f"{rel}: activate failed: {exc}")

    if errors and not drafts:
        raise PackValidationError(
            "behavior policy import failed",
            errors=errors,
        )
    return {
        "drafts": drafts,
        "activated": activated,
        "errors": errors,
        "count": len(drafts),
        "activate_requested": activate,
    }


def remove_stashed_assets(stack_root: Path, identity: str) -> None:
    root = assets_dir(stack_root, identity)
    if root.is_dir():
        import shutil

        shutil.rmtree(root, ignore_errors=True)
