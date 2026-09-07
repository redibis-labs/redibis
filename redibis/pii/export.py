"""Export regex catalogue and NER model inventory (CLI / API / LLM tuning)."""

from __future__ import annotations

import csv
import io
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from redibis.pii.ner_registry import NERModelRegistry
from redibis.pii.regex_catalog import _entry_to_dict, build_effective_catalog
from redibis.pii.regex_overrides import RegexOverrides


def _catalog_rows(
    *,
    overrides: RegexOverrides | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    cat = build_effective_catalog(overrides)
    rows: list[dict[str, Any]] = []
    for name, entry in sorted(cat.items()):
        if active_only and not entry.active:
            continue
        rows.append(_entry_to_dict(name, entry))
    return rows


def _render(rows: list[dict], fmt: str) -> str:
    fmt = (fmt or "json").lower()
    if fmt == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False)
    if fmt == "yaml":
        return yaml.safe_dump(rows, sort_keys=False, allow_unicode=True)
    if fmt == "csv":
        if not rows:
            return ""
        buf = io.StringIO()
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            for key in ("context_hints",):
                if key in flat and isinstance(flat[key], (list, tuple)):
                    flat[key] = "|".join(str(x) for x in flat[key])
            writer.writerow(flat)
        return buf.getvalue()
    raise ValueError(f"unsupported format {fmt!r}; use json, yaml, or csv")


def export_regex_catalog(
    *,
    overrides: RegexOverrides | None = None,
    active_only: bool = False,
    fmt: str = "json",
) -> str | list[dict]:
    """Serialize the effective catalog. Returns str for json/yaml/csv, else list."""
    rows = _catalog_rows(overrides=overrides, active_only=active_only)
    if fmt == "list":
        return rows
    return _render(rows, fmt)


def list_regex_catalog_summary(
    *,
    overrides: RegexOverrides | None = None,
    active_only: bool = True,
) -> list[dict[str, str]]:
    """Compact name / entity / active listing for CLI ``pii regex list``."""
    rows = _catalog_rows(overrides=overrides, active_only=active_only)
    return [
        {
            "name": r["name"],
            "entity_type": r["entity_type"],
            "group": r["recognizer_group"],
            "active": str(r["active"]),
        }
        for r in rows
    ]


def export_ner_models(models_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """List discovered NER models with labels and manifest metadata."""
    from redibis.pii.model_upload import resolve_models_dir

    root = resolve_models_dir(str(models_dir) if models_dir else None)
    specs = NERModelRegistry.discover(root)
    return [asdict(s) for s in specs]


def export_ner_bundle(name: str, out_dir: str | Path, *, models_dir: str | None = None) -> Path:
    """Copy a model directory + manifest into a transferable folder."""
    from redibis.pii.model_upload import resolve_models_dir

    root = resolve_models_dir(models_dir)
    src = root / name
    if not src.is_dir():
        raise FileNotFoundError(f"NER model {name!r} not found under {root}")
    dest = Path(out_dir) / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


def write_pii_signals_artifact(
    detections: list,
    *,
    table: str,
    out_dir: str | Path,
) -> Path:
    """Write per-column regex_hits + ner_hits evidence (no raw cell values)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe_table = table.replace("/", "_").replace("\\", "_")
    path = out / f"pii_signals.{safe_table}.json"
    columns = {}
    for d in detections:
        columns[d.column] = {
            "regex_hits": list(d.regex_hits or []),
            "ner_hits": list(d.ner_hits or []),
            "entity_type": d.entity_type,
            "presidio_score": d.presidio_score,
            "gliner_score": d.gliner_score,
        }
    payload = {"table": table, "columns": columns}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    md_path = out / f"pii_signals.{safe_table}.md"
    lines = [f"# PII signals — {table}", ""]
    for col, data in sorted(columns.items()):
        lines.append(f"## {col}")
        for hit in data.get("regex_hits") or []:
            lines.append(
                f"- regex `{hit.get('pattern_name')}` → {hit.get('entity_type')} "
                f"(score={hit.get('score')}, match_rate={hit.get('match_rate')})"
            )
        for hit in data.get("ner_hits") or []:
            lines.append(
                f"- NER `{hit.get('label')}` labels={hit.get('labels')} "
                f"(score={hit.get('score')}, match_rate={hit.get('match_rate')})"
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return path
