"""Read-only structured evaluation against engine, contract, or LLM proposals."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from redibis.evaluation.schema import (
    TableEvalError,
    structural_fingerprint,
    validate_table_dataset,
)
from redibis.evaluation.scoring import evaluate_table
from redibis.evaluation.targets import from_contract, from_engine_detections, from_llm_proposals

ProgressCb = Callable[[str, dict[str, Any]], None]


class TableEvalCancelled(Exception):
    """Raised when a caller stops an in-flight table evaluation."""


def _load_df(path: str):
    import pandas as pd

    if str(path).endswith((".parquet", ".pq")):
        return pd.read_parquet(path)
    return pd.read_csv(path)


def scaffold_from_frame(
    df,
    *,
    table_name: str = "",
    suggestions: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    from redibis.pii.eval.span_metrics import current_redibis_version

    names = [str(c) for c in df.columns]
    dtypes = [str(df[c].dtype) for c in df.columns]
    suggestions = suggestions or {}
    columns = []
    for name, dtype in zip(names, dtypes):
        hint = suggestions.get(name) or {}
        columns.append({
            "name": name,
            "is_pii": False,
            "entity_type": "",
            "logicalType": str(hint.get("logicalType") or dtype),
            "privacy_classification": "",
            "businessName": "",
            "description": "",
            "business_definition": "",
            "tags": [],
            "suggestion": {
                "is_pii": bool(hint.get("is_pii", False)),
                "entity_type": str(hint.get("entity_type") or "").upper(),
            },
        })
    return {
        "kind": "redibis.table_column_eval_dataset",
        "schema_version": "1.0",
        "redibis_version": current_redibis_version(),
        "table_name": table_name,
        "fingerprint": structural_fingerprint(names, dtypes),
        "residency": "portable",
        "columns": columns,
    }


def _engine_actuals(df, *, engines: str, equation: str, redibis_config: Any) -> list[dict[str, Any]]:
    from redibis.pii.detector import detect_pii
    from redibis.pii.equations import decide_pii
    from redibis.pii.thresholds import Thresholds

    detections = detect_pii(df, engines=engines)
    thresholds = Thresholds()
    verdicts = [decide_pii(d, equation, thresholds) for d in detections]
    return from_engine_detections(verdicts, source="engine")


def _llm_actuals(df, *, engines: str, equation: str, redibis_config: Any) -> list[dict[str, Any]]:
    from redibis.pii.detector import detect_pii
    from redibis.pii.equations import decide_pii
    from redibis.pii.llm_refiner import refine_detections
    from redibis.pii.thresholds import Thresholds

    llm_cfg = getattr(getattr(redibis_config, "pii", None), "llm", None)
    if not llm_cfg or not bool(getattr(llm_cfg, "enabled", False)):
        raise TableEvalError("LLM proposal target requires pii.llm.enabled")
    detections = detect_pii(df, engines=engines)
    thresholds = Thresholds()
    refined = refine_detections(detections, thresholds, redibis_config=redibis_config)
    verdicts = [decide_pii(d, equation, thresholds) for d in refined]
    return from_llm_proposals(verdicts)


def _semantic_judge(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    redibis_config: Any,
) -> dict[str, bool]:
    """Optional presence-preserving judge for definition text. Never sends raw values."""
    from redibis.enrich.capability_routing import get_provider_for_role
    from redibis.telemetry.model_gateway import guarded_model_call

    fields = {}
    try:
        provider, binding = get_provider_for_role(
            "contract.enrichment",
            agents_cfg=getattr(redibis_config, "agents", None),
        )
    except Exception:
        return fields
    for field in ("description", "business_definition"):
        exp = str(expected.get(field) or "").strip()
        act = str(actual.get(field) or "").strip()
        if not exp:
            continue
        if exp.casefold() == act.casefold():
            fields[field] = True
            continue
        system = "You compare business definitions. Reply with JSON {\"match\": true|false}."
        user = f"expected: {exp[:400]}\nactual: {act[:400]}"
        try:
            raw, _ = guarded_model_call(
                lambda: provider.complete(system, user, json_mode=True),
                model_id=binding.model or getattr(provider, "name", "enrichment"),
                provider=provider,
                user_prompt=user,
                system_prompt=system,
                redibis_config=redibis_config,
                model_role="contract.enrichment",
            )
            text = str(raw or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            parsed = json.loads(text)
            fields[field] = isinstance(parsed, dict) and parsed.get("match") is True
        except Exception:
            fields[field] = False
    return fields


def run_table_evaluation(
    dataset: Mapping[str, Any],
    *,
    df=None,
    data_path: str = "",
    contract: Optional[Mapping[str, Any]] = None,
    target: str = "engine",
    engines: str = "both",
    equation: str = "independent",
    redibis_config: Any = None,
    semantic: bool = False,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> dict[str, Any]:
    if target not in ("engine", "contract", "llm"):
        raise TableEvalError(f"unknown target {target!r}")
    if target != "contract":
        if df is None:
            if not data_path:
                raise TableEvalError("data_path or dataframe is required")
            df = _load_df(data_path)
    if cancel_event is not None and cancel_event.is_set():
        raise TableEvalCancelled("evaluation cancelled")
    if df is not None:
        names = [str(c) for c in df.columns]
        fingerprint = structural_fingerprint(names, [str(df[c].dtype) for c in df.columns])
    else:
        names = [str(c.get("name") or "") for c in (dataset.get("columns") or [])]
        fingerprint = str(dataset.get("fingerprint") or "")
    if progress_cb:
        progress_cb("validate", {"percent": 5})
    normalized = validate_table_dataset(
        dataset, table_columns=names if df is not None else None, table_fingerprint=fingerprint
    )
    if normalized["stale_fingerprint"]:
        raise TableEvalError("dataset fingerprint is stale for the current table")
    if normalized["missing_columns"] or normalized["extra_columns"]:
        raise TableEvalError(
            "dataset columns do not match the current table "
            f"(missing={normalized['missing_columns']}, extra={normalized['extra_columns']})"
        )
    if progress_cb:
        progress_cb("collect", {"target": target, "percent": 20})
    if target == "contract":
        actuals = from_contract(contract)
    elif target == "llm":
        actuals = _llm_actuals(df, engines=engines, equation=equation, redibis_config=redibis_config)
    else:
        actuals = _engine_actuals(df, engines=engines, equation=equation, redibis_config=redibis_config)
    if cancel_event is not None and cancel_event.is_set():
        raise TableEvalCancelled("evaluation cancelled")
    semantic_by_column = {}
    if semantic:
        by_name = {row["name"]: row for row in actuals}
        for expected in normalized["columns"]:
            if cancel_event is not None and cancel_event.is_set():
                raise TableEvalCancelled("evaluation cancelled")
            semantic_by_column[expected["name"]] = _semantic_judge(
                expected, by_name.get(expected["name"]) or {}, redibis_config=redibis_config
            )
    if progress_cb:
        progress_cb("score", {"percent": 90})
    report = evaluate_table(
        normalized,
        actuals,
        target=target,
        table_columns=names,
        table_fingerprint=fingerprint,
        semantic_by_column=semantic_by_column,
    )
    report["provenance"] = {
        "engines": engines if target in ("engine", "llm") else "",
        "equation": equation if target in ("engine", "llm") else "",
        "llm_proposal_count": sum(bool(row.get("is_proposal")) for row in actuals),
    }
    if progress_cb:
        progress_cb("done", {"percent": 100})
    return report
