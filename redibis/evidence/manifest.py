"""Pure builders for ``evidence_manifest.json``."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Optional

from redibis.evidence.models import (
    ArtifactRef,
    CoverageEntry,
    CoverageStatus,
    EvidenceManifest,
    SCHEMA_VERSION,
)


_PHASES = ("profile", "quality", "pii", "llm", "agentic")


def coverage_entry(
    status: str,
    *,
    reason: str = "",
    artifact: str = "",
    error: str = "",
) -> dict[str, Any]:
    return CoverageEntry(
        status=status, reason=reason, artifact=artifact, error=error,
    ).to_dict()


def logical_artifact_name(run_dir: Path | str | None, path: str) -> str:
    """Relative name inside the run folder; never a host absolute path."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    if raw.startswith("_meta/") or raw.startswith("s3://"):
        return raw
    p = Path(raw)
    if run_dir:
        try:
            return p.resolve().relative_to(Path(run_dir).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return p.name or raw.replace("\\", "/").rsplit("/", 1)[-1]


def file_integrity(path: Path) -> tuple[str, int]:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return "", 0
    return hashlib.sha256(data).hexdigest(), len(data)


def artifact_ref(
    *,
    name: str,
    path: str = "",
    kind: str = "",
    sensitivity: str = "shareable",
    local_path: Optional[Path] = None,
    storage_key: str = "",
) -> dict[str, Any]:
    sha, size = ("", 0)
    local = False
    if local_path is not None and Path(local_path).is_file():
        sha, size = file_integrity(Path(local_path))
        local = True
    ref = ArtifactRef(
        name=name,
        path=path or name,
        kind=kind,
        sensitivity=sensitivity,
        sha256=sha,
        size=size,
        local=local,
        storage=bool(storage_key),
        storage_key=storage_key,
    )
    return ref.to_dict()


def infer_coverage(
    *,
    artifacts: dict[str, Any],
    errors: Iterable[dict[str, Any]] = (),
    warnings: Iterable[dict[str, Any]] = (),
    run_profile: bool = False,
    run_quality: bool = False,
    run_pii: bool = False,
    llm_calls: Iterable[dict[str, Any]] = (),
    agentic: bool = False,
    run_status: str = "",
    empty: bool = False,
) -> dict[str, Any]:
    err_by_phase = {}
    for item in list(errors) + list(warnings):
        phase = str(item.get("phase") or "")
        if phase:
            err_by_phase[phase] = str(item.get("error") or item.get("message") or "error")
    status = (run_status or "").strip().lower()

    def _artifact_present(artifact_key: str) -> str:
        value = artifacts.get(artifact_key) or ""
        if isinstance(value, dict):
            return str(value.get("path") or value.get("name") or "")
        return str(value)

    def _outcome_reason(phase: str, path: str) -> str:
        if status == "cancelled":
            return "scan cancelled"
        if status == "failed" and not path:
            return "scan failed"
        if empty:
            return "empty table"
        return ""

    def _phase(enabled: bool, artifact_key: str, phase: str) -> dict[str, Any]:
        if phase in err_by_phase:
            return coverage_entry(
                CoverageStatus.ERROR.value,
                reason="artifact write failed",
                error=err_by_phase[phase],
            )
        if not enabled:
            return coverage_entry(CoverageStatus.SKIPPED.value, reason=f"{phase} not configured")
        path = _artifact_present(artifact_key)
        extra = _outcome_reason(phase, path)
        if status == "cancelled" and not path:
            return coverage_entry(CoverageStatus.SKIPPED.value, reason="scan cancelled")
        if status == "failed" and not path:
            return coverage_entry(
                CoverageStatus.ERROR.value,
                reason="scan failed",
                error=err_by_phase.get(phase, extra),
            )
        if path:
            return coverage_entry(
                CoverageStatus.EVALUATED.value,
                artifact=str(path),
                reason=extra,
            )
        return coverage_entry(
            CoverageStatus.ERROR.value,
            reason=f"{phase} ran but no evidence artifact was written",
        )

    llm_list = list(llm_calls)
    if llm_list:
        llm = coverage_entry(
            CoverageStatus.EVALUATED.value,
            artifact="llm_calls/",
            reason=f"{len(llm_list)} call(s)",
        )
    else:
        llm = coverage_entry(CoverageStatus.NOT_RUN.value, reason="no guarded model calls")
    if "llm" in err_by_phase:
        llm = coverage_entry(
            CoverageStatus.ERROR.value,
            reason="llm evidence write warning",
            error=err_by_phase["llm"],
            artifact="llm_calls/" if llm_list else "",
        )

    return {
        "profile": _phase(run_profile, "evidence_bundle", "profile") if run_profile else (
            coverage_entry(CoverageStatus.SKIPPED.value, reason="run_profile=false")
        ),
        "quality": _phase(run_quality, "quality_contract", "quality") if run_quality else (
            coverage_entry(CoverageStatus.SKIPPED.value, reason="run_quality=false")
        ),
        "pii": _phase(run_pii, "pii_detections", "pii") if run_pii else (
            coverage_entry(CoverageStatus.SKIPPED.value, reason="run_pii=false")
        ),
        "llm": llm,
        "agentic": coverage_entry(
            CoverageStatus.EVALUATED.value if agentic else CoverageStatus.NOT_RUN.value,
            reason="" if agentic else "no agent run",
            artifact="run_trace.json" if agentic else "",
        ),
    }


def build_manifest(
    *,
    table: str,
    run_id: str,
    artifacts: dict[str, Any],
    coverage: Optional[dict[str, Any]] = None,
    engines: Optional[list[dict[str, Any]]] = None,
    llm_calls: Optional[list[dict[str, Any]]] = None,
    result_variants: Optional[list[dict[str, Any]]] = None,
    errors: Optional[list[dict[str, Any]]] = None,
    warnings: Optional[list[dict[str, Any]]] = None,
    config_hash: str = "",
    config_artifact: str = "",
    sensitivity: Optional[dict[str, Any]] = None,
    source_files: Optional[list[str]] = None,
    run_profile: bool = False,
    run_quality: bool = False,
    run_pii: bool = False,
    agentic: bool = False,
    run_status: str = "",
    session_id: str = "",
    empty: bool = False,
) -> dict[str, Any]:
    artifact_map = {
        str(k): v for k, v in (artifacts or {}).items()
        if not str(k).startswith("s3:")
    }
    artifact_map = dict(sorted(artifact_map.items(), key=lambda kv: str(kv[0])))
    err_list = list(errors or [])
    warn_list = list(warnings or [])
    calls = list(llm_calls or [])
    status = (run_status or "").strip()
    if empty and status in ("", "success"):
        status = "empty"
    cov = coverage or infer_coverage(
        artifacts=artifact_map,
        errors=err_list,
        warnings=warn_list,
        run_profile=run_profile,
        run_quality=run_quality,
        run_pii=run_pii,
        llm_calls=calls,
        agentic=agentic,
        run_status=status,
        empty=empty or status == "empty",
    )
    for phase in _PHASES:
        cov.setdefault(phase, coverage_entry(CoverageStatus.NOT_RUN.value))
    logical_sources = sorted(
        p for p in (source_files or [])
        if p and not Path(str(p)).is_absolute()
    )
    manifest = EvidenceManifest(
        table=table,
        run_id=run_id,
        session_id=session_id,
        schema_version=SCHEMA_VERSION,
        run_status=status,
        coverage=cov,
        artifacts=artifact_map,
        engines=list(engines or []),
        llm_calls=calls,
        result_variants=list(result_variants or []),
        errors=err_list,
        warnings=warn_list,
        config_hash=config_hash,
        config_artifact=config_artifact,
        sensitivity=dict(sensitivity or {}),
        source_files=logical_sources,
    )
    return manifest.to_dict()
