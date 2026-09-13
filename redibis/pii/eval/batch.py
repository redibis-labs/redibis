"""Discover use-case JSON files and score them without stopping on failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from redibis.pii.eval.runner import EvalCancelled, evaluate_with_service
from redibis.pii.eval.span_metrics import (
    BATCH_REPORT_KIND,
    SCHEMA_VERSION_1_2,
    aggregate_cases,
    aggregate_classes,
    aggregate_tags,
    classify_eval_payload,
    current_redibis_version,
)

ProgressCb = Callable[[str, dict[str, Any]], None]


def discover_eval_files(path: Path, *, recursive: bool = False) -> list[Path]:
    """Return JSON files to attempt. Directories default to top-level ``*.json``."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(str(path))
    pattern = "**/*.json" if recursive else "*.json"
    root = path.resolve()
    return sorted(
        p
        for p in path.glob(pattern)
        if p.is_file()
        and not p.is_symlink()
        and p.resolve().is_relative_to(root)
    )


def _load_json(path: Path) -> tuple[Any, Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except OSError as exc:
        return None, f"cannot read file: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"


def _rel_name(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _file_entry(
    path: Path,
    *,
    root: Path,
    status: str,
    error: str = "",
    report: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": _rel_name(path, root),
        "name": path.name,
        "status": status,
        "error": error,
    }
    if report:
        entry["dataset_id"] = str(report.get("dataset_id") or path.stem)
        entry["case_count"] = int(report.get("case_count") or 0)
        entry["proposal_count"] = int(report.get("proposal_count") or 0)
        entry["exact"] = report.get("exact")
        entry["strict"] = report.get("strict") or report.get("exact")
        entry["value"] = report.get("value")
        entry["overlap"] = report.get("overlap")
        entry["type"] = report.get("type")
        entry["class_distribution"] = report.get("class_distribution")
        entry["cases"] = report.get("cases") or []
        entry["provenance"] = report.get("provenance") or {}
        entry["scan_config"] = report.get("scan_config") or {}
        entry["redibis_version"] = report.get("redibis_version")
    else:
        entry["dataset_id"] = path.stem
        entry["case_count"] = 0
        entry["cases"] = []
    return entry


def assemble_batch_report(
    files: Sequence[Mapping[str, Any]],
    *,
    overlap_iou: float = 0.5,
    source: str = "",
) -> dict[str, Any]:
    evaluated_cases: list[Mapping[str, Any]] = []
    evaluated = 0
    failed = 0
    for entry in files:
        if entry.get("status") == "evaluated":
            evaluated += 1
            evaluated_cases.extend(entry.get("cases") or [])
        else:
            failed += 1
    sample = evaluated_cases[0] if evaluated_cases else {}
    provenance = {}
    for entry in files:
        if entry.get("status") == "evaluated" and entry.get("provenance"):
            provenance = dict(entry.get("provenance") or {})
            break
    payload: dict[str, Any] = {
        "kind": BATCH_REPORT_KIND,
        "schema_version": SCHEMA_VERSION_1_2,
        "redibis_version": current_redibis_version(),
        "offset_unit": "unicode_codepoint",
        "overlap_iou": overlap_iou,
        "normalization_profile": str(provenance.get("normalization_profile") or "v1"),
        "source": source,
        "file_count": len(files),
        "evaluated_count": evaluated,
        "failed_count": failed,
        "case_count": len(evaluated_cases),
        "by_tag": (
            aggregate_tags(evaluated_cases, "value" if "value" in sample else "exact")
            if evaluated_cases and ("value" in sample or "exact" in sample)
            else {}
        ),
        "class_distribution": aggregate_classes(evaluated_cases) if evaluated_cases else {},
        "provenance": provenance,
        "files": list(files),
    }
    if evaluated_cases and "exact" in sample:
        exact = aggregate_cases(evaluated_cases, "exact")
        payload["exact"] = exact
        payload["strict"] = exact
    if evaluated_cases and "value" in sample:
        payload["value"] = aggregate_cases(evaluated_cases, "value")
    if evaluated_cases and "overlap" in sample:
        payload["overlap"] = aggregate_cases(evaluated_cases, "overlap")
    if evaluated_cases and "type" in sample:
        payload["type"] = aggregate_cases(evaluated_cases, "type")
    return payload


def evaluate_path(
    svc: Any,
    path: Path,
    *,
    options: Optional[Mapping[str, Any]] = None,
    recursive: bool = False,
    fail_fast: bool = False,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[Any] = None,
) -> dict[str, Any]:
    """Score a file or a folder of use-case JSON files. Continue after failures."""
    opts = dict(options or {})
    files = discover_eval_files(path, recursive=recursive)
    root = path if path.is_dir() else path.parent
    entries: list[dict[str, Any]] = []
    for index, file_path in enumerate(files):
        if cancel_event is not None and cancel_event.is_set():
            raise EvalCancelled("evaluation cancelled")
        if progress_cb is not None:
            progress_cb(
                "file",
                {
                    "index": index,
                    "total": len(files),
                    "path": _rel_name(file_path, root),
                    "name": file_path.name,
                },
            )
        raw, error = _load_json(file_path)
        if error:
            entries.append(_file_entry(file_path, root=root, status="failed", error=error))
            if fail_fast:
                break
            continue
        status, reason = classify_eval_payload(raw)
        if status != "ok":
            entries.append(_file_entry(file_path, root=root, status="failed", error=reason))
            if fail_fast:
                break
            continue
        try:
            report = evaluate_with_service(
                svc,
                raw,
                options=opts,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
        except EvalCancelled:
            raise
        except Exception as exc:
            entries.append(_file_entry(file_path, root=root, status="failed", error=str(exc)))
            if fail_fast:
                break
            continue
        entries.append(_file_entry(file_path, root=root, status="evaluated", report=report))
    if not files:
        entries.append(
            {
                "path": path.name,
                "name": path.name,
                "status": "failed",
                "error": "no JSON files found",
                "dataset_id": path.name,
                "case_count": 0,
                "cases": [],
            }
        )
    return assemble_batch_report(
        entries,
        overlap_iou=float(opts.get("overlap_iou") or 0.5),
        source=path.name,
    )


__all__ = [
    "assemble_batch_report",
    "discover_eval_files",
    "evaluate_path",
]
