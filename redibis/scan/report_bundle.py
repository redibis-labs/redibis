"""Flush scan report artifacts to disk or RunOutputWriter."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from redibis.pii.equations import build_column_report
from redibis.pii.pii_regex_review import export_pii_regex_review
from redibis.pii.report_writer import export_pii_detection_report
from redibis.scan.types import ScanRunResult
from redibis.scan.config import ScanConfig
from redibis.store.run_output_writer import RunOutputWriter
from redibis.store.storage_backend import _sanitize_for_yaml


class ReportBundle:
    """Collects report payloads from a ``ScanRunResult`` and writes them on flush."""

    def __init__(self, result: ScanRunResult, config: ScanConfig):
        self.result = result
        self.config = config

    @classmethod
    def from_result(cls, result: ScanRunResult, config: ScanConfig) -> ReportBundle:
        return cls(result, config)

    def flush(
        self,
        run_dir: Path,
        *,
        run_writer: Optional[RunOutputWriter] = None,
        df: Optional[pd.DataFrame] = None,
    ) -> dict[str, str]:
        run_dir.mkdir(parents=True, exist_ok=True)
        artifacts: dict[str, str] = {}
        result = self.result
        config = self.config
        errors: list[dict[str, Any]] = []

        if getattr(config, "evidence_bundle_enabled", True):
            try:
                eb_path = _write_evidence_bundle(run_dir, result, config, df)
                if eb_path is not None:
                    artifacts["evidence_bundle"] = str(eb_path)
            except Exception as exc:
                errors.append({"phase": "profile", "error": f"{type(exc).__name__}: {exc}"})

        if result.profile is not None:
            profile = result.profile
            ir = run_dir / "interactive_review.html"
            profile.export_interactive_review(
                output_filename=str(ir), open_browser=False
            )
            artifacts["interactive_review"] = str(ir)

            tr = run_dir / "triage_report.html"
            profile.export_triage_report(
                output_filename=str(tr), open_browser=False
            )
            artifacts["triage_report"] = str(tr)

        if config.run_pii:
            pr = run_dir / "pii_regex_review.html"
            export_pii_regex_review(
                overrides=config.pii_regex_overrides,
                output_filename=str(pr),
                open_browser=False,
            )
            artifacts["pii_regex_review"] = str(pr)

        if result.quality_gatekeeper is not None and result.quality_results is not None:
            qa = result.quality_gatekeeper
            report_data = qa._extract_report_data(result.quality_results)
            qr_json = run_dir / "quality_results.json"
            with open(qr_json, "w", encoding="utf-8") as fh:
                json.dump({
                    "table": result.table,
                    "run_id": result.run_id,
                    "success": report_data.get("success"),
                    "statistics": report_data.get("statistics"),
                    "results": report_data.get("results"),
                }, fh, indent=2, default=str)
            artifacts["quality_results"] = str(qr_json)

            qr = run_dir / f"quality-report-{result.run_id}.html"
            qa.export_quality_report(
                result.quality_results,
                output_filename=str(qr),
                report_title=f"Quality Report — {result.table}",
                open_browser=False,
            )
            artifacts["quality_report"] = str(qr)

            if config.generate_ge_docs and not qa.in_memory:
                ge_dir = run_dir / "ge_report"
                try:
                    qa.copy_data_docs_to(ge_dir)
                    artifacts["ge_report"] = str(ge_dir / "index.html")
                except Exception as exc:
                    errors.append({"phase": "quality", "error": f"ge_docs: {exc}"})

        if result.quality_contract is not None:
            qc_path = run_dir / "quality_contract.yaml"
            with open(qc_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(
                    _sanitize_for_yaml(result.quality_contract),
                    fh,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            artifacts["quality_contract"] = str(qc_path)

        if result.pii_contract is not None:
            pc_path = run_dir / "pii_contract.yaml"
            with open(pc_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(
                    result.pii_contract,
                    fh,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            artifacts["pii_contract"] = str(pc_path)

        if result.pii_detections:
            from redibis.evidence.persist import atomic_write_json

            pii_json = run_dir / "pii_detections.json"
            atomic_write_json(pii_json, [_detection_to_dict(d) for d in result.pii_detections])
            artifacts["pii_detections"] = str(pii_json)

            pii_html = run_dir / "pii_detections.html"
            export_pii_detection_report(
                detections=result.pii_detections,
                table_name=result.table,
                output_filename=str(pii_html),
            )
            artifacts["pii_detections_html"] = str(pii_html)

            pii_report = run_dir / "pii_report.json"
            with open(pii_report, "w", encoding="utf-8") as fh:
                json.dump(
                    [
                        build_column_report(
                            d, equation=config.equation_mode
                        ).to_dict()
                        for d in result.pii_detections
                    ],
                    fh,
                    indent=2,
                    default=str,
                )
            artifacts["pii_report"] = str(pii_report)

        coverage_records = _build_coverage_from_result(result, config)
        if coverage_records is not None:
            from redibis.services.pipeline import (
                SCAN_COVERAGE_ARTIFACT,
                scan_coverage_payload,
                write_scan_coverage,
            )

            payload = scan_coverage_payload(
                coverage_records,
                table=result.table,
                scan_id=result.run_id,
            )
            cov_path = run_dir / SCAN_COVERAGE_ARTIFACT
            with open(cov_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, default=str)
            artifacts["scan_coverage"] = str(cov_path)
            if run_writer is not None:
                try:
                    write_scan_coverage(
                        run_writer,
                        coverage_records,
                        table=result.table,
                        scan_id=result.run_id,
                    )
                except Exception as exc:
                    errors.append({"phase": "coverage", "error": str(exc)})

        try:
            cfg_path = _write_effective_config(run_dir, config)
            artifacts["effective_config"] = str(cfg_path)
        except Exception as exc:
            errors.append({"phase": "config", "error": str(exc)})

        try:
            variants_paths = _write_result_variants(run_dir, result, config, artifacts)
            artifacts.update(variants_paths)
        except Exception as exc:
            errors.append({"phase": "comparison", "error": str(exc)})

        shareable_key = ""
        if artifacts.get("evidence_bundle"):
            try:
                shareable_key = _store_shareable_evidence(
                    run_dir, artifacts["evidence_bundle"], run_writer,
                )
                if shareable_key:
                    artifacts["evidence_bundle_shareable"] = shareable_key
            except Exception as exc:
                errors.append({"phase": "store", "error": str(exc)})

        if run_writer is not None:
            s3_links, upload_errors = _upload_artifacts(run_writer, run_dir)
            artifacts.update(s3_links)
            errors.extend(upload_errors)

        try:
            rec = None
            try:
                from redibis.telemetry.llm_evidence import current_llm_evidence_recorder

                rec = current_llm_evidence_recorder()
            except Exception:
                rec = None
            if rec is not None:
                errors.extend(list(rec.warnings or []))
            man_path = _write_evidence_manifest(
                run_dir, result, config, artifacts, errors, shareable_key,
            )
            artifacts["evidence_manifest"] = str(man_path)
            if run_writer is not None:
                try:
                    artifacts["s3:evidence_manifest.json"] = run_writer.write_file(
                        "evidence_manifest.json", man_path,
                    )
                except Exception as exc:
                    errors.append({"phase": "manifest", "error": f"upload: {exc}"})
                    man_path = _write_evidence_manifest(
                        run_dir, result, config, artifacts, errors, shareable_key,
                    )
                    artifacts["evidence_manifest"] = str(man_path)
        except Exception as exc:
            errors.append({"phase": "manifest", "error": str(exc)})

        if artifacts.get("pii_detections_html") and "pii_detection_report" not in artifacts:
            artifacts["pii_detection_report"] = artifacts["pii_detections_html"]

        if errors:
            artifacts["evidence_errors"] = json.dumps(errors)
            result.evidence_warnings.extend(errors)
            log = __import__("logging").getLogger(__name__)
            for item in errors:
                log.warning("evidence write warning: %s", item)
        result.artifacts.update(artifacts)
        return artifacts


def _build_coverage_from_result(result: ScanRunResult, config: ScanConfig):
    """Derive scan coverage records from an in-memory scan result."""
    from redibis.services.pipeline import build_scan_coverage

    columns: list[str] = []
    if result.col_dtypes:
        columns = list(result.col_dtypes.keys())
    else:
        profiles = getattr(result.profile, "column_profiles", None) if result.profile is not None else None
        if isinstance(profiles, (list, tuple)):
            columns = [p.column for p in profiles]
        elif result.pii_detections:
            columns = [d.column for d in result.pii_detections]

    if not columns and not config.run_profile and not config.run_pii:
        return None

    return build_scan_coverage(
        scan_id=result.run_id,
        columns=columns,
        profile=result.profile,
        pii_detections=result.pii_detections,
        run_profile=bool(config.run_profile and result.profile is not None),
        run_pii=bool(config.run_pii),
        pii_column_filter=config.pii_columns,
        empty_sample=bool(result.total_rows == 0 and (config.run_profile or config.run_pii)),
        profile_error=(
            result.error
            if result.status == "failed" and config.run_profile and result.profile is None
            else None
        ),
        pii_error=(
            result.error
            if result.status == "failed" and config.run_pii and not result.pii_detections
            else None
        ),
    )


def _split_table(table: str) -> tuple[str, str]:
    if "." in table:
        db, tbl = table.split(".", 1)
        return db, tbl
    return "", table


def _detection_to_dict(d) -> dict:
    from redibis.evidence.redact import sanitize_shareable_payload

    try:
        payload = asdict(d)
    except Exception:
        payload = {
            "column": d.column,
            "detected": d.detected,
            "entity_type": d.entity_type,
            "confidence": d.confidence,
        }
    if not payload.get("engine_evidence"):
        try:
            from redibis.evidence.engines import project_engine_evidence

            payload["engine_evidence"] = project_engine_evidence(d)
        except Exception:
            pass
    return sanitize_shareable_payload(payload)


def _write_evidence_bundle(
    run_dir: Path, result: ScanRunResult, config: ScanConfig, df: Optional[pd.DataFrame],
) -> Optional[Path]:
    """Build ``evidence_bundle.json`` from the completed scan + optional DataFrame.

    This is the only place ``profile_metrics`` / ``evidence_samples`` (impure —
    they need the DataFrame) meet the pure ``evidence_bundle`` builder.
    """
    from redibis.scan import profile_metrics as pm
    from redibis.scan.evidence_bundle import BundleOptions, SampleMode, build_evidence_bundle
    from redibis.scan.evidence_samples import collect_samples, seed_from_run_id

    try:
        sample_mode = SampleMode(getattr(config, "evidence_bundle_sample_mode", "raw"))
    except ValueError:
        sample_mode = SampleMode.RAW

    options = BundleOptions(
        sample_mode=sample_mode,
        sample_n=int(getattr(config, "evidence_bundle_sample_n", 10)),
        top_values_n=int(getattr(config, "evidence_bundle_top_values_n", 20)),
        format_masks_n=int(getattr(config, "evidence_bundle_format_masks_n", 10)),
        numeric_histogram_bins=int(getattr(config, "evidence_bundle_numeric_histogram_bins", 20)),
        length_histogram_bins=int(getattr(config, "evidence_bundle_length_histogram_bins", 20)),
        include_profile_groups=tuple(getattr(config, "evidence_bundle_include_profile_groups", None) or ()),
    )
    seed = seed_from_run_id(result.run_id)

    mask_plan = None
    raw_to_masked: dict[str, dict[str, Any]] = {}
    samples: Optional[dict[str, list]] = None
    columns = list(df.columns) if df is not None else _columns_from_result(result)

    if df is not None and sample_mode == SampleMode.MASKED:
        from redibis.masking.plan import auto_suggest_plan

        detections = [
            {"column": d.column, "detected": d.detected, "entity_type": d.entity_type}
            for d in (result.pii_detections or [])
        ]
        mask_plan = auto_suggest_plan(result.table, list(df.columns), detections)
        raw_to_masked = _raw_to_masked_maps(df, mask_plan, seed)

    if df is not None and sample_mode != SampleMode.NONE:
        samples = collect_samples(df, n=options.sample_n, seed=seed, mask_plan=mask_plan)

    by_engine = _by_engine_block(config, result)
    profiles: dict[str, dict] = {}
    for col in columns:
        if df is None:
            profiles[col] = {"by_engine": by_engine} if by_engine else {}
            continue
        values = df[col].where(df[col].notna(), None).tolist()
        profile: dict[str, Any] = {
            "counts": pm.profile_counts(values),
            "length": pm.profile_length(values, bins=options.length_histogram_bins),
            "format_masks": pm.profile_format_masks(values, top_n=options.format_masks_n),
            "charset": pm.profile_charset(values),
        }
        numeric = pm.profile_numeric(values, bins=options.numeric_histogram_bins)
        if numeric is not None:
            profile["numeric"] = numeric
        temporal = pm.profile_temporal(values)
        if temporal is not None:
            profile["temporal"] = temporal

        frequency = pm.profile_frequency(values, top_n=options.top_values_n)
        if sample_mode == SampleMode.MASKED:
            col_map = raw_to_masked.get(col, {})
            for item in frequency.get("top_values", []):
                item["value"] = col_map.get(str(item["value"]), "‹masked›")
        elif sample_mode == SampleMode.NONE:
            for item in frequency.get("top_values", []):
                item.pop("value", None)
        profile["frequency"] = frequency
        if by_engine:
            profile["by_engine"] = by_engine
        if options.include_profile_groups:
            allowed = set(options.include_profile_groups) | {"by_engine"}
            profile = {k: v for k, v in profile.items() if k in allowed}
        profiles[col] = profile

    pack_stack = None
    try:
        from redibis.classification.evidence import current_pack_stack

        pack_stack = current_pack_stack()
    except Exception:
        pack_stack = None

    bundle = build_evidence_bundle(
        result, config, profiles=profiles, samples=samples,
        pack_stack=pack_stack, options=options, seed=seed,
    )
    from redibis.evidence.persist import atomic_write_json

    path = run_dir / "evidence_bundle.json"
    atomic_write_json(path, bundle)
    return path


def _columns_from_result(result: ScanRunResult) -> list[str]:
    if result.col_dtypes:
        return list(result.col_dtypes.keys())
    profiles = getattr(result.profile, "column_profiles", None) if result.profile is not None else None
    if isinstance(profiles, (list, tuple)):
        return [p.column for p in profiles]
    if result.pii_detections:
        return [d.column for d in result.pii_detections]
    return []


def _by_engine_block(config: ScanConfig, result: Optional[ScanRunResult] = None) -> dict:
    if not getattr(config, "run_profile", False):
        return {}
    engine = getattr(config, "profiler_engine", "great_expectations") or "great_expectations"
    duration = None
    if result is not None:
        phase = (getattr(result, "phase_timings", None) or {}).get("profiling") or {}
        duration = phase.get("duration_ms")
    block = {"ran": True, "metrics": ["counts", "length", "frequency"]}
    if duration is not None:
        block["duration_ms"] = duration
    key = {
        "duckdb": "duckdb_profiler",
        "openmetadata": "om_profiler",
        "ydata": "ydata_profiler",
        "ydata_profiling": "ydata_profiler",
    }.get(engine, "ge_profiler")
    if engine == "duckdb":
        block["metrics"] = ["counts", "numeric"]
    return {key: block}


def _write_effective_config(run_dir: Path, config: ScanConfig) -> Path:
    from redibis.evidence.sanitize import config_sha256, sanitize_mapping

    payload = sanitize_mapping(config)
    path = run_dir / "effective_config.yaml"
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, default_flow_style=False, sort_keys=False)
    digest_path = run_dir / "effective_config.sha256"
    digest_path.write_text(config_sha256(payload) + "\n", encoding="utf-8")
    return path


def _write_result_variants(
    run_dir: Path, result: ScanRunResult, config: ScanConfig, artifacts: dict[str, str],
) -> dict[str, str]:
    from redibis.evidence.compare import build_result_bundle, pii_columns_from_detections

    det_cols = None
    domain = "scan"
    if config.run_pii:
        det_cols = pii_columns_from_detections(result.pii_detections or [])
        domain = "pii"
    elif config.run_profile:
        from redibis.evidence.compare import profile_columns_from_result

        summary = profile_columns_from_result(result)
        det_cols = summary or None
        domain = "profile"
    bundle = build_result_bundle(
        table=result.table,
        run_id=result.run_id,
        deterministic=det_cols,
        artifacts={"deterministic": artifacts.get("pii_report") or artifacts.get("evidence_bundle") or ""},
        domain=domain,
    )
    out: dict[str, str] = {}
    variants_path = run_dir / "result_variants.json"
    variants_path.write_text(
        json.dumps(bundle["variants"], indent=2, default=str), encoding="utf-8",
    )
    out["result_variants"] = str(variants_path)
    cmp_path = run_dir / "result_comparison.json"
    cmp_path.write_text(
        json.dumps(bundle["comparison"], indent=2, default=str), encoding="utf-8",
    )
    out["result_comparison"] = str(cmp_path)
    return out


def _store_shareable_evidence(
    run_dir: Path, bundle_path: str, run_writer: Optional[RunOutputWriter],
) -> str:
    from redibis.scan.evidence_ops import store_evidence, strip_raw_values

    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    stripped = strip_raw_values(bundle)
    shareable = run_dir / "evidence_bundle.shareable.json"
    from redibis.evidence.persist import atomic_write_json

    atomic_write_json(shareable, stripped)
    if run_writer is None:
        return str(shareable)
    _, key = store_evidence(bundle, run_writer)
    return key


def _write_evidence_manifest(
    run_dir: Path,
    result: ScanRunResult,
    config: ScanConfig,
    artifacts: dict[str, str],
    errors: list[dict[str, Any]],
    shareable_key: str,
) -> Path:
    from redibis.evidence.manifest import artifact_ref, build_manifest, logical_artifact_name
    from redibis.evidence.persist import atomic_write_json
    from redibis.evidence.sanitize import config_sha256, sanitize_mapping
    from redibis.scan.evidence_bundle import _engine_registry

    llm_index: list[dict[str, Any]] = []
    index_path = run_dir / "llm_calls" / "index.json"
    if index_path.is_file():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8")) or {}
            llm_index = list(payload.get("calls") or [])
            for item in payload.get("warnings") or []:
                if isinstance(item, dict):
                    errors.append(item)
        except (OSError, json.JSONDecodeError):
            llm_index = []

    engines = []
    try:
        engines = _engine_registry(result, config)
    except Exception:
        engines = []

    refs: dict[str, Any] = {}
    logical_names: list[str] = []
    digest_path = run_dir / "effective_config.sha256"
    if digest_path.is_file() and "effective_config.sha256" not in artifacts:
        artifacts = dict(artifacts)
        artifacts["effective_config.sha256"] = str(digest_path)
    llm_dir = run_dir / "llm_calls"
    if llm_dir.is_dir():
        for share in sorted(llm_dir.glob("*.json")):
            if share.name in {"index.json"} or share.name.endswith(".raw.json"):
                continue
            key = f"llm_calls/{share.name}"
            if key not in artifacts and f"s3:{key}" not in artifacts:
                artifacts = dict(artifacts)
                artifacts[key] = str(share)
    for key, value in sorted(artifacts.items(), key=lambda kv: str(kv[0])):
        if str(key).startswith("s3:"):
            continue
        raw = str(value or "")
        name = logical_artifact_name(run_dir, raw) or str(key)
        local = Path(raw) if raw and not raw.startswith("_meta/") and not raw.startswith("s3") else None
        if local is not None and not local.is_file():
            local = None
        sensitivity = "restricted" if str(name).endswith(".raw.json") else "shareable"
        refs[str(key)] = artifact_ref(
            name=name,
            path=name,
            kind=str(key),
            sensitivity=sensitivity,
            local_path=local,
            storage_key=raw if raw.startswith("_meta/") else str(artifacts.get(f"s3:{name}") or ""),
        )
        if name:
            logical_names.append(name)
    if shareable_key:
        refs["evidence_bundle_shareable"] = artifact_ref(
            name="evidence_bundle.shareable.json",
            path="evidence_bundle.shareable.json",
            kind="evidence_bundle",
            sensitivity="shareable",
            local_path=run_dir / "evidence_bundle.shareable.json",
            storage_key=shareable_key,
        )
    variants_payload = None
    variants_path = run_dir / "result_variants.json"
    if variants_path.is_file():
        try:
            variants_payload = json.loads(variants_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            variants_payload = None
    variant_list = []
    if isinstance(variants_payload, dict):
        variant_list = list(variants_payload.get("variants") or [])
    elif isinstance(variants_payload, list):
        variant_list = variants_payload
    shareable_calls = [
        {k: c.get(k) for k in (
            "seq", "call_id", "parent_call_id", "model_role", "status",
            "blocked", "shareable", "execution_mode", "step_id", "attempt",
        )}
        for c in llm_index if isinstance(c, dict)
    ]
    result_status = str(getattr(result, "status", "") or "")
    empty = bool(getattr(result, "total_rows", 0) == 0)
    if result_status == "success" and empty:
        run_status = "empty"
    elif result_status in {"success", "failed", "cancelled"}:
        run_status = result_status
    else:
        run_status = result_status or ""
    session_id = str(getattr(result, "session_id", "") or getattr(config, "session_id", "") or "")
    manifest = build_manifest(
        table=result.table,
        run_id=result.run_id,
        artifacts=refs,
        engines=engines,
        llm_calls=shareable_calls,
        result_variants=variant_list,
        errors=errors,
        warnings=list(getattr(result, "evidence_warnings", None) or []),
        config_hash=config_sha256(sanitize_mapping(config)),
        config_artifact="effective_config.yaml",
        run_profile=bool(config.run_profile),
        run_quality=bool(config.run_quality),
        run_pii=bool(config.run_pii),
        source_files=sorted(set(logical_names)),
        agentic=any(c.get("execution_mode") == "agentic" for c in llm_index if isinstance(c, dict)),
        run_status=run_status,
        session_id=session_id,
        empty=empty,
    )
    path = run_dir / "evidence_manifest.json"
    atomic_write_json(path, manifest)
    return path


def _raw_to_masked_maps(
    df: pd.DataFrame, mask_plan, seed: int,
) -> dict[str, dict[str, Any]]:
    """``{column: {raw_value_str: masked_value}}`` — same keys+seed as ``collect_samples``."""
    from redibis.masking.engine import MaskingEngine, RunKeys

    keys = RunKeys.mint(run_id=f"evidence_{seed:x}", seed=str(seed))
    transformed = MaskingEngine(mask_plan, keys).transform_dataframe(df)
    maps: dict[str, dict[str, Any]] = {}
    for col in df.columns:
        raw = df[col].dropna()
        if raw.empty:
            maps[col] = {}
            continue
        masked_vals = transformed.loc[raw.index, col]
        maps[col] = {str(k): v for k, v in zip(raw.tolist(), masked_vals.tolist())}
    return maps


def _upload_artifacts(run_writer: RunOutputWriter, run_dir: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    from redibis.telemetry.llm_evidence import skip_raw_upload

    s3_links: dict[str, str] = {}
    errors: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir).as_posix()
        if skip_raw_upload(relative) or relative == "evidence_bundle.json":
            continue
        if relative == "evidence_manifest.json":
            continue
        try:
            key = run_writer.write_file(relative, path)
            s3_links[f"s3:{relative}"] = key
        except Exception as exc:
            errors.append({"phase": "upload", "error": f"{relative}: {exc}"})
    return s3_links, errors
