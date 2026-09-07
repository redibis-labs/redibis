"""
Pure contract diff — deterministic (C_det) vs LLM-enriched (C_llm).

Used for per-run governance artifacts consumed downstream (e.g. OpenMetadata).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

from redibis.enrich.diff import _columns_map, _first_schema

# Per-column fields compared between deterministic and LLM contracts.
_DIFF_FIELDS: tuple[str, ...] = (
    "classification",
    "entity_type",
    "tags",
    "privacy",
    "business.definition",
    "quality",
)


def diff_contracts(c_det: dict, c_llm: dict) -> dict:
    """Walk schema columns; emit per-field override / add / unchanged rows."""
    det_cols = _columns_map(c_det)
    llm_cols = _columns_map(c_llm)
    all_names = sorted(set(det_cols) | set(llm_cols))

    fields: list[dict] = []
    summary = {"overrides": 0, "adds": 0, "unchanged": 0}

    for name in all_names:
        det_snap = _field_snapshot(det_cols.get(name, {}))
        llm_snap = _field_snapshot(llm_cols.get(name, {}))
        for field in _DIFF_FIELDS:
            det_val = _empty_as_none(det_snap.get(field))
            llm_val = _empty_as_none(llm_snap.get(field))
            if det_val == llm_val:
                kind = "unchanged"
            elif det_val is None and llm_val is not None:
                kind = "add"
            else:
                kind = "override"
            if kind == "override":
                summary["overrides"] += 1
            elif kind == "add":
                summary["adds"] += 1
            else:
                summary["unchanged"] += 1
            fields.append({
                "column": name,
                "path": f"schema.properties.{name}.{field}",
                "field": field,
                "det_value": det_val,
                "llm_value": llm_val,
                "kind": kind,
            })

    return {"fields": fields, "summary": summary, "columns": all_names}


def render_diff_md(diff: dict) -> str:
    """Human-readable Markdown grouped by column change kind."""
    by_column: dict[str, dict[str, list[dict]]] = {}
    for row in diff.get("fields") or []:
        col = row.get("column") or "?"
        kind = row.get("kind") or "unchanged"
        by_column.setdefault(col, {"override": [], "add": [], "unchanged": []})
        by_column[col][kind].append(row)

    lines = [
        "# Contract diff (deterministic → LLM)",
        "",
        f"- Overrides: **{diff.get('summary', {}).get('overrides', 0)}**",
        f"- Adds: **{diff.get('summary', {}).get('adds', 0)}**",
        f"- Unchanged fields: **{diff.get('summary', {}).get('unchanged', 0)}**",
        "",
    ]

    def _section(title: str, kind: str) -> None:
        changed_cols = [
            c for c, kinds in by_column.items() if kinds.get(kind)
        ]
        if not changed_cols:
            return
        lines.append(f"## {title}")
        lines.append("")
        for col in sorted(changed_cols):
            lines.append(f"### `{col}`")
            for row in by_column[col][kind]:
                field = row.get("field", "")
                if kind == "add":
                    lines.append(f"- **{field}** (added): {_fmt(row.get('llm_value'))}")
                else:
                    lines.append(
                        f"- **{field}**: {_fmt(row.get('det_value'))} → "
                        f"{_fmt(row.get('llm_value'))}"
                    )
            lines.append("")

    _section("LLM changed", "override")
    _section("LLM added", "add")
    _section("Unchanged", "unchanged")
    return "\n".join(lines).rstrip() + "\n"


def _field_snapshot(prop: dict) -> dict[str, Any]:
    from redibis.contracts.privacy import col_pii_engine

    ce = col_pii_engine(prop) if prop else {}
    business = prop.get("business") if isinstance(prop.get("business"), dict) else {}
    return {
        "classification": prop.get("classification"),
        "entity_type": ce.get("entity_type") or prop.get("entity_type"),
        "tags": sorted(prop.get("tags") or []),
        "privacy": _normalize_privacy(prop.get("privacy")),
        "business.definition": business.get("definition"),
        "quality": _normalize_quality(prop.get("quality")),
    }


def _normalize_privacy(privacy: Any) -> Optional[dict]:
    if not isinstance(privacy, dict) or not privacy:
        return None
    out = copy.deepcopy(privacy)
    ce = out.get("classification_engine")
    if isinstance(ce, dict):
        out["classification_engine"] = {
            k: ce[k]
            for k in ("detected", "entity_type", "confidence", "classification")
            if k in ce
        }
    return out


def _normalize_quality(quality: Any) -> Optional[list]:
    if not quality:
        return None
    if not isinstance(quality, list):
        return None
    return sorted(
        [json.dumps(q, sort_keys=True, default=str) for q in quality if isinstance(q, dict)],
    )


def _empty_as_none(value: Any) -> Any:
    if value is None or value == "" or value == [] or value == {}:
        return None
    return value


def _fmt(value: Any) -> str:
    if value is None:
        return "_(none)_"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(text) > 120:
            return text[:117] + "..."
        return text
    text = str(value)
    if len(text) > 120:
        return text[:117] + "..."
    return text
