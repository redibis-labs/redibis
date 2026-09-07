"""Artifact residency gate — refuse local / raw_trained corpora & models on egress."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from redibis.pack.errors import PackValidationError

PathLike = Union[str, Path]


class ArtifactResidencyError(PackValidationError):
    """Raised when a local / raw-trained artifact would cross the boundary."""

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message, errors=errors)


def _truthy_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def _scan_mapping(obj: Mapping[str, Any], *, path: str = "") -> list[str]:
    violations: list[str] = []
    residency = obj.get("residency")
    if isinstance(residency, str) and residency.strip().lower() == "local":
        violations.append(f"{path or 'root'}: residency=local")
    if _truthy_flag(obj.get("raw_trained")):
        violations.append(f"{path or 'root'}: raw_trained=true")
    if obj.get("contains_raw_values") is True:
        violations.append(f"{path or 'root'}: contains_raw_values=true")

    for key, val in obj.items():
        child = f"{path}.{key}" if path else str(key)
        if isinstance(val, Mapping):
            violations.extend(_scan_mapping(val, path=child))
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, Mapping):
                    violations.extend(_scan_mapping(item, path=f"{child}[{i}]"))
    return violations


def scan_payload(obj: Any, *, path: str = "") -> list[str]:
    """Return residency violations for an in-memory payload."""
    if isinstance(obj, Mapping):
        return _scan_mapping(obj, path=path)
    if isinstance(obj, list):
        out: list[str] = []
        for i, item in enumerate(obj):
            out.extend(scan_payload(item, path=f"{path}[{i}]" if path else f"[{i}]"))
        return out
    if isinstance(obj, (bytes, bytearray)):
        try:
            text = bytes(obj).decode("utf-8")
        except UnicodeDecodeError:
            return []
        return scan_text(text, path=path or "bytes")
    if isinstance(obj, str):
        return scan_text(obj, path=path or "text")
    return []


def scan_text(text: str, *, path: str = "text") -> list[str]:
    """Scan JSON / JSONL / YAML-ish text for residency markers."""
    violations: list[str] = []
    stripped = (text or "").strip()
    if not stripped:
        return violations

    # JSONL: one object per line
    lines = stripped.splitlines()
    if len(lines) > 1 and lines[0].lstrip().startswith("{"):
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            violations.extend(scan_payload(payload, path=f"{path}:line{i+1}"))
        if violations:
            return violations

    try:
        payload = json.loads(stripped)
        return scan_payload(payload, path=path)
    except json.JSONDecodeError:
        pass

    try:
        import yaml

        payload = yaml.safe_load(stripped)
        if payload is not None:
            return scan_payload(payload, path=path)
    except Exception:
        pass

    # Last-resort substring markers (avoid packing smuggled stamps).
    lowered = stripped.lower()
    if '"residency"' in lowered and "local" in lowered:
        if '"residency": "local"' in lowered or '"residency":"local"' in lowered:
            violations.append(f"{path}: residency=local (text)")
    if '"raw_trained"' in lowered and ("true" in lowered):
        if '"raw_trained": true' in lowered or '"raw_trained":true' in lowered:
            violations.append(f"{path}: raw_trained=true (text)")
    return violations


def scan_path(path: PathLike) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    if p.is_dir():
        out: list[str] = []
        for child in sorted(p.rglob("*")):
            if child.is_file() and child.suffix.lower() in {
                ".json", ".jsonl", ".yaml", ".yml", ".md", ".txt",
            }:
                out.extend(scan_path(child))
        return out
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_text(text, path=str(p))


def assert_artifact_portable(
    obj: Any = None,
    *,
    path: Optional[PathLike] = None,
    label: str = "artifact",
) -> None:
    """Raise ``ArtifactResidencyError`` if local / raw_trained markers are present."""
    violations: list[str] = []
    if path is not None:
        violations.extend(scan_path(path))
    if obj is not None:
        violations.extend(scan_payload(obj, path=label))
    if violations:
        raise ArtifactResidencyError(
            f"{label} refused: local/raw_trained residency cannot leave the boundary",
            errors=violations,
        )


class ArtifactResidencyGate:
    """Shared gate used by pack export/import and any upload/egress attach path."""

    @staticmethod
    def check_payload(obj: Any, *, label: str = "payload") -> None:
        assert_artifact_portable(obj, label=label)

    @staticmethod
    def check_path(path: PathLike, *, label: str = "path") -> None:
        assert_artifact_portable(path=path, label=label)

    @staticmethod
    def check_pack_files(files: Mapping[str, Any], *, label: str = "pack") -> None:
        """Scan every pack member (bytes or structured) before write/import."""
        violations: list[str] = []
        for rel, content in files.items():
            rel_s = str(rel)
            # Skip binary weight blobs — residency stamps live in JSON/YAML manifests.
            if rel_s.lower().endswith((".pt", ".bin", ".onnx", ".safetensors", ".gguf")):
                # Weights without a portable stamp are still blocked if a sibling
                # manifest says raw_trained; we only scan text members here.
                continue
            violations.extend(scan_payload(content, path=f"{label}:{rel_s}"))
        if violations:
            raise ArtifactResidencyError(
                f"{label} refused: local/raw_trained content cannot be packed or uploaded",
                errors=violations,
            )

    @staticmethod
    def check_many(paths: Iterable[PathLike], *, label: str = "artifacts") -> None:
        violations: list[str] = []
        for p in paths:
            violations.extend(scan_path(p))
        if violations:
            raise ArtifactResidencyError(
                f"{label} refused: local/raw_trained residency cannot leave the boundary",
                errors=violations,
            )
