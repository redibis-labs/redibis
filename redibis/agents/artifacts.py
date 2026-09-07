"""Agent-run artifact manifest and guarded download helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from redibis.agents.run_models import AgentRun, StepRecord, StepStatus
from redibis.services.session.loader import artifact_path_allowed
from redibis.store.storage_backend import LocalBackend, S3Backend, StorageBackend

_STORAGE_WORKFLOW_BY_KIND = {
    "enrich": ("enrich",),
}

_KIND_ORDER = {
    "report": 0,
    "contract": 1,
    "diff": 2,
    "evidence": 3,
    "masked_csv": 4,
    "log": 5,
}

_ALLOWED_ARTIFACT_FILES = {
    "interactive_review.html",
    "triage_report.html",
    "pii_detections.html",
    "pii_detection_report.html",
    "pii_report.json",
    "quality_contract.yaml",
    "pii_contract.yaml",
    "contract.deterministic.yaml",
    "contract.llm.yaml",
    "contract_diff.json",
    "contract_diff.md",
    "llm_prompt_context.json",
    "enrichment_meta.json",
    "evidence_bundle.json",
    "evidence_bundle.shareable.json",
    "evidence_manifest.json",
    "effective_config.yaml",
    "result_variants.json",
    "result_comparison.json",
    "run_trace.json",
    "run.log",
    "run_debug.log",
    "agent_report.json",
    "agent_report.md",
}

_ALLOWED_ARTIFACT_PATTERNS = (
    "quality-report-*.html",
    "profile-report-*.html",
)

_ALLOWED_DEEP_SCAN_FILES = {
    "manifest.json",
    "contract_synthesis.md",
    "columns.jsonl",
}

_ALLOWED_MASKING_SUFFIXES = (
    ".csv",
    ".parquet",
    ".manifest.json",
    ".audit.json",
)


@dataclass
class ArtifactEntry:
    """One downloadable agent-run artifact."""

    id: str
    name: str
    kind: str
    type: str
    table: str = ""
    step_id: str = ""
    size: int = 0
    viewable_in_redibis: bool = False
    local_path: str = ""
    storage_bucket: str = ""
    storage_key: str = ""
    scope_root: str = ""
    scope_prefix: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "type": self.type,
            "table": self.table,
            "step_id": self.step_id,
            "size": self.size,
            "viewable_in_redibis": self.viewable_in_redibis,
        }


def build_run_artifact_index(
    run: AgentRun,
    *,
    report_output_dir: Path,
    scan_output_dir: Optional[Path] = None,
    storage_backend: Optional[StorageBackend] = None,
    runs_bucket: str = "",
    lineage_root: Optional[Path] = None,
    contract_store: Optional[Any] = None,
) -> dict[str, ArtifactEntry]:
    """Aggregate artifacts for one agent run from step outputs, run folders, and storage."""

    entries: dict[str, ArtifactEntry] = {}
    seen: set[str] = set()
    report_output_dir = Path(report_output_dir)
    scan_output_dir = Path(scan_output_dir) if scan_output_dir is not None else None

    collected: set[tuple[str, str, str]] = set()
    for step in run.steps:
        table = step.table or ""
        run_id = str((step.output or {}).get("run_id") or "")
        for target_table, target_run_id in _step_storage_targets(step):
            key = (step.step_id, target_table, target_run_id)
            if key in collected:
                continue
            collected.add(key)
            run_roots = _run_roots(
                target_run_id,
                report_output_dir=report_output_dir,
                scan_output_dir=scan_output_dir,
            )
            for run_root in run_roots:
                _collect_run_dir(
                    entries,
                    seen,
                    run_root=run_root,
                    table=target_table,
                    step_id=step.step_id,
                )
            _collect_storage_artifacts(
                entries,
                seen,
                table=target_table,
                run_id=target_run_id,
                step=step,
                backend=storage_backend,
                runs_bucket=runs_bucket,
            )

    for step in run.steps:
        out = step.output or {}
        run_id = str(out.get("run_id") or "")
        table = step.table or ""
        run_roots = _run_roots(run_id, report_output_dir=report_output_dir, scan_output_dir=scan_output_dir)
        _collect_explicit_output_artifacts(
            entries,
            seen,
            step=step,
            run_roots=run_roots,
            table=table,
            run_id=run_id,
            backend=storage_backend,
            runs_bucket=runs_bucket,
        )

    if lineage_root is not None:
        _collect_agent_lineage_dir(
            entries,
            seen,
            lineage_root=Path(lineage_root),
            agent_run_id=run.run_id,
        )

    if contract_store is not None and lineage_root is not None:
        _collect_active_contract_exports(
            entries,
            seen,
            run=run,
            contract_store=contract_store,
            lineage_root=Path(lineage_root),
        )

    return dict(
        sorted(
            entries.items(),
            key=lambda item: (
                item[1].table or "",
                _KIND_ORDER.get(item[1].kind, 99),
                item[1].name,
            ),
        )
    )


def build_run_artifact_manifest(
    run: AgentRun,
    *,
    report_output_dir: Path,
    scan_output_dir: Optional[Path] = None,
    storage_backend: Optional[StorageBackend] = None,
    runs_bucket: str = "",
    lineage_root: Optional[Path] = None,
    contract_store: Optional[Any] = None,
) -> dict[str, Any]:
    """Return the public artifact manifest payload for one run."""

    index = build_run_artifact_index(
        run,
        report_output_dir=report_output_dir,
        scan_output_dir=scan_output_dir,
        storage_backend=storage_backend,
        runs_bucket=runs_bucket,
        lineage_root=lineage_root,
        contract_store=contract_store,
    )
    return {
        "run_id": run.run_id,
        "artifacts": [entry.to_public_dict() for entry in index.values()],
    }


def get_download_response(
    entry: ArtifactEntry,
    *,
    storage_backend: Optional[StorageBackend] = None,
):
    """Return a FastAPI response for one artifact entry."""

    from fastapi import HTTPException
    from fastapi.responses import FileResponse, Response

    if entry.local_path:
        target = Path(entry.local_path)
        scope_root = Path(entry.scope_root or target.parent)
        if not target.exists() or not artifact_path_allowed(target, run_dir=scope_root):
            raise HTTPException(status_code=404, detail="artifact not found")
        if target.is_dir():
            payload = _zip_local_dir(target, scope_root=scope_root)
            filename = f"{target.name}.zip"
            return Response(
                content=payload,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return FileResponse(target, filename=Path(entry.name).name)

    if not entry.storage_bucket or not entry.storage_key or storage_backend is None:
        raise HTTPException(status_code=404, detail="artifact not found")

    if not _storage_key_allowed(entry.storage_key, entry.scope_prefix):
        raise HTTPException(status_code=404, detail="artifact not found")

    filename = Path(entry.name.rstrip("/")).name or "artifact"
    if entry.type == "dir":
        payload = _zip_storage_prefix(
            storage_backend,
            entry.storage_bucket,
            entry.storage_key,
            scope_prefix=entry.scope_prefix,
        )
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}.zip"'},
        )

    try:
        data = storage_backend.get_bytes(entry.storage_bucket, entry.storage_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    media_type = _guess_media_type(filename)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run_roots(
    run_id: str,
    *,
    report_output_dir: Path,
    scan_output_dir: Optional[Path],
) -> list[Path]:
    roots: list[Path] = []
    for base in (report_output_dir, scan_output_dir):
        if not run_id or base is None:
            continue
        candidate = Path(base) / run_id
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _step_storage_targets(step: StepRecord) -> list[tuple[str, str]]:
    """Return ``(table, run_id)`` pairs for runs-bucket and local run-folder lookup."""
    out = step.output or {}
    table = step.table or ""
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []

    def add(candidate_table: str, run_id: str) -> None:
        t = (candidate_table or "").strip()
        rid = (run_id or "").strip()
        if t and rid and (t, rid) not in seen:
            seen.add((t, rid))
            pairs.append((t, rid))

    add(table, str(out.get("run_id") or ""))
    for sub in out.get("sub_steps") or []:
        sub_table = str(sub.get("table") or sub.get("contract_table") or table)
        add(sub_table, str(sub.get("run_id") or ""))
    return pairs


def _contract_tables_for_run(run: AgentRun) -> list[str]:
    """Tables that likely have an active contract after this run."""
    contract_nodes = {"enrich", "contract", "contract_write", "catalog_push"}
    tables: set[str] = set(run.tables or [])
    for step in run.steps:
        if step.status != StepStatus.COMPLETED:
            continue
        out = step.output or {}
        if step.table and (step.node_kind in contract_nodes or out.get("auto_written") or out.get("merged")):
            tables.add(step.table)
        for sub in out.get("sub_steps") or []:
            if sub.get("skipped") or sub.get("failed"):
                continue
            if sub.get("kind") in ("enrich", "quality", "pii", "classify"):
                sub_table = str(sub.get("table") or sub.get("contract_table") or step.table or "")
                if sub_table:
                    tables.add(sub_table)
    return sorted(tables)


def _collect_active_contract_exports(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    run: AgentRun,
    contract_store: Any,
    lineage_root: Path,
) -> None:
    """Materialize active ODCS contracts into the agent run dir for download."""
    import yaml

    agent_dir = lineage_root / run.run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    scope_root = agent_dir.resolve()
    for table in _contract_tables_for_run(run):
        active = contract_store.get_active(table)
        if not active:
            continue
        safe = _table_safe(table)
        display_name = f"contract.active.{safe}.yaml"
        path = agent_dir / display_name
        try:
            path.write_text(
                yaml.safe_dump(active, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        except OSError:
            continue
        _add_local_file(
            entries,
            seen,
            file_path=path,
            display_name=display_name,
            table=table,
            step_id="",
            scope_root=scope_root,
        )


def _collect_agent_lineage_dir(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    lineage_root: Path,
    agent_run_id: str,
) -> None:
    agent_dir = lineage_root / agent_run_id
    if not agent_dir.is_dir():
        return
    scope_root = agent_dir.resolve()
    for path in sorted(agent_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name not in {
            "run_trace.json",
            "run_debug.log",
            "run.log",
            "agent_report.json",
            "agent_report.md",
        } and not (name.startswith("contract.active.") and name.endswith(".yaml")):
            continue
        _add_local_file(
            entries,
            seen,
            file_path=path,
            display_name=name,
            table="",
            step_id="",
            scope_root=scope_root,
        )


def _collect_run_dir(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    run_root: Path,
    table: str,
    step_id: str,
) -> None:
    ge_report = run_root / "ge_report"
    if ge_report.is_dir():
        _add_local_dir(
            entries,
            seen,
            directory=ge_report,
            display_name="ge_report/",
            table=table,
            step_id=step_id,
            scope_root=run_root,
        )

    for path in sorted(run_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(run_root).as_posix()
        if rel.startswith("ge_report/"):
            continue
        _add_local_file(
            entries,
            seen,
            file_path=path,
            display_name=_display_name_for_run_file(rel, table=table),
            table=table,
            step_id=step_id,
            scope_root=run_root,
        )


def _collect_storage_artifacts(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    table: str,
    run_id: str,
    step: StepRecord,
    backend: Optional[StorageBackend],
    runs_bucket: str,
) -> None:
    if backend is None or not runs_bucket or not table or not run_id:
        return

    workflows = set(_STORAGE_WORKFLOW_BY_KIND.get(step.node_kind, ()))
    for sub_step in (step.output or {}).get("sub_steps") or []:
        kind = str((sub_step or {}).get("kind") or "")
        workflows.update(_STORAGE_WORKFLOW_BY_KIND.get(kind, ()))

    table_safe = _table_safe(table)
    for workflow in sorted(workflows):
        prefix = f"{workflow}/{table_safe}/{run_id}/"
        try:
            keys = backend.list_keys(runs_bucket, prefix=prefix)
        except Exception:
            continue
        if not keys:
            continue

        ge_prefix = f"{prefix}ge_report/"
        ge_keys = [key for key in keys if key.startswith(ge_prefix)]
        if ge_keys:
            _add_storage_dir(
                entries,
                seen,
                backend=backend,
                bucket=runs_bucket,
                key_prefix=ge_prefix,
                display_name="ge_report/",
                table=table,
                step_id=step.step_id,
                scope_prefix=prefix,
            )

        for key in keys:
            rel = key[len(prefix):]
            if rel.startswith("ge_report/"):
                continue
            _add_storage_file(
                entries,
                seen,
                backend=backend,
                bucket=runs_bucket,
                key=key,
                display_name=rel,
                table=table,
                step_id=step.step_id,
                scope_prefix=prefix,
            )


def _collect_explicit_output_artifacts(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    step: StepRecord,
    run_roots: list[Path],
    table: str,
    run_id: str,
    backend: Optional[StorageBackend],
    runs_bucket: str,
) -> None:
    output = step.output or {}
    refs: list[tuple[str, str]] = []

    artifacts = output.get("artifacts")
    if isinstance(artifacts, dict):
        for key, value in artifacts.items():
            if isinstance(value, str) and value.strip():
                refs.append((str(key), value))

    for key in ("manifest", "bundle"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            refs.append((key, value))

    if not refs:
        return

    table_safe = _table_safe(table) if table else ""
    for label, ref in refs:
        name_hint = label[3:] if label.startswith("s3:") else label
        if label.startswith("s3:") and backend is not None and runs_bucket:
            _add_storage_file(
                entries,
                seen,
                backend=backend,
                bucket=runs_bucket,
                key=ref,
                display_name=name_hint or Path(ref).name,
                table=table,
                step_id=step.step_id,
                scope_prefix=_storage_scope_prefix(ref, table_safe=table_safe, run_id=run_id),
            )
            continue

        candidate = _resolve_local_candidate(ref, run_roots)
        if candidate is not None:
            scope_root = _matching_scope_root(candidate, run_roots)
            if scope_root is None:
                continue
            display_name = candidate.name
            for root in run_roots:
                try:
                    display_name = candidate.relative_to(root).as_posix()
                    break
                except ValueError:
                    continue
            if candidate.is_dir():
                if candidate.name == "ge_report":
                    _add_local_dir(
                        entries,
                        seen,
                        directory=candidate,
                        display_name=f"{display_name.rstrip('/')}/",
                        table=table,
                        step_id=step.step_id,
                        scope_root=scope_root,
                    )
                else:
                    for nested in sorted(candidate.rglob("*")):
                        if not nested.is_file():
                            continue
                        nested_name = nested.relative_to(scope_root).as_posix()
                        _add_local_file(
                            entries,
                            seen,
                            file_path=nested,
                            display_name=nested_name,
                            table=table,
                            step_id=step.step_id,
                            scope_root=scope_root,
                        )
            else:
                _add_local_file(
                    entries,
                    seen,
                    file_path=candidate,
                    display_name=display_name,
                    table=table,
                    step_id=step.step_id,
                    scope_root=scope_root,
                )
            continue

        if backend is not None and runs_bucket and table_safe and run_id:
            scope_prefix = _storage_scope_prefix(ref, table_safe=table_safe, run_id=run_id)
            if scope_prefix and _storage_key_exists(backend, runs_bucket, ref):
                _add_storage_file(
                    entries,
                    seen,
                    backend=backend,
                    bucket=runs_bucket,
                    key=ref,
                    display_name=Path(ref).name,
                    table=table,
                    step_id=step.step_id,
                    scope_prefix=scope_prefix,
                )


def _resolve_local_candidate(ref: str, run_roots: list[Path]) -> Optional[Path]:
    raw = Path(ref)
    candidates = [raw]
    if not raw.is_absolute():
        candidates.extend(root / raw for root in run_roots)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.exists():
            return resolved
    return None


def _matching_scope_root(path: Path, run_roots: list[Path]) -> Optional[Path]:
    for root in run_roots:
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _add_local_file(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    file_path: Path,
    display_name: str,
    table: str,
    step_id: str,
    scope_root: Path,
) -> None:
    if not _artifact_name_allowed(display_name, is_dir=False):
        return
    try:
        resolved = file_path.resolve()
    except OSError:
        return
    if not resolved.is_file():
        return
    token = f"local:{resolved}"
    if token in seen:
        return
    seen.add(token)
    entry = ArtifactEntry(
        id=_artifact_id(token),
        name=display_name,
        kind=_infer_kind(display_name, is_dir=False),
        type=_infer_type(display_name, is_dir=False),
        table=table,
        step_id=step_id,
        size=resolved.stat().st_size,
        viewable_in_redibis=_viewable_in_redibis(display_name, is_dir=False, table=table),
        local_path=str(resolved),
        scope_root=str(scope_root.resolve()),
    )
    entries[entry.id] = entry


def _add_local_dir(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    directory: Path,
    display_name: str,
    table: str,
    step_id: str,
    scope_root: Path,
) -> None:
    if not _artifact_name_allowed(display_name, is_dir=True):
        return
    try:
        resolved = directory.resolve()
    except OSError:
        return
    if not resolved.is_dir():
        return
    token = f"localdir:{resolved}"
    if token in seen:
        return
    seen.add(token)
    entry = ArtifactEntry(
        id=_artifact_id(token),
        name=display_name,
        kind=_infer_kind(display_name, is_dir=True),
        type="dir",
        table=table,
        step_id=step_id,
        size=_dir_size(resolved),
        viewable_in_redibis=_viewable_in_redibis(display_name, is_dir=True, table=table),
        local_path=str(resolved),
        scope_root=str(scope_root.resolve()),
    )
    entries[entry.id] = entry


def _add_storage_file(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    backend: StorageBackend,
    bucket: str,
    key: str,
    display_name: str,
    table: str,
    step_id: str,
    scope_prefix: str,
) -> None:
    if not _artifact_name_allowed(display_name, is_dir=False):
        return
    token = f"storage:{bucket}:{key}"
    if token in seen:
        return
    seen.add(token)
    entry = ArtifactEntry(
        id=_artifact_id(token),
        name=display_name,
        kind=_infer_kind(display_name, is_dir=False),
        type=_infer_type(display_name, is_dir=False),
        table=table,
        step_id=step_id,
        size=_storage_size(backend, bucket, key),
        viewable_in_redibis=_viewable_in_redibis(display_name, is_dir=False, table=table),
        storage_bucket=bucket,
        storage_key=key,
        scope_prefix=scope_prefix,
    )
    entries[entry.id] = entry


def _add_storage_dir(
    entries: dict[str, ArtifactEntry],
    seen: set[str],
    *,
    backend: StorageBackend,
    bucket: str,
    key_prefix: str,
    display_name: str,
    table: str,
    step_id: str,
    scope_prefix: str,
) -> None:
    if not _artifact_name_allowed(display_name, is_dir=True):
        return
    token = f"storagedir:{bucket}:{key_prefix}"
    if token in seen:
        return
    seen.add(token)
    entry = ArtifactEntry(
        id=_artifact_id(token),
        name=display_name,
        kind=_infer_kind(display_name, is_dir=True),
        type="dir",
        table=table,
        step_id=step_id,
        size=_storage_dir_size(backend, bucket, key_prefix),
        viewable_in_redibis=_viewable_in_redibis(display_name, is_dir=True, table=table),
        storage_bucket=bucket,
        storage_key=key_prefix,
        scope_prefix=scope_prefix,
    )
    entries[entry.id] = entry


def _display_name_for_run_file(rel: str, *, table: str) -> str:
    """Map persisted task log filenames to board-friendly download names."""
    leaf = Path(rel).name
    if leaf.endswith(".log") and table and leaf.startswith(f"{table}."):
        return f"{table}.debug.log"
    return rel


def _artifact_id(token: str) -> str:
    return hashlib.sha1(token.encode("utf-8")).hexdigest()


def _artifact_name_allowed(name: str, *, is_dir: bool) -> bool:
    normalized = str(name or "").replace("\\", "/").strip()
    if not normalized:
        return False

    trimmed = normalized.rstrip("/")
    lower = trimmed.lower()
    leaf = Path(lower).name

    if is_dir:
        return lower == "ge_report"

    if lower.startswith("llm_calls/") and lower.endswith(".json") and not lower.endswith(".raw.json"):
        return True

    if lower in _ALLOWED_ARTIFACT_FILES:
        return True

    if lower.startswith("contract.active.") and lower.endswith(".yaml"):
        return True

    if lower.endswith(".log"):
        return True

    if any(fnmatch.fnmatch(leaf, pattern) for pattern in _ALLOWED_ARTIFACT_PATTERNS):
        return True

    if lower.startswith("deep_scan_bundle/") and leaf in _ALLOWED_DEEP_SCAN_FILES:
        return True

    if any(lower.endswith(suffix) for suffix in _ALLOWED_MASKING_SUFFIXES):
        return "mask_" in lower or "masked" in lower

    return False


def _infer_type(name: str, *, is_dir: bool) -> str:
    if is_dir:
        return "dir"
    suffix = Path(name).suffix.lower()
    return {
        ".html": "html",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".jsonl": "jsonl",
        ".csv": "csv",
        ".md": "md",
        ".log": "log",
        ".txt": "txt",
    }.get(suffix, suffix.lstrip(".") or "file")


def _infer_kind(name: str, *, is_dir: bool) -> str:
    lower = name.lower().rstrip("/")
    if is_dir:
        if lower.endswith("ge_report"):
            return "report"
        if lower.endswith("logs"):
            return "log"
        return "evidence"
    if "masked" in lower and lower.endswith(".csv"):
        return "masked_csv"
    if "diff" in lower:
        return "diff"
    if lower.endswith((".yaml", ".yml")) and "contract" in lower:
        return "contract"
    if lower.endswith(".html"):
        return "report"
    if "/logs/" in lower or lower.endswith((".log", ".jsonl")):
        return "log"
    if any(token in lower for token in ("evidence/", "bundle/", "tuning_bundle/", "manifest", "pii_report", "detections", "quality_results")):
        return "evidence"
    if lower.endswith((".json", ".md")):
        return "evidence"
    if lower.endswith(".csv"):
        return "masked_csv" if "masked" in lower else "report"
    return "report"


def _viewable_in_redibis(name: str, *, is_dir: bool, table: str) -> bool:
    if not table:
        return False
    kind = _infer_kind(name, is_dir=is_dir)
    artifact_type = _infer_type(name, is_dir=is_dir)
    return kind in {"report", "contract", "diff"} and artifact_type in {"html", "yaml", "md", "dir"}


def _table_safe(table: str) -> str:
    return str(table).replace(".", "_")


def _dir_size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def _storage_dir_size(backend: StorageBackend, bucket: str, key_prefix: str) -> int:
    total = 0
    try:
        keys = backend.list_keys(bucket, prefix=key_prefix)
    except Exception:
        return 0
    for key in keys:
        total += _storage_size(backend, bucket, key)
    return total


def _storage_size(backend: StorageBackend, bucket: str, key: str) -> int:
    try:
        if isinstance(backend, LocalBackend):
            path = backend._full_path(bucket, key)
            return path.stat().st_size if path.is_file() else 0
        if isinstance(backend, S3Backend):
            meta = backend.client.head_object(Bucket=bucket, Key=key)
            return int(meta.get("ContentLength") or 0)
    except Exception:
        return 0

    try:
        return len(backend.get_bytes(bucket, key))
    except Exception:
        return 0


def _storage_key_exists(backend: StorageBackend, bucket: str, key: str) -> bool:
    try:
        return backend.exists(bucket, key)
    except Exception:
        return False


def _storage_scope_prefix(key: str, *, table_safe: str, run_id: str) -> str:
    for workflow in _STORAGE_WORKFLOW_BY_KIND.get("enrich", ()):
        prefix = f"{workflow}/{table_safe}/{run_id}/"
        if key.startswith(prefix):
            return prefix
    return ""


def _storage_key_allowed(key: str, scope_prefix: str) -> bool:
    if not scope_prefix:
        return False
    return key == scope_prefix.rstrip("/") or key.startswith(scope_prefix)


def _zip_local_dir(directory: Path, *, scope_root: Path) -> bytes:
    if not artifact_path_allowed(directory, run_dir=scope_root):
        raise ValueError("directory outside allowed scope")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if not artifact_path_allowed(path, run_dir=scope_root):
                continue
            zf.write(path, arcname=path.relative_to(directory).as_posix())
    return buf.getvalue()


def _zip_storage_prefix(
    backend: StorageBackend,
    bucket: str,
    key_prefix: str,
    *,
    scope_prefix: str,
) -> bytes:
    if not _storage_key_allowed(key_prefix.rstrip("/") + "/", scope_prefix):
        raise ValueError("directory outside allowed scope")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key in sorted(backend.list_keys(bucket, prefix=key_prefix)):
            if not _storage_key_allowed(key, scope_prefix):
                continue
            rel = key[len(key_prefix):]
            if not rel:
                continue
            zf.writestr(rel, backend.get_bytes(bucket, key))
    return buf.getvalue()


def _guess_media_type(name: str) -> str:
    suffix = Path(name).suffix
    return StorageBackend._guess_content_type(suffix)
