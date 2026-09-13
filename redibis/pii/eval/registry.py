"""In-process (+ optional directory) registry of evaluation runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

_RUNS: dict[str, dict[str, Any]] = {}


def registry_dir() -> Path | None:
    raw = (os.getenv("REDIBIS_EVAL_REGISTRY_DIR") or "").strip()
    if raw:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return None


def put_run(report: Mapping[str, Any]) -> str:
    run_uuid = str((report.get("provenance") or {}).get("run_uuid") or "")
    if not run_uuid:
        from redibis.pii.eval.provenance import new_run_uuid

        run_uuid = new_run_uuid()
        provenance = dict(report.get("provenance") or {})
        provenance["run_uuid"] = run_uuid
        stored = dict(report)
        stored["provenance"] = provenance
    else:
        stored = dict(report)
    _RUNS[run_uuid] = stored
    dest = registry_dir()
    if dest is not None:
        (dest / f"{run_uuid}.json").write_text(
            json.dumps(stored, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return run_uuid


def get_run(run_uuid: str) -> dict[str, Any] | None:
    uid = str(run_uuid or "").strip()
    if uid in _RUNS:
        return _RUNS[uid]
    dest = registry_dir()
    if dest is None:
        return None
    path = dest / f"{uid}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        _RUNS[uid] = data
        return data
    return None


def list_runs() -> list[dict[str, Any]]:
    dest = registry_dir()
    if dest is not None:
        for path in dest.glob("*.json"):
            if path.stem not in _RUNS:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    _RUNS[path.stem] = data
    rows = []
    for uid, report in _RUNS.items():
        prov = report.get("provenance") or {}
        exact = (report.get("exact") or {}).get("micro") or {}
        value = (report.get("value") or {}).get("micro") or {}
        rows.append({
            "run_uuid": uid,
            "label": prov.get("label") or "",
            "redibis_version": report.get("redibis_version") or prov.get("redibis_version"),
            "dataset_id": report.get("dataset_id") or "",
            "rules_checksum": prov.get("rules_checksum") or "",
            "normalization_profile": report.get("normalization_profile") or prov.get("normalization_profile") or "",
            "case_count": report.get("case_count") or 0,
            "strict_f1": exact.get("f1"),
            "value_f1": value.get("f1"),
            "gate": (report.get("gates") or {}).get("summary"),
        })
    rows.sort(key=lambda r: str(r.get("run_uuid")))
    return rows


def _cases(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    if report.get("cases"):
        return list(report.get("cases") or [])
    out: list[dict[str, Any]] = []
    for entry in report.get("files") or []:
        out.extend(entry.get("cases") or [])
    return out


def get_case(run_uuid: str, case_id: str) -> dict[str, Any] | None:
    report = get_run(run_uuid)
    if not report:
        return None
    for case in _cases(report):
        if str(case.get("id") or "") == str(case_id):
            return {
                "run_uuid": run_uuid,
                "provenance": report.get("provenance") or {},
                "case": case,
            }
    return None


def compare_runs(a_uuid: str, b_uuid: str) -> dict[str, Any]:
    left = get_run(a_uuid)
    right = get_run(b_uuid)
    if left is None:
        raise KeyError(f"unknown run {a_uuid}")
    if right is None:
        raise KeyError(f"unknown run {b_uuid}")
    left_cases = {str(c.get("id") or ""): c for c in _cases(left)}
    right_cases = {str(c.get("id") or ""): c for c in _cases(right)}
    ids = sorted(set(left_cases) | set(right_cases))
    changes: list[dict[str, Any]] = []
    for case_id in ids:
        c1 = left_cases.get(case_id)
        c2 = right_cases.get(case_id)
        if c1 is None or c2 is None:
            changes.append({
                "case_id": case_id,
                "change": "added" if c1 is None else "removed",
            })
            continue
        d1 = _class_by_expected(c1)
        d2 = _class_by_expected(c2)
        keys = sorted(set(d1) | set(d2))
        for key in keys:
            a_cls = d1.get(key)
            b_cls = d2.get(key)
            if a_cls != b_cls:
                changes.append({
                    "case_id": case_id,
                    "expected_id": key,
                    "from": a_cls,
                    "to": b_cls,
                })
    return {
        "a": a_uuid,
        "b": b_uuid,
        "changes": changes,
        "change_count": len(changes),
        "a_micro": {
            "strict": (left.get("strict") or left.get("exact") or {}).get("micro"),
            "value": (left.get("value") or {}).get("micro"),
        },
        "b_micro": {
            "strict": (right.get("strict") or right.get("exact") or {}).get("micro"),
            "value": (right.get("value") or {}).get("micro"),
        },
    }


def _class_by_expected(case: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in case.get("match_classes") or []:
        eid = row.get("expected_id")
        if eid:
            out[str(eid)] = str(row.get("class") or "")
    return out
