"""Persistent active pack stack (import / list / remove)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from redibis.config import RedibisConfig
from redibis.pack.errors import PackLoadError, PackValidationError, RdbPackError
from redibis.pack.identity import compute_stack_sha256, compute_stack_uuid  # noqa: F401
from redibis.pack.loader import load_pack
from redibis.pack.resolver import apply_packs
from redibis.pack.stack_models import AppliedPackStack, PackLayerRef

PathLike = Union[str, Path]

STACK_FILENAME = "stack.json"


def default_stack_dir() -> Path:
    env = os.environ.get("REDIBIS_PACK_STACK_DIR")
    if env:
        return Path(env).expanduser()
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "packs"


@dataclass
class PackStackStore:
    """Immutable imported layers under a local directory."""

    root: Path = field(default_factory=default_stack_dir)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()

    @property
    def stack_path(self) -> Path:
        return self.root / STACK_FILENAME

    def _read_raw(self) -> dict[str, Any]:
        if not self.stack_path.is_file():
            return {"layers": []}
        try:
            data = json.loads(self.stack_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PackLoadError(f"invalid pack stack: {exc}") from exc
        if not isinstance(data, dict):
            raise PackLoadError("pack stack must be a mapping")
        layers = data.get("layers") or []
        if not isinstance(layers, list):
            raise PackLoadError("pack stack layers must be a list")
        return {"layers": layers}

    def _write_raw(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.stack_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.stack_path)

    def list_layers(self) -> list[PackLayerRef]:
        out: list[PackLayerRef] = []
        for item in self._read_raw()["layers"]:
            if not isinstance(item, dict):
                continue
            out.append(
                PackLayerRef(
                    id=str(item.get("id") or ""),
                    version=str(item.get("version") or ""),
                    checksum=str(item.get("checksum") or ""),
                    mode=str(item.get("mode") or "overlay"),
                    path=item.get("path"),
                    source=str(item.get("source") or "import"),
                )
            )
        return out

    def stored_pack_path(self, identity: str) -> Path:
        safe = identity.replace("/", "_")
        return self.root / "imported" / f"{safe}.rdbpack"

    def dry_run_import(
        self,
        path: PathLike,
        *,
        mode: Optional[str] = None,
    ) -> dict[str, Any]:
        pack = load_pack(path)
        effective_mode = mode or pack.manifest.mode
        existing = {f"{L.id}@{L.version}": L for L in self.list_layers()}
        identity = pack.manifest.identity
        conflict = identity in existing
        return {
            "ok": True,
            "dry_run": True,
            "identity": identity,
            "pack_sha256": pack.pack_sha256,
            "mode": effective_mode,
            "contents": pack.manifest.contents.model_dump(mode="json"),
            "already_present": conflict,
            "prior_checksum": existing[identity].checksum if conflict else None,
            "checksum_changed": (
                conflict and existing[identity].checksum != pack.pack_sha256
            ),
            "warnings": list(pack.warnings),
            "sections": sorted(
                p for p in pack.files if p not in {"pack.yaml", "CHECKSUMS.json", "README.md"}
            ),
        }

    def import_pack(
        self,
        path: PathLike,
        *,
        mode: Optional[str] = None,
        dry_run: bool = False,
        activate: bool = False,
        policy_store_dir: Optional[PathLike] = None,
        actor: str = "rdbpack-import",
    ) -> dict[str, Any]:
        """Import a pack as a new stack layer.

        Behavior policies become inactive drafts (unless ``activate``).
        Quality rulesets and masking plans are stashed under the stack assets dir.
        """
        from redibis.pack.sections import (
            import_behavior_policies,
            stash_named_documents,
        )

        report = self.dry_run_import(path, mode=mode)
        if dry_run:
            # Preview section actions without writing.
            pack_preview = load_pack(path)
            report["behavior_files"] = sorted(
                r
                for r in pack_preview.files
                if r.startswith("behavior/") and r.endswith((".yaml", ".yml"))
            )
            report["quality_files"] = sorted(
                r
                for r in pack_preview.files
                if r.startswith("quality/rulesets/")
            )
            report["masking_files"] = sorted(
                r
                for r in pack_preview.files
                if r.startswith("masking/plans/")
            )
            report["prompt_files"] = sorted(
                r
                for r in pack_preview.files
                if r.startswith("assets/prompts/") and r.endswith(".md")
            )
            report["activate_requested"] = bool(activate)
            notes = [
                "behavior policies import as inactive drafts "
                "(pass --activate to approve+activate)"
                if not activate
                else (
                    "with --activate, imported behavior policies will be approved "
                    "and activated after draft create"
                )
            ]
            if report["prompt_files"]:
                notes.append(
                    f"{len(report['prompt_files'])} enrichment prompt file(s) "
                    "will replace Settings → Prompts"
                )
            report["activate_note"] = "; ".join(notes)
            return report

        src = Path(path).expanduser()
        pack = load_pack(src)
        effective_mode = mode or pack.manifest.mode
        dest = self.stored_pack_path(pack.manifest.identity)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)

        layers = [L.to_dict() for L in self.list_layers()]
        identity = pack.manifest.identity
        layers = [
            L
            for L in layers
            if L.get("identity") != identity
            and f"{L.get('id')}@{L.get('version')}" != identity
        ]
        entry = PackLayerRef(
            id=pack.manifest.metadata.id,
            version=pack.manifest.metadata.version,
            checksum=pack.pack_sha256,
            mode=effective_mode,
            path=str(dest),
            source="import",
        ).to_dict()
        entry["imported_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        layers.append(entry)
        self._write_raw({"layers": layers})

        stashed = stash_named_documents(pack, stack_root=self.root)
        from redibis.pack.sections import apply_prompt_assets

        prompts = apply_prompt_assets(pack, replace=True)
        behavior = import_behavior_policies(
            pack,
            policy_store_dir=Path(policy_store_dir) if policy_store_dir else None,
            actor=actor,
            activate=bool(activate),
        )

        report["dry_run"] = False
        report["imported"] = True
        report["stored_path"] = str(dest)
        report["stashed"] = stashed
        report["prompts"] = prompts
        report["behavior"] = behavior
        report["activate_requested"] = bool(activate)
        if activate:
            report["activate_note"] = (
                f"activated {len(behavior.get('activated') or [])} behavior policies"
            )
        else:
            report["activate_note"] = (
                f"created {behavior.get('count', 0)} behavior policy draft(s); "
                "not activated"
            )
        if prompts.get("count"):
            report["activate_note"] = (
                (report.get("activate_note") or "")
                + f"; applied {prompts['count']} enrichment prompt file(s) to Settings"
            ).lstrip("; ").strip()
        return report

    def remove(self, identity: str) -> dict[str, Any]:
        from redibis.pack.sections import remove_stashed_assets

        identity = identity.strip()
        raw = self._read_raw()
        layers = list(raw["layers"])
        kept: list[dict[str, Any]] = []
        removed: Optional[dict[str, Any]] = None
        for item in layers:
            if not isinstance(item, dict):
                continue
            item_id = f"{item.get('id')}@{item.get('version')}"
            if item_id == identity or item.get("identity") == identity:
                removed = item
                continue
            kept.append(item)
        if removed is None:
            raise PackValidationError(
                f"pack not in active stack: {identity}",
                errors=[f"pack not in active stack: {identity}"],
            )
        self._write_raw({"layers": kept})
        stored = removed.get("path")
        if stored:
            p = Path(stored)
            if p.is_file():
                p.unlink()
        remove_stashed_assets(self.root, identity)
        return {"ok": True, "removed": identity, "prior": removed}

    def resolve(
        self,
        base: Optional[RedibisConfig] = None,
        *,
        include_builtin_default: bool = True,
    ) -> AppliedPackStack:
        from redibis.pack.sections import load_stashed_named_documents

        cfg = base or RedibisConfig.default()
        paths: list[str] = []
        modes: dict[str, str] = {}
        for layer in self.list_layers():
            if not layer.path:
                continue
            paths.append(layer.path)
            modes[str(Path(layer.path).expanduser())] = layer.mode
        stack = apply_packs(
            cfg,
            paths,
            include_builtin_default=include_builtin_default,
            modes=modes,
        )
        # Merge stashed named docs (replace by name — later layers win).
        for layer in self.list_layers():
            quality, masking = load_stashed_named_documents(self.root, layer.identity)
            stack.quality_rulesets.update(quality)
            stack.masking_plans.update(masking)
        return stack


def diff_pack_against_active(
    path: PathLike,
    *,
    store: Optional[PackStackStore] = None,
    base: Optional[RedibisConfig] = None,
) -> dict[str, Any]:
    """Compare a pack's portable config to the current resolved stack."""
    store = store or PackStackStore()
    pack = load_pack(path)
    active = store.resolve(base or RedibisConfig.default())
    from redibis.pack.config_allowlist import extract_portable_config

    pack_cfg = {}
    if "config/redibis.yaml" in pack.files:
        pack_cfg = pack.yaml("config/redibis.yaml") or {}
    active_cfg = extract_portable_config(active.config)

    def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(node, dict):
            for k, v in node.items():
                key = f"{prefix}.{k}" if prefix else str(k)
                out.update(_flatten(v, key))
        else:
            out[prefix] = node
        return out

    flat_pack = _flatten(pack_cfg)
    flat_active = _flatten(active_cfg)
    changed = sorted(k for k in flat_pack if flat_pack.get(k) != flat_active.get(k))
    only_pack = sorted(set(flat_pack) - set(flat_active))
    return {
        "identity": pack.manifest.identity,
        "pack_sha256": pack.pack_sha256,
        "active_layers": active.layer_audit(),
        "changed_keys": changed,
        "only_in_pack": only_pack,
        "change_count": len(changed),
    }
