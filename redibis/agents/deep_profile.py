"""Tier-2 profiling capability catalogue (Phase 7)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "profiling_capabilities.yaml"


@dataclass(frozen=True)
class CapabilityParam:
    name: str
    param_type: str = "string"
    default: Any = None
    required: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfilingCapability:
    id: str
    engine: str
    label: str
    description: str
    params: tuple[CapabilityParam, ...] = ()
    output: str = "profiling_report"
    cost: str = "low"

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        merged = {p.name: p.default for p in self.params if p.default is not None}
        merged.update(params or {})
        for spec in self.params:
            if spec.required and spec.name not in merged:
                errors.append(f"missing param {spec.name!r}")
            val = merged.get(spec.name)
            if val is None:
                continue
            if spec.choices and str(val) not in spec.choices:
                if isinstance(val, list):
                    bad = [v for v in val if str(v) not in spec.choices]
                    if bad:
                        errors.append(f"{spec.name} values {bad} not in {list(spec.choices)}")
                else:
                    errors.append(f"{spec.name} not in {list(spec.choices)}")
            if spec.param_type in ("int", "float") and spec.min_value is not None:
                try:
                    num = float(val)
                except (TypeError, ValueError):
                    errors.append(f"{spec.name} must be numeric")
                else:
                    if num < spec.min_value:
                        errors.append(f"{spec.name} >= {spec.min_value}")
                    if spec.max_value is not None and num > spec.max_value:
                        errors.append(f"{spec.name} <= {spec.max_value}")
        return errors


def load_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or _CATALOG_PATH
    if not p.is_file():
        return {"version": "1.0", "capabilities": [], "deny": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _ydata_available() -> bool:
    from redibis.profiling.ydata_report import ydata_available

    return ydata_available()


def _capability_available(cap_id: str) -> bool:
    # ydata.correlations always listed — dispatches to pandas when the extra is absent.
    if cap_id == "ydata.profile_report":
        return _ydata_available()
    return True


def list_capabilities(path: Optional[Path] = None) -> list[ProfilingCapability]:
    raw = load_catalog(path)
    caps: list[ProfilingCapability] = []
    for item in raw.get("capabilities") or []:
        cap_id = str(item["id"])
        if not _capability_available(cap_id):
            continue
        param_specs: list[CapabilityParam] = []
        param_block = item.get("params") or {}
        if isinstance(param_block, dict):
            for name, spec in param_block.items():
                if isinstance(spec, dict):
                    param_specs.append(CapabilityParam(
                        name=name,
                        param_type=spec.get("type", "string"),
                        default=spec.get("default"),
                        required=bool(spec.get("required")),
                        min_value=spec.get("min"),
                        max_value=spec.get("max"),
                        choices=tuple(spec.get("choices") or ()),
                    ))
        caps.append(ProfilingCapability(
            id=cap_id,
            engine=str(item.get("engine") or ""),
            label=str(item.get("label") or item["id"]),
            description=str(item.get("description") or ""),
            params=tuple(param_specs),
            output=str(item.get("output") or "profiling_report"),
            cost=str(item.get("cost") or "low"),
        ))
    return caps


def get_capability(cap_id: str, path: Optional[Path] = None) -> ProfilingCapability:
    if not _capability_available(cap_id):
        raise KeyError(
            f"profiling capability {cap_id!r} unavailable "
            "(install redibis[ydata] for ydata.profile_report)"
        )
    for cap in list_capabilities(path):
        if cap.id == cap_id:
            return cap
    raise KeyError(f"unknown profiling capability: {cap_id!r}")


def _deny_match(cap_id: str, deny: list[str]) -> bool:
    import fnmatch
    for pattern in deny:
        if fnmatch.fnmatch(cap_id, pattern):
            return True
    return False


def run_deep_profile(
    table: str,
    run_id: str,
    capability_ids: list[str],
    params_by_id: Optional[dict[str, dict[str, Any]]] = None,
    *,
    run_dir: Path,
    config: Any = None,
    df: Any = None,
) -> dict[str, Any]:
    """
    Dispatch whitelisted catalogue capabilities inside redibis — profiling report only.
    """
    from redibis.config import RedibisConfig
    from redibis.telemetry.otel import run_context

    cfg = config if config is not None else RedibisConfig.default()
    catalog = load_catalog()
    deny = list(catalog.get("deny") or [])
    params_by_id = params_by_id or {}
    reports: list[dict[str, Any]] = []
    errors: list[str] = []

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    with run_context(run_id) as tel:
        for cap_id in capability_ids:
            if _deny_match(cap_id, deny):
                errors.append(f"denied capability {cap_id!r}")
                continue
            try:
                cap = get_capability(cap_id)
            except KeyError as exc:
                errors.append(str(exc))
                continue
            param_errors = cap.validate_params(params_by_id.get(cap_id) or {})
            if param_errors:
                errors.extend([f"{cap_id}: {e}" for e in param_errors])
                continue

            with tel.span("deep_profile", capability=cap_id, table=table, engine=cap.engine):
                artifact = _dispatch_capability(
                    cap,
                    table=table,
                    run_id=run_id,
                    df=df,
                    config=cfg,
                    params=params_by_id.get(cap_id) or {},
                    run_dir=run_dir,
                )

            if artifact.get("html_path"):
                html_path = Path(artifact["html_path"])
                reports.append({
                    "capability_id": cap_id,
                    "artifact": str(html_path),
                    "artifact_type": "html",
                    "summary": artifact.get("summary", {}),
                    "chat_summary": artifact.get("chat_summary", ""),
                })
            else:
                artifact_path = run_dir / f"deep_profile_{cap_id.replace('.', '_')}.json"
                import json
                artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
                reports.append({
                    "capability_id": cap_id,
                    "artifact": str(artifact_path),
                    "artifact_type": "json",
                    "summary": artifact.get("summary", {}),
                })

    manifest_path = run_dir / "deep_profile_manifest.json"
    import json
    manifest = {"table": table, "run_id": run_id, "reports": reports, "errors": errors}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "table": table,
        "run_id": run_id,
        "reports": reports,
        "errors": errors,
        "manifest": str(manifest_path),
        "telemetry": tel.export(),
    }


def _dispatch_capability(
    cap: ProfilingCapability,
    *,
    table: str,
    run_id: str,
    df: Any,
    config: Any,
    params: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """Run one catalogue entry via the registered profiler backend."""
    if df is None:
        return {"summary": {"skipped": True, "reason": "no dataframe"}, "capability": cap.id}

    if cap.id == "native.relationships":
        return _native_relationships(df, params)

    if cap.id == "ydata.correlations":
        return _ydata_correlations(df, params)

    if cap.id == "ydata.profile_report":
        return _ydata_profile_report(df, params, run_dir=run_dir, table=table, run_id=run_id)

    if cap.id == "ge.value_set_expectations":
        return _ge_value_set_candidates(df, params)

    return {
        "summary": {"note": f"capability {cap.id} registered; backend hook pending"},
        "capability": cap.id,
        "engine": cap.engine,
        "params": params,
    }


def _native_relationships(df: Any, params: dict[str, Any]) -> dict[str, Any]:
    from redibis.profiling.relationships import profile_relationships

    rel = profile_relationships(
        df,
        engine="auto",
        methods=tuple(params.get("methods") or ("pearson", "spearman")),
        redundancy_threshold=float(params.get("redundancy_threshold") or 0.95),
        near_constant_threshold=float(params.get("near_constant_threshold") or 0.99),
    )
    return {
        "summary": {
            "row_count": rel.get("row_count"),
            "duplicate_rows": rel.get("duplicate_rows"),
            "constant_columns": len(rel.get("constant_columns") or []),
            "near_constant_columns": len(rel.get("near_constant_columns") or []),
            "redundant_pairs": len(rel.get("redundant_pairs") or []),
        },
        "relationships": rel,
    }


def _ydata_correlations(df: Any, params: dict[str, Any]) -> dict[str, Any]:
    methods = params.get("methods") or ["pearson"]
    if _ydata_available():
        try:
            from redibis.profiling import ydata_report as yr

            hints = yr.report_type_hints(df, minimal=True)
            rep = yr.profile_report(df, minimal=True, correlations={"calculate": True})
            desc = rep.get_description()
            corr = getattr(desc, "correlations", None) or (
                desc.get("correlations") if isinstance(desc, dict) else {}
            )
            matrix: dict[str, Any] = {}
            if corr:
                matrix["ydata"] = corr if isinstance(corr, dict) else str(corr)
            return {
                "summary": {
                    "methods": methods,
                    "columns": int(df.select_dtypes(include="number").shape[1]),
                    "source": "ydata",
                    "type_hints": len(hints),
                },
                "matrix": matrix,
            }
        except Exception as exc:
            return _pandas_correlations(df, methods, fallback_reason=str(exc))
    return _pandas_correlations(df, methods, fallback_reason="ydata not installed")


def _pandas_correlations(
    df: Any,
    methods: list[str],
    *,
    fallback_reason: str = "",
) -> dict[str, Any]:
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] < 2:
        return {
            "summary": {
                "columns": int(numeric.shape[1]),
                "methods": methods,
                "source": "pandas",
                "fallback": fallback_reason or None,
            },
            "matrix": {},
        }
    matrix: dict[str, Any] = {}
    for method in methods:
        if method == "pearson":
            matrix["pearson"] = numeric.corr(method="pearson").round(4).to_dict()
        elif method == "spearman":
            matrix["spearman"] = numeric.corr(method="spearman").round(4).to_dict()
    return {
        "summary": {
            "methods": methods,
            "columns": int(numeric.shape[1]),
            "source": "pandas",
            "fallback": fallback_reason or None,
        },
        "matrix": matrix,
    }


def _ydata_profile_report(
    df: Any,
    params: dict[str, Any],
    *,
    run_dir: Path,
    table: str,
    run_id: str,
) -> dict[str, Any]:
    from redibis.profiling import ydata_report as yr

    minimal = bool(params.get("minimal", True))
    html_path = run_dir / "ydata_report.html"
    summary = yr.save_html_summary(
        df,
        str(html_path),
        title=str(params.get("title") or f"{table} — YData profile"),
        minimal=minimal,
    )
    alerts = summary.get("alerts") or []
    chat_lines = [
        f"YData profile for {table}: "
        f"{summary.get('row_count', '?')} rows, {summary.get('column_count', '?')} columns.",
    ]
    if alerts:
        chat_lines.append("Top alerts: " + "; ".join(alerts[:5]))
    chat_lines.append(f"Full report: {html_path.name}")
    return {
        "html_path": str(html_path),
        "summary": {
            "row_count": summary.get("row_count"),
            "column_count": summary.get("column_count"),
            "alert_count": len(alerts),
            "top_alerts": alerts[:5],
            "type_hints": summary.get("type_hints"),
        },
        "chat_summary": " ".join(chat_lines),
    }


def _ge_value_set_candidates(df: Any, params: dict[str, Any]) -> dict[str, Any]:
    max_unique = int(params.get("max_unique") or 50)
    candidates: list[dict[str, Any]] = []
    for col in df.columns:
        nunique = int(df[col].nunique(dropna=True))
        if 2 <= nunique <= max_unique:
            values = df[col].dropna().unique().tolist()[:max_unique]
            candidates.append({"column": col, "unique": nunique, "sample_values": values[:10]})
    return {
        "summary": {"candidates": len(candidates), "max_unique": max_unique},
        "expectations": candidates,
    }
