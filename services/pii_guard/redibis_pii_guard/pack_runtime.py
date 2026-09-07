"""Pack / RuleSet / policy runtime with mtime-based hot-reload."""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

from redibis.pii.deid.policy import DeidPolicy
from redibis.pii.rules.ruleset import RuleSet, RuleSetCompiler

logger = logging.getLogger("pii_guard.pack")


class PackRuntime:
    """Load RuleSet + de-id policies from an optional mounted ``.rdbpack`` or dir."""

    def __init__(self, pack_path: Optional[str] = None):
        self._pack_path = (pack_path or os.environ.get("REDIBIS_PII_GUARD_PACK") or "").strip()
        self._mtime: float = 0.0
        self._ruleset: RuleSet = RuleSetCompiler.default()
        self._policies: dict[str, DeidPolicy] = {}
        self._stack_audit: list[dict[str, Any]] = []
        self.reload(force=True)

    @property
    def ruleset(self) -> RuleSet:
        self.reload()
        return self._ruleset

    @property
    def policies(self) -> dict[str, DeidPolicy]:
        self.reload()
        return dict(self._policies)

    @property
    def pack_path(self) -> str:
        return self._pack_path

    def get_policy(self, policy_id: str) -> Optional[DeidPolicy]:
        return self.policies.get(policy_id)

    def reload(self, *, force: bool = False) -> bool:
        """Reload when pack mtime changes. Returns True if reloaded."""
        if not self._pack_path:
            if force:
                self._ruleset = RuleSetCompiler.default()
                self._policies = {}
                self._stack_audit = []
            return force

        path = Path(self._pack_path).expanduser()
        if not path.exists():
            logger.warning("PII Guard pack path missing: %s", path)
            return False
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if not force and mtime == self._mtime:
            return False

        try:
            self._load(path)
            self._mtime = mtime
            logger.info(
                "pii_guard_pack_reloaded path=%s ruleset=%s policies=%s",
                path,
                self._ruleset.id,
                len(self._policies),
            )
            return True
        except Exception as exc:
            logger.warning("pii_guard pack reload failed: %s", exc)
            return False

    def _load(self, path: Path) -> None:
        from redibis.config import RedibisConfig
        from redibis.pack import apply_packs, load_pack
        from redibis.pii.deid.pack_store import load_deid_policies

        if path.is_dir():
            # Unpacked pack root (manifest + sections on disk)
            policies = load_deid_policies(path)
            # Prefer compiling via writing a temp pack if .rdbpack sibling missing —
            # for dirs, apply locale YAML if present via LoadedPack-like walk is heavy;
            # use default ruleset + dir policies unless a .rdbpack is the path.
            self._ruleset = RuleSetCompiler.default()
            self._policies = policies
            self._stack_audit = [{"source": "dir", "path": str(path)}]
            return

        pack = load_pack(path)
        stack = apply_packs(
            RedibisConfig.default(),
            [path],
            include_builtin_default=True,
        )
        self._ruleset = RuleSetCompiler.from_stack(stack)
        self._stack_audit = stack.layer_audit()

        # Extract masking/policies from pack zip into a temp dir for pack_store
        policies: dict[str, DeidPolicy] = {}
        with tempfile.TemporaryDirectory(prefix="pii-guard-pack-") as td:
            root = Path(td)
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path, "r") as zf:
                    for name in zf.namelist():
                        if "masking/policies/" in name and name.endswith((".yaml", ".yml")):
                            dest = root / name
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(zf.read(name))
                policies = load_deid_policies(root)
            # Also accept policies already merged onto stack.masking_plans? those are plans.
            # Pack deid policies live under masking/policies/
        # Fallback: empty policies from pack files via load_pack yaml
        if not policies:
            for rel, _ in (pack.files or {}).items():
                if rel.startswith("masking/policies/") and rel.endswith((".yaml", ".yml")):
                    raw = pack.yaml(rel)
                    if isinstance(raw, dict):
                        pol = DeidPolicy.from_dict(raw)
                        policies[pol.id] = pol
        self._policies = policies

    def ruleset_info(self) -> dict[str, Any]:
        rs = self.ruleset
        return {
            "id": rs.id,
            "version": rs.version,
            "default_region": rs.default_region,
            "entities": rs.entity_catalogue(),
            "pack_path": self._pack_path or None,
            "layers": list(self._stack_audit),
        }
