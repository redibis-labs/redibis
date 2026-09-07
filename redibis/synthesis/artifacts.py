"""Write Contract Synthesis run artifacts to a local output directory.

Synthesis is portable — artifacts are written to disk (or an optional
``RunOutputWriter``) and **never** through ``ContractStore.upsert``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import yaml


def write_synthesis_artifacts(
    output_dir: Path | str,
    *,
    candidate: dict[str, Any],
    base_contract: Optional[dict[str, Any]] = None,
    evidence: Optional[dict[str, Any]] = None,
    lineage: Optional[dict[str, Any]] = None,
    lineage_react_flow: Optional[dict[str, Any]] = None,
    traceability: Optional[dict[str, Any]] = None,
    stage_results: Optional[list[dict[str, Any]]] = None,
    meta: Optional[dict[str, Any]] = None,
    comparison: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """Persist synthesis outputs; return ``{name: path}`` map."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    def _write_yaml(name: str, data: Any) -> None:
        path = root / name
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        written[name] = str(path)

    def _write_json(name: str, data: Any) -> None:
        path = root / name
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        written[name] = str(path)

    if base_contract is not None:
        _write_yaml("contract.base.v3.yaml", base_contract)
    _write_yaml("contract.synthesized.v3.1.yaml", candidate)
    _write_json("contract.synthesized.v3.1.json", candidate)

    if evidence is not None:
        _write_json("evidence_bundle.json", evidence)
    if lineage is not None:
        _write_json("lineage.json", lineage)
    if lineage_react_flow is not None:
        _write_json("lineage_graph.json", lineage_react_flow)
    if traceability is not None:
        _write_json("requirements_traceability.json", traceability)
        # Markdown matrix
        rows = (traceability or {}).get("rows") or []
        lines = [
            "# Requirements traceability",
            "",
            f"Coverage: `{json.dumps((traceability or {}).get('coverage') or {})}`",
            "",
            "| Requirement | Status | Contract paths | Notes |",
            "|---|---|---|---|",
        ]
        for row in rows:
            paths = ", ".join(row.get("contract_paths") or []) or "—"
            lines.append(
                f"| {row.get('requirement_id')} | {row.get('status')} | "
                f"`{paths}` | {(row.get('notes') or '').replace('|', '/')} |"
            )
        md_path = root / "requirements_traceability.md"
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written["requirements_traceability.md"] = str(md_path)

    if stage_results is not None:
        _write_json("stage_results.json", stage_results)
    if meta is not None:
        _write_json("synthesis_meta.json", meta)
    if comparison is not None:
        _write_json("analysis_comparison.json", comparison)

    return written
