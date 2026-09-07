"""Bounded sandbox for approved dynamic tools (open-core T8.3 execution path).

Source is ``ast.parse``-validated then ``run(params, *, table, context)`` is invoked
with a restricted builtin namespace. No imports, no I/O primitives, no reflection.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from redibis.agents.dynamic_tools import DynamicToolManifest

_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "dir", "help", "input", "breakpoint", "memoryview",
    "bytearray", "bytes", "format", "super", "classmethod", "staticmethod", "property",
    "os", "sys", "subprocess", "socket", "pathlib", "importlib", "shutil", "pickle",
    "ctypes", "code", "runpy", "builtins",
})

_FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
)


class SandboxError(ValueError):
    """Dynamic tool failed validation or sandbox execution."""


@dataclass(frozen=True)
class SandboxContext:
    """Read-only view of pipeline context exposed to dynamic tools."""

    table: str
    dry_run: bool
    policy_scope: tuple[str, ...]

    @classmethod
    def from_tool_context(cls, ctx: Any, *, table: str, policy_scope: list[str]) -> "SandboxContext":
        return cls(
            table=table,
            dry_run=bool(getattr(ctx, "dry_run", False)),
            policy_scope=tuple(policy_scope or ()),
        )


def _safe_builtins() -> dict[str, Any]:
    allowed = (
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len",
        "list", "max", "min", "range", "repr", "round", "set", "sorted", "str",
        "sum", "tuple", "zip", "isinstance",
    )
    out: dict[str, Any] = {k: getattr(builtins, k) for k in allowed}
    out["True"] = True
    out["False"] = False
    out["None"] = None
    return out


class _SourceValidator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, _FORBIDDEN_NODES):
            self.errors.append(f"line {getattr(node, 'lineno', '?')}: {type(node).__name__} not allowed")
            return
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name and _is_forbidden(name):
            self.errors.append(f"line {node.lineno}: call to {name!r} not allowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if _is_forbidden(node.id):
            self.errors.append(f"line {node.lineno}: name {node.id!r} not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            self.errors.append(f"line {node.lineno}: attribute {node.attr!r} not allowed")
        if _is_forbidden(node.attr):
            self.errors.append(f"line {node.lineno}: attribute {node.attr!r} not allowed")
        self.generic_visit(node)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_forbidden(name: str) -> bool:
    return name in _FORBIDDEN_NAMES or name.startswith("__")


def validate_dynamic_source(source: str) -> list[str]:
    """Parse-only validation — returns human-readable errors (empty if ok)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    visitor = _SourceValidator()
    visitor.visit(tree)
    return visitor.errors


def _verify_source_hash(path: Path, manifest: DynamicToolManifest) -> None:
    if not manifest.source_sha256:
        return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != manifest.source_sha256:
        raise SandboxError(
            f"source hash mismatch for {manifest.name!r} — re-approve after changes"
        )


def run_dynamic_tool(
    source_path: Path,
    manifest: DynamicToolManifest,
    params: dict[str, Any],
    *,
    table: str,
    context: Any,
) -> dict[str, Any]:
    """Execute an approved dynamic tool in the bounded sandbox."""
    path = Path(source_path)
    if not path.is_file():
        raise SandboxError(f"dynamic tool source missing: {path}")

    source = path.read_text(encoding="utf-8")
    errors = validate_dynamic_source(source)
    if errors:
        raise SandboxError("; ".join(errors))

    _verify_source_hash(path, manifest)

    tree = ast.parse(source, filename=str(path))
    namespace: dict[str, Any] = {"__builtins__": _safe_builtins()}
    exec(compile(tree, str(path), "exec"), namespace)  # noqa: S102 — validated subset only

    run_fn = namespace.get("run")
    if not callable(run_fn):
        raise SandboxError("dynamic tool must define callable run(params, *, table, context)")

    sandbox_ctx = SandboxContext.from_tool_context(
        context,
        table=table,
        policy_scope=manifest.policy_scope,
    )
    try:
        result = run_fn(params or {}, table=table, context=sandbox_ctx)
    except TypeError:
        result = run_fn(params or {})

    if isinstance(result, dict):
        out = dict(result)
    else:
        out = {"result": result}
    out.setdefault("tool", manifest.name)
    out.setdefault("sandbox", True)
    out["source_sha256"] = manifest.source_sha256
    return out
