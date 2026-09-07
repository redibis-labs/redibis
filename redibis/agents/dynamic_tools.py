"""Dynamic tool registry — approved sandboxed tools (Phase 8 T8.3)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from redibis.agents.node_registry import NodeSpec, register_node


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DynamicToolManifest:
    """MCP-style manifest for a human-approved dynamic tool."""

    name: str
    version: str = "1.0.0"
    description: str = ""
    policy_scope: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    source_sha256: str = ""
    approved: bool = False
    approved_by: str = ""
    approved_at: str = ""
    documentation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "policy_scope": list(self.policy_scope),
            "input_schema": dict(self.input_schema),
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "documentation": dict(self.documentation),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DynamicToolManifest":
        return cls(
            name=str(data.get("name") or ""),
            version=str(data.get("version") or "1.0.0"),
            description=str(data.get("description") or ""),
            policy_scope=list(data.get("policy_scope") or []),
            input_schema=dict(data.get("input_schema") or {}),
            source_path=str(data.get("source_path") or ""),
            source_sha256=str(data.get("source_sha256") or ""),
            approved=bool(data.get("approved")),
            approved_by=str(data.get("approved_by") or ""),
            approved_at=str(data.get("approved_at") or ""),
            documentation=dict(data.get("documentation") or {}),
        )

    def node_kind(self) -> str:
        return f"dynamic:{self.name}"


class DynamicToolRegistry:
    """File-backed registry — only approved tools bind to the agent palette."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _tool_dir(self, name: str) -> Path:
        safe = name.replace("/", "_").replace(":", "_")
        return self.root / safe

    def _manifest_path(self, name: str) -> Path:
        return self._tool_dir(name) / "manifest.json"

    @staticmethod
    def hash_source(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def register(
        self,
        manifest: DynamicToolManifest,
        *,
        source_file: Optional[Path] = None,
        approved_by: str = "",
        force_approve: bool = False,
    ) -> DynamicToolManifest:
        """
        Stage a dynamic tool. Execution binding requires ``approved_by`` or ``force_approve``.

        Never auto-executes generated code — human approval is mandatory.
        """
        if not manifest.name:
            raise ValueError("manifest.name is required")

        tool_dir = self._tool_dir(manifest.name)
        tool_dir.mkdir(parents=True, exist_ok=True)

        if source_file is not None:
            src = Path(source_file)
            if not src.is_file():
                raise FileNotFoundError(src)
            manifest.source_path = str(src)
            manifest.source_sha256 = self.hash_source(src)
            dest = tool_dir / src.name
            if src.resolve() != dest.resolve():
                dest.write_bytes(src.read_bytes())

        if approved_by or force_approve:
            manifest.approved = True
            manifest.approved_by = approved_by or "system"
            manifest.approved_at = _utc_iso()
        else:
            manifest.approved = False

        manifest.documentation = {
            "kind": manifest.node_kind(),
            "label": manifest.name,
            "description": manifest.description,
            "policy_scope": manifest.policy_scope,
            "markdown": (
                f"## Dynamic tool: {manifest.name}\n\n"
                f"{manifest.description}\n\n"
                f"**Policy scope:** {', '.join(manifest.policy_scope) or 'none'}\n"
            ),
        }
        self._manifest_path(manifest.name).write_text(
            json.dumps(manifest.to_dict(), indent=2),
            encoding="utf-8",
        )
        if manifest.approved:
            self._bind_to_palette(manifest)
        return manifest

    def _bind_to_palette(self, manifest: DynamicToolManifest) -> None:
        kind = manifest.node_kind()
        register_node(NodeSpec(
            kind=kind,
            label=manifest.name,
            tool=f"dynamic:{manifest.name}",
            description=manifest.description or f"Dynamic tool {manifest.name}",
            category="dynamic",
        ))

    def list_tools(self, *, approved_only: bool = False) -> list[DynamicToolManifest]:
        tools: list[DynamicToolManifest] = []
        if not self.root.is_dir():
            return tools
        for child in sorted(self.root.iterdir()):
            path = child / "manifest.json"
            if not path.is_file():
                continue
            try:
                manifest = DynamicToolManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, OSError):
                continue
            if approved_only and not manifest.approved:
                continue
            tools.append(manifest)
        return tools

    def get(self, name: str) -> DynamicToolManifest:
        path = self._manifest_path(name)
        if not path.is_file():
            raise KeyError(f"dynamic tool not found: {name!r}")
        return DynamicToolManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def bind_approved_runners(self, *, sandbox_enabled: bool = False) -> int:
        """Register runners for approved tools (sandbox or register-only stub)."""
        from redibis.agents.plugins import register_runner

        count = 0
        for manifest in self.list_tools(approved_only=True):
            kind = manifest.node_kind()

            if sandbox_enabled:
                def _make_sandbox_runner(m: DynamicToolManifest):
                    def _run(params, *, table, context):
                        from redibis.agents.dynamic_sandbox import SandboxError, run_dynamic_tool

                        src_name = Path(m.source_path).name if m.source_path else ""
                        if not src_name:
                            return {
                                "skipped": True,
                                "reason": f"dynamic tool {m.name!r} has no source file",
                            }
                        source_path = self._tool_dir(m.name) / src_name
                        try:
                            return run_dynamic_tool(
                                source_path,
                                m,
                                params or {},
                                table=table,
                                context=context,
                            )
                        except SandboxError as exc:
                            return {"failed": True, "error": str(exc), "tool": m.name}

                    return _run

                register_runner(kind, _make_sandbox_runner(manifest))
            else:
                def _make_stub_runner(m: DynamicToolManifest):
                    def _run(params, *, table, context):
                        return {
                            "skipped": True,
                            "reason": (
                                f"dynamic tool {m.name!r} is registered — "
                                "enable agents.dynamic_sandbox_enabled to execute"
                            ),
                            "policy_scope": m.policy_scope,
                            "source_sha256": m.source_sha256,
                            "params": params,
                        }
                    return _run

                register_runner(kind, _make_stub_runner(manifest))

            self._bind_to_palette(manifest)
            count += 1
        return count
