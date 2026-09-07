"""Deep Scan — multi-producer evidence fan-out and LLM-ready synthesis bundle."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from redibis.agents.deep_profile import (
    CapabilityParam,
    ProfilingCapability,
    _ge_value_set_candidates,
    _native_relationships,
    load_catalog as _load_yaml_catalog,
)

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "deep_scan.yaml"


@dataclass
class EvidenceArtifact:
    producer: str
    kind: str
    table: str
    run_id: str
    engine: str
    columns: dict[str, Any]
    summary: dict[str, Any]
    span_id: str = ""

    def write(self, run_dir: Path) -> tuple[Path, Path]:
        evidence_dir = Path(run_dir) / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        safe_table = self.table.replace(".", "_")
        safe_prod = self.producer.replace(".", "_")
        json_path = evidence_dir / f"{safe_prod}.{safe_table}.json"
        md_path = json_path.with_suffix(".md")
        payload = {
            "producer": self.producer,
            "kind": self.kind,
            "table": self.table,
            "run_id": self.run_id,
            "engine": self.engine,
            "columns": self.columns,
            "summary": self.summary,
            "span_id": self.span_id,
        }
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._render_md(), encoding="utf-8")
        return json_path, md_path

    def _render_md(self) -> str:
        lines = [
            f"# Deep Scan evidence — `{self.producer}`",
            "",
            f"- **Table:** `{self.table}`",
            f"- **Run:** `{self.run_id}`",
            f"- **Engine:** `{self.engine}`",
            f"- **Kind:** `{self.kind}`",
            "",
            "## Summary",
            "",
        ]
        for key, val in sorted(self.summary.items()):
            lines.append(f"- **{key}:** {val}")
        lines.extend(["", "## Per-column signals", ""])
        if not self.columns:
            lines.append("_No column-level signals._")
        else:
            for col, sig in sorted(self.columns.items()):
                lines.append(f"### `{col}`")
                if isinstance(sig, dict):
                    for k, v in sig.items():
                        lines.append(f"- {k}: {v}")
                else:
                    lines.append(f"- {sig}")
                lines.append("")
        return "\n".join(lines) + "\n"

    def ref(self) -> dict[str, Any]:
        safe_table = self.table.replace(".", "_")
        safe_prod = self.producer.replace(".", "_")
        return {
            "producer": self.producer,
            "kind": self.kind,
            "engine": self.engine,
            "json": f"evidence/{safe_prod}.{safe_table}.json",
            "markdown": f"evidence/{safe_prod}.{safe_table}.md",
            "column_count": len(self.columns),
            "summary": self.summary,
            "span_id": self.span_id,
        }


def load_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or _CATALOG_PATH
    if not p.is_file():
        return {"version": "1.0", "producers": [], "deny": [], "default_producers": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def list_producers(path: Optional[Path] = None) -> list[ProfilingCapability]:
    raw = load_catalog(path)
    caps: list[ProfilingCapability] = []
    for item in raw.get("producers") or []:
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
            id=str(item["id"]),
            engine=str(item.get("engine") or ""),
            label=str(item.get("label") or item["id"]),
            description=str(item.get("description") or ""),
            params=tuple(param_specs),
            output=str(item.get("output") or "evidence"),
            cost=str(item.get("cost") or "low"),
        ))
    return caps


def get_producer(producer_id: str, path: Optional[Path] = None) -> ProfilingCapability:
    for cap in list_producers(path):
        if cap.id == producer_id:
            return cap
    raise KeyError(f"unknown deep-scan producer: {producer_id!r}")


def _deny_match(producer_id: str, deny: list[str]) -> bool:
    for pattern in deny:
        if fnmatch.fnmatch(producer_id, pattern):
            return True
    return False


def reconcile_signals(columns_signals: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """
    Per-column reconciliation — agreement matrix → false-positive risk.

    * low: ≥2 independent PII engines agree
    * high: single engine fires but profiler suggests surrogate-key / high-cardinality ID
    """
    out: dict[str, dict[str, Any]] = {}
    for col, signals in (columns_signals or {}).items():
        pii_engines: list[str] = []
        profiler_id_like = False
        profiler_numeric_seq = False

        for sig in signals:
            producer = str(sig.get("producer") or "")
            verdict = sig.get("verdict")
            is_pii = verdict in ("pii", "detected", True) or sig.get("detected") is True
            if producer in (
                "rule.regex_catalog",
                "ner.presidio",
                "ner.gliner",
                "equation.decide",
            ) and is_pii:
                pii_engines.append(producer)

            if producer.startswith("profile"):
                card = sig.get("cardinality_ratio")
                nunique = sig.get("nunique")
                n_rows = sig.get("row_count") or sig.get("n_rows")
                if card is not None and float(card) >= 0.95:
                    profiler_id_like = True
                if (
                    nunique is not None
                    and n_rows is not None
                    and int(n_rows) > 0
                    and int(nunique) == int(n_rows)
                    and sig.get("dtype", "").startswith(("int", "Int"))
                ):
                    profiler_numeric_seq = True

        unique_engines = sorted(set(pii_engines))
        if len(unique_engines) >= 2:
            risk = "low"
            verdict = "pii"
        elif len(unique_engines) == 1 and (profiler_id_like or profiler_numeric_seq):
            risk = "high"
            verdict = "not_pii"
        elif len(unique_engines) == 1:
            risk = "medium"
            verdict = "pii"
        else:
            risk = "low"
            verdict = "not_pii"

        out[col] = {
            "verdict": verdict,
            "false_positive_risk": risk,
            "engines_agreeing": unique_engines,
            "profiler_contradicts": profiler_id_like or profiler_numeric_seq,
        }
    return out


def build_synthesis_bundle(
    table: str,
    run_id: str,
    evidence: list[EvidenceArtifact],
    run_dir: Path,
    *,
    columns_signals: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> Path:
    """Write ``deep_scan_bundle/`` with manifest, markdown brief, and columns JSONL."""
    bundle_dir = Path(run_dir) / "deep_scan_bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    if columns_signals is None:
        columns_signals = {}
        for art in evidence:
            for col, sig in (art.columns or {}).items():
                row = dict(sig) if isinstance(sig, dict) else {"signal": sig}
                row["producer"] = art.producer
                columns_signals.setdefault(col, []).append(row)

    agreement: dict[str, list[str]] = {}
    for col, sigs in columns_signals.items():
        engines = []
        for sig in sigs:
            if sig.get("verdict") in ("pii", "detected", True) or sig.get("detected"):
                engines.append(str(sig.get("producer") or ""))
        agreement[col] = sorted(set(e for e in engines if e))

    reconciled = reconcile_signals(columns_signals)
    refs = [art.ref() for art in evidence]

    manifest = {
        "table": table,
        "run_id": run_id,
        "evidence": refs,
        "agreement_matrix": agreement,
        "reconciled": reconciled,
    }
    (bundle_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    lines = [
        f"# Contract synthesis brief — `{table}`",
        "",
        f"Run `{run_id}` — synthesise an ODCS contract from multi-engine evidence.",
        "Conflicts (high false-positive risk) are highlighted.",
        "",
    ]
    for col in sorted(columns_signals.keys()):
        rec = reconciled.get(col, {})
        risk = rec.get("false_positive_risk", "unknown")
        lines.append(f"## Column `{col}` (risk: **{risk}**)")
        for sig in columns_signals[col]:
            prod = sig.get("producer", "?")
            verdict = sig.get("verdict", sig.get("detected", sig.get("entity_type", "")))
            rule = sig.get("rule", sig.get("pattern", ""))
            lines.append(f"- **{prod}:** verdict={verdict}; rule={rule}")
        if risk == "high":
            lines.append("- **CONFLICT:** profiler contradicts naive PII rule — review before marking PII.")
        lines.append("")

    lines.extend([
        "## Ask",
        "",
        "Produce ODCS v3 property entries per column using the evidence above.",
        "Prefer columns with low false-positive risk; escalate high-risk columns for human review.",
        "Do not embed raw cell values — use classifications, tags, and pattern ids only.",
        "",
    ])
    (bundle_dir / "contract_synthesis.md").write_text("\n".join(lines), encoding="utf-8")

    jsonl_path = bundle_dir / "columns.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for col in sorted(columns_signals.keys()):
            row = {
                "column": col,
                "signals": columns_signals[col],
                "reconciled": reconciled.get(col, {}),
            }
            fh.write(json.dumps(row, default=str) + "\n")

    return bundle_dir


_ProducerFn = Callable[..., EvidenceArtifact]


def _dispatch_producer(
    cap: ProfilingCapability,
    *,
    table: str,
    run_id: str,
    df: Any,
    config: Any,
    params: dict[str, Any],
    span_id: str = "",
    run_dir: Path | None = None,
) -> EvidenceArtifact:
    dispatch: dict[str, _ProducerFn] = {
        "rule.regex_catalog": _produce_regex_catalog,
        "ner.presidio": _produce_ner_presidio,
        "ner.gliner": _produce_ner_gliner,
        "equation.decide": _produce_equation_decide,
        "profile.ge": _produce_profile_ge,
        "profile.om": _produce_profile_om,
        "profile.duckdb": _produce_profile_duckdb,
        "profile.relationships": _produce_profile_relationships,
        "profile.ydata": _produce_profile_ydata,
        "telecom.evidence": _produce_telecom_evidence,
        "quality.candidates": _produce_quality_candidates,
    }
    fn = dispatch.get(cap.id)
    if fn is None:
        return EvidenceArtifact(
            producer=cap.id,
            kind=cap.output,
            table=table,
            run_id=run_id,
            engine=cap.engine,
            columns={},
            summary={"note": f"producer {cap.id} registered; hook pending"},
            span_id=span_id,
        )
    return fn(
        table=table,
        run_id=run_id,
        df=df,
        config=config,
        params=params,
        span_id=span_id,
        cap=cap,
        run_dir=run_dir,
    )


def _produce_regex_catalog(**kw: Any) -> EvidenceArtifact:
    import pandas as pd
    from redibis.pii.detector import detect_pii

    df = kw["df"]
    table, run_id, span_id = kw["table"], kw["run_id"], kw["span_id"]
    cap = kw["cap"]
    columns: dict[str, Any] = {}
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )
    detections = detect_pii(df, columns=df.columns.tolist(), engines="regex")
    for d in detections:
        if d.presidio_score is None and not d.regex_hits:
            continue
        columns[d.column] = {
            "verdict": "pii" if (d.presidio_score or 0) >= 0.5 else "not_pii",
            "score": d.presidio_score,
            "pattern": d.presidio_pattern or "",
            "match_rate": d.presidio_match_rate,
            "entity_type": d.entity_type or "",
            "regex_hits": d.regex_hits or [],
        }
    return EvidenceArtifact(
        producer=cap.id, kind=cap.output, table=table, run_id=run_id,
        engine=cap.engine, columns=columns,
        summary={"columns_scored": len(columns)}, span_id=span_id,
    )


def _produce_telecom_evidence(**kw: Any) -> EvidenceArtifact:
    import pandas as pd
    from redibis.config import RedibisConfig
    from redibis.pii.telecom_evidence import build_telecom_evidence

    df, table, run_id, span_id, cap = kw["df"], kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    params = kw.get("params") or {}
    config = kw.get("config") or RedibisConfig.default()
    sample_policy = str(params.get("sample_policy") or "raw")
    sample_n = int(params.get("sample_n") or 10)
    columns: dict[str, Any] = {}
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )
    for col in df.columns:
        series = df[col].dropna()
        if len(series) > 100:
            series = series.sample(n=100, random_state=42)
        values = series.tolist()
        if not values:
            continue
        columns[str(col)] = build_telecom_evidence(
            str(col),
            values,
            sample_policy=sample_policy,
            sample_n=sample_n,
            prefixes=config.pii.msisdn_prefixes,
            use_phonenumbers=config.pii.use_phonenumbers,
        )
    return EvidenceArtifact(
        producer=cap.id, kind=cap.output, table=table, run_id=run_id,
        engine=cap.engine, columns=columns,
        summary={
            "columns_profiled": len(columns),
            "sample_policy": sample_policy,
        },
        span_id=span_id,
    )


def _produce_ner_presidio(**kw: Any) -> EvidenceArtifact:
    return _produce_regex_catalog(**kw)


def _produce_ner_gliner(**kw: Any) -> EvidenceArtifact:
    import pandas as pd
    from redibis.config import RedibisConfig
    from redibis.models import canonical_entity
    from redibis.pii.ner_backend import model_slug
    from redibis.pii.ner_ensemble import NEREnsemble

    df, table, run_id, span_id, cap = kw["df"], kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    config = kw.get("config") or RedibisConfig.default()
    run_dir = Path(kw.get("run_dir") or ".")
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )

    try:
        ensemble = NEREnsemble.from_config(config.pii)
    except Exception as exc:
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={},
            summary={"skipped": True, "reason": str(exc)}, span_id=span_id,
        )

    if not ensemble.backends:
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={},
            summary={"skipped": True, "reason": "no NER backends configured"}, span_id=span_id,
        )

    label_groups = list(config.pii.ner.label_groups or []) or None
    per_model: dict[str, dict[str, Any]] = {b.name: {} for b in ensemble.backends}
    for entry in ensemble.unavailable:
        per_model[str(entry.get("model") or "")] = {}
    agreement: dict[str, Any] = {}

    for col in df.columns:
        series = df[col].dropna().astype(str)
        if len(series) > 100:
            series = series.sample(n=100, random_state=42)
        values = series.tolist()
        if not values:
            continue
        passes = ensemble.run_column(values, str(col), label_groups=label_groups)
        entities_by_model: dict[str, set[str]] = {}
        col_hits: list[dict] = []
        for p in passes:
            if p.status != "ok":
                per_model.setdefault(p.model, {})[col] = {
                    "status": "unavailable",
                    "reason": p.reason,
                    "labels_requested": p.labels,
                }
                continue
            if p.report is None:
                continue
            hits = [
                {"label": h.label, "score": h.score, "match_rate": h.match_rate, "labels": p.labels}
                for h in p.report.hits
            ]
            per_model.setdefault(p.model, {})[col] = {"hits": hits, "labels_requested": p.labels}
            col_hits.extend({"model": p.model, **h} for h in hits)
            ents = set()
            for h in p.report.hits:
                ent = canonical_entity(h.label.upper().replace(" ", "_"))
                if ent:
                    ents.add(ent)
            if ents:
                entities_by_model[p.model] = ents
        all_ents: dict[str, list[str]] = {}
        for model, ents in entities_by_model.items():
            for e in ents:
                all_ents.setdefault(e, []).append(model)
        agreeing = {e: models for e, models in all_ents.items() if len(models) > 1}
        agreement[col] = {
            "models_run": len(entities_by_model),
            "entities": sorted(all_ents.keys()),
            "agreeing_entities": agreeing,
            "hit_count": len(col_hits),
        }

    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    safe_table = table.replace(".", "_")
    models_written: list[str] = []
    for model_name, cols in per_model.items():
        safe_model = model_slug(model_name)
        payload = {
            "producer": f"ner.{safe_model}",
            "kind": cap.output,
            "table": table,
            "run_id": run_id,
            "engine": cap.engine,
            "model": model_name,
            "columns": cols,
            "summary": {"columns_scored": len(cols)},
        }
        json_path = evidence_dir / f"ner.{safe_model}.{safe_table}.json"
        md_path = json_path.with_suffix(".md")
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md_lines = [
            f"# NER evidence — `{model_name}`",
            "",
            f"- **Table:** `{table}`",
            f"- **Columns scored:** {len(cols)}",
            "",
        ]
        for col_name, sig in sorted(cols.items()):
            md_lines.append(f"## `{col_name}`")
            for hit in sig.get("hits") or []:
                md_lines.append(
                    f"- {hit.get('label')}: score={hit.get('score')}, "
                    f"match_rate={hit.get('match_rate')}"
                )
            md_lines.append("")
        md_path.write_text("\n".join(md_lines), encoding="utf-8")
        models_written.append(model_name)

    return EvidenceArtifact(
        producer=cap.id, kind="ner_agreement", table=table, run_id=run_id,
        engine=cap.engine, columns=agreement,
        summary={
            "models": models_written,
            "model_count": len(models_written),
            "label_groups": label_groups or [],
            "columns_scored": len(agreement),
        },
        span_id=span_id,
    )


def _produce_equation_decide(**kw: Any) -> EvidenceArtifact:
    import pandas as pd
    from redibis.config import RedibisConfig
    from redibis.services import pipeline

    df, table, run_id, span_id, cap = kw["df"], kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    params = kw.get("params") or {}
    config = kw.get("config") or RedibisConfig.default()
    columns: dict[str, Any] = {}
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )
    equation = params.get("equation_mode") or config.pii.equation_mode
    thresholds = config.pii.thresholds
    detections = pipeline.run_pii_detection(
        df,
        engines=config.pii.engines,
        equation_mode=equation,
        thresholds=thresholds,
        ner_config=config.pii.ner,
        gliner_config=config.pii.gliner,
        ner_always_run=config.pii.ner_always_run(),
        pii_config=config.pii,
    )
    for d in detections:
        columns[d.column] = {
            "verdict": "pii" if d.detected else "not_pii",
            "detected": d.detected,
            "confidence": d.confidence,
            "equation": d.equation_used,
            "entity_type": d.entity_type or "",
            "presidio_score": d.presidio_score,
            "gliner_score": d.gliner_score,
            "phone_score": d.phone_score,
            "msisdn_valid_rate": d.msisdn_valid_rate,
        }
    return EvidenceArtifact(
        producer=cap.id, kind="pii_verdict", table=table, run_id=run_id,
        engine=cap.engine, columns=columns,
        summary={"flagged": sum(1 for c in columns.values() if c.get("detected"))},
        span_id=span_id,
    )


def _produce_profile_ge(**kw: Any) -> EvidenceArtifact:
    return _produce_profile_engine("great_expectations", **kw)


def _produce_profile_om(**kw: Any) -> EvidenceArtifact:
    return _produce_profile_engine("open_metadata", **kw)


def _produce_profile_duckdb(**kw: Any) -> EvidenceArtifact:
    return _produce_profile_engine("duckdb", **kw)


def _produce_profile_engine(engine: str, **kw: Any) -> EvidenceArtifact:
    import pandas as pd
    from redibis.config import ProfilingConfig, RedibisConfig
    from redibis.profiling import get_profiler

    df, table, run_id, span_id, cap = kw["df"], kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    columns: dict[str, Any] = {}
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )
    cfg = kw.get("config") or RedibisConfig.default()
    prof_cfg = ProfilingConfig(engine=engine, triage_threshold=cfg.profiling.triage_threshold)
    try:
        profiler = get_profiler(prof_cfg)
        result = profiler.profile(df, dataset_name=table.split(".")[-1])
    except Exception as exc:
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={},
            summary={"error": str(exc)}, span_id=span_id,
        )
    n_rows = len(df)
    for p in result.column_profiles:
        nunique = int(df[p.column].nunique(dropna=True)) if p.column in df.columns else 0
        columns[p.column] = {
            "cardinality_ratio": p.cardinality_ratio,
            "nunique": nunique,
            "n_rows": n_rows,
            "row_count": n_rows,
            "dtype": p.dtype,
            "logical_type": p.logical_type,
            "triage_score": p.triage_score,
        }
    return EvidenceArtifact(
        producer=cap.id, kind=cap.output, table=table, run_id=run_id,
        engine=cap.engine, columns=columns,
        summary={"columns": len(columns), "profiler": engine}, span_id=span_id,
    )


def _produce_profile_relationships(**kw: Any) -> EvidenceArtifact:
    import pandas as pd

    df, table, run_id, span_id, cap = kw["df"], kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    params = kw.get("params") or {}
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )
    rel = _native_relationships(df, params)
    columns = {
        c: {"flag": "constant"}
        for c in (rel.get("relationships") or {}).get("constant_columns") or []
    }
    return EvidenceArtifact(
        producer=cap.id, kind=cap.output, table=table, run_id=run_id,
        engine=cap.engine, columns=columns, summary=rel.get("summary", {}), span_id=span_id,
    )


def _produce_profile_ydata(**kw: Any) -> EvidenceArtifact:
    from redibis.profiling.ydata_report import ydata_available

    table, run_id, span_id, cap = kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    if not ydata_available():
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={},
            summary={"skipped": True, "reason": "ydata not installed"}, span_id=span_id,
        )
    return _produce_profile_engine("great_expectations", **kw)


def _produce_quality_candidates(**kw: Any) -> EvidenceArtifact:
    import pandas as pd

    df, table, run_id, span_id, cap = kw["df"], kw["table"], kw["run_id"], kw["span_id"], kw["cap"]
    params = kw.get("params") or {}
    if df is None or not isinstance(df, pd.DataFrame):
        return EvidenceArtifact(
            producer=cap.id, kind=cap.output, table=table, run_id=run_id,
            engine=cap.engine, columns={}, summary={"skipped": True}, span_id=span_id,
        )
    raw = _ge_value_set_candidates(df, params)
    columns = {}
    for row in raw.get("expectations") or []:
        col = row.get("column")
        if not col:
            continue
        columns[col] = {
            "verdict": "candidate",
            "nunique": row.get("unique"),
            "rule": "value_set_candidate",
        }
    return EvidenceArtifact(
        producer=cap.id, kind=cap.output, table=table, run_id=run_id,
        engine=cap.engine, columns=columns, summary=raw.get("summary", {}), span_id=span_id,
    )


def run_deep_scan(
    table: str,
    run_id: str,
    df: Any,
    *,
    producers: Optional[list[str]] = None,
    params_by_id: Optional[dict[str, dict[str, Any]]] = None,
    run_dir: Path,
    config: Any = None,
    build_bundle: bool = True,
) -> dict[str, Any]:
    """Fan out whitelisted producers; each writes md+json evidence artifacts."""
    from redibis.config import RedibisConfig
    from redibis.obs import persist_run_log
    from redibis.telemetry.otel import run_context

    cfg = config if config is not None else RedibisConfig.default()
    catalog = load_catalog()
    deny = list(catalog.get("deny") or [])
    producer_ids = producers or list(catalog.get("default_producers") or [])
    params_by_id = params_by_id or {}
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    evidence: list[EvidenceArtifact] = []
    errors: list[str] = []
    columns_signals: dict[str, list[dict[str, Any]]] = {}
    telemetry: list[dict[str, Any]] = []

    from contextlib import nullcontext

    log_ctx = (
        persist_run_log(run_dir, table, run_id)
        if cfg.observability.persist_run_log
        else nullcontext()
    )

    with log_ctx, run_context(run_id) as tel:
        for prod_id in producer_ids:
            if _deny_match(prod_id, deny):
                errors.append(f"denied producer {prod_id!r}")
                continue
            try:
                cap = get_producer(prod_id)
            except KeyError as exc:
                errors.append(str(exc))
                continue
            param_errors = cap.validate_params(params_by_id.get(prod_id) or {})
            if param_errors:
                errors.extend([f"{prod_id}: {e}" for e in param_errors])
                continue

            try:
                with tel.span(
                    "deep_scan.producer",
                    producer=prod_id,
                    table=table,
                    engine=cap.engine,
                ) as span_rec:
                    artifact = _dispatch_producer(
                        cap,
                        table=table,
                        run_id=run_id,
                        df=df,
                        config=cfg,
                        params=params_by_id.get(prod_id) or {},
                        span_id=span_rec.span_id,
                        run_dir=run_dir,
                    )
                artifact.write(run_dir)
                evidence.append(artifact)
                for col, sig in (artifact.columns or {}).items():
                    row = dict(sig) if isinstance(sig, dict) else {"signal": sig}
                    row["producer"] = prod_id
                    columns_signals.setdefault(col, []).append(row)
            except Exception as exc:
                errors.append(f"{prod_id}: {exc}")

        telemetry = tel.export()

    bundle_path = None
    if build_bundle and evidence:
        bundle_path = str(
            build_synthesis_bundle(
                table, run_id, evidence, run_dir, columns_signals=columns_signals
            )
        )

    manifest_path = run_dir / "deep_scan_manifest.json"
    manifest = {
        "table": table,
        "run_id": run_id,
        "evidence": [e.ref() for e in evidence],
        "errors": errors,
        "bundle": bundle_path,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    return {
        "table": table,
        "run_id": run_id,
        "evidence": [e.ref() for e in evidence],
        "errors": errors,
        "manifest": str(manifest_path),
        "bundle": bundle_path,
        "telemetry": telemetry,
    }
