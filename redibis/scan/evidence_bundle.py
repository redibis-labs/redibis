"""
redibis.scan.evidence_bundle
=============================
Pure projection of a completed scan (``EngineScanResult``) into the evidence
bundle schema — see ``docs/internal/EVIDENCE_BUNDLE_PLAN.md`` §3 for the
canonical JSON shape and §4 for the profiling metric definitions this module
assembles under ``columns.<name>.profile``.

Module-scope imports are restricted to the standard library (``dataclasses``,
``datetime``, ``enum``, ``hashlib``, ``json``, ``math``, ``re``,
``statistics``, ``typing``). No ``httpx``, ``boto3``, ``requests``,
``redibis.store``, ``sqlalchemy`` or ``pandas`` — this module never touches
the network, a filesystem, or a DataFrame. It only re-shapes data its caller
already computed (profiles, samples, pack stack) into the canonical schema.
Anything that legitimately needs another redibis module (package version,
default thresholds) is a **function-scope** import so the module stays a
leaf at import time. See CLAUDE.md invariant 23.

The bundle stores detector *evidence*, never a verdict it computed itself —
``columns.<name>.pii_verdict`` is copied from ``PIIDetection`` fields that
``decide_pii`` (``redibis/pii/equations.py``) already set. This module does
not import ``decide_pii`` and never calls it (invariant 6).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # type-only — no runtime import, keeps this module a leaf
    from redibis.scan.config import ScanConfig
    from redibis.scan.types import EngineScanResult

SCHEMA_VERSION = "2.0"
BUNDLE_KIND = "redibis.evidence_bundle"


class SampleMode(str, Enum):
    RAW = "raw"
    MASKED = "masked"
    NONE = "none"


class Coverage(str, Enum):
    EVALUATED = "evaluated"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class BundleOptions:
    sample_mode: SampleMode = SampleMode.RAW
    sample_n: int = 10
    top_values_n: int = 20
    format_masks_n: int = 10
    numeric_histogram_bins: int = 20
    length_histogram_bins: int = 20
    include_profile_groups: tuple[str, ...] = ()  # empty = all applicable


# Default per-engine confidence floors, mirrored from ``redibis/pii/thresholds.py``
# (``Thresholds`` dataclass defaults). Duplicated here — deliberately — rather
# than imported, so this module never has a runtime dependency on the equation
# package (invariant 6 / repo-context note: "do not import from the builder").
_DEFAULT_THRESHOLDS: dict[str, float] = {
    "presidio_min": 0.80,
    "gliner_min": 0.70,
    "llm_min": 0.82,
    "phone_min": 0.80,
    "nid_min": 0.85,
    "imei_min": 0.85,
    "imsi_min": 0.90,
    "geo_min": 0.75,
    "learned_min": 0.90,
    "very_high_confidence_floor": 0.90,
}

#: Negative-signal pattern for timestamp-shaped column names. Kept in sync with
#: (but not imported from) ``redibis/pii/national_id_egypt.py::_NOT_NID_NAME_RE``.
_NS_TIMESTAMP_NAME_PATTERN = (
    r"(?:^|_)(ts|time|timestamp|epoch|datetime|dt|date|created|updated|"
    r"modified|inserted|loaded|event_time|at)(?:$|_)"
)
_NS_TIMESTAMP_NAME_RE = re.compile(_NS_TIMESTAMP_NAME_PATTERN, re.IGNORECASE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _threshold(config: Any, name: str) -> float:
    """Read a threshold off ``config.thresholds`` (or the shared default)."""
    t = getattr(config, "thresholds", None)
    if t is not None:
        val = getattr(t, name, None)
        if val is not None:
            return float(val)
    return _DEFAULT_THRESHOLDS[name]


# ─────────────────────────────────────────────────────────────────────────────
# T2 — provenance + timestamps
# ─────────────────────────────────────────────────────────────────────────────

def _git_sha() -> str:
    """Short git SHA embedded at package-build time.

    Read from ``REDIBIS_GIT_SHA`` (stamped into the process environment by CI /
    packaging tooling). Empty in a dev checkout that has not stamped it — the
    schema explicitly allows this one field to be empty outside a build.
    """
    import os

    return os.environ.get("REDIBIS_GIT_SHA", "")


def _build_flavour() -> str:
    """``"oss"`` or ``"enterprise"`` — whether the vendor-internal tree is present."""
    import importlib.util

    try:
        return "enterprise" if importlib.util.find_spec("enterprise") is not None else "oss"
    except (ImportError, ValueError):
        return "oss"


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def _config_sha256(config: Any) -> str:
    """Stable hash of the effective scan configuration.

    Same digest as ``evidence_manifest.json`` (``redibis.evidence.sanitize.config_sha256``).
    """
    from redibis.evidence.sanitize import config_sha256, sanitize_mapping

    return config_sha256(sanitize_mapping(config))


def _jsonable(obj: Any) -> Any:
    """Best-effort recursive conversion to plain JSON-safe types."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {f: _jsonable(getattr(obj, f)) for f in obj.__dataclass_fields__}
    if hasattr(obj, "__fspath__"):  # Path-like
        return str(obj)
    return str(obj)


def _provenance(config: Any, *, pack_stack: Optional[dict]) -> dict:
    import platform
    import socket

    from redibis import __version__

    return {
        "redibis_version": __version__,
        "redibis_git_sha": _git_sha(),
        "redibis_build": _build_flavour(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "host": socket.gethostname(),
        "pack_stack": pack_stack,
        "config_sha256": _config_sha256(config),
    }


def _phase_block(phase_timings: dict, name: str) -> Optional[dict]:
    entry = (phase_timings or {}).get(name)
    if not entry:
        return None
    started = entry.get("started_at")
    finished = entry.get("finished_at")
    duration_ms = entry.get("duration_ms")
    if duration_ms is None and started and finished:
        try:
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(finished)
            duration_ms = int((t1 - t0).total_seconds() * 1000)
        except (ValueError, TypeError):
            duration_ms = None
    return {"started_at": started, "finished_at": finished, "duration_ms": duration_ms}


def _timestamps(result: Any) -> dict:
    phase_timings = getattr(result, "phase_timings", None) or {}
    phases = {}
    for name in ("sampling", "profiling", "quality", "pii"):
        block = _phase_block(phase_timings, name)
        if block is not None:
            phases[name] = block
    return {
        "bundle_created_at": _utc_now_iso(),
        "scan_started_at": getattr(result, "started_at", None),
        "scan_finished_at": getattr(result, "completed_at", None),
        "phases": phases,
        "timezone": "UTC",
    }


# ─────────────────────────────────────────────────────────────────────────────
# T3 — engine registry
# ─────────────────────────────────────────────────────────────────────────────

def _pkg_version(name: str) -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _pkg_location(name: str) -> str:
    import importlib.util

    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return ""
    if spec is None or not spec.origin:
        return ""
    return spec.origin


def _any_detection(detections: list, pred) -> bool:
    return any(pred(d) for d in detections)


def _engine_registry(result: Any, config: Any) -> list[dict]:
    """Every engine Redibis knows about, whether or not it ran this scan."""
    detections = list(getattr(result, "pii_detections", None) or [])
    run_pii = bool(getattr(config, "run_pii", False))

    entries: list[dict] = []

    presidio_ran = run_pii and _any_detection(
        detections, lambda d: d.presidio_score is not None or bool(d.regex_hits)
    )
    entries.append(_engine_entry(
        "presidio", "Microsoft Presidio", "ner+regex",
        pkg="presidio_analyzer", ran=presidio_ran,
        reason=None if presidio_ran else ("pii.engines excludes regex" if run_pii else "run_pii=false"),
        extra={"nlp_engine": "spacy"},
    ))

    ner_config = getattr(config, "ner_config", None)
    ner_enabled = bool(ner_config is not None or getattr(config, "gliner_config", None) is not None)
    gliner_ran = run_pii and _any_detection(
        detections, lambda d: d.gliner_score is not None or bool(d.ner_hits)
    )
    gliner_extra: dict[str, Any] = {}
    if ner_config is not None:
        gliner_extra["model"] = getattr(ner_config, "model_path", "") or None
        gliner_extra["device"] = getattr(ner_config, "device", None)
        gliner_extra["batch_size"] = getattr(ner_config, "batch_size", None)
    entries.append(_engine_entry(
        "gliner", "GLiNER", "ner",
        pkg="gliner", ran=gliner_ran,
        reason=None if gliner_ran else (
            "not configured" if not ner_enabled else "pii.engines excludes ner"
        ),
        extra=gliner_extra,
    ))

    llm_config = getattr(config, "llm_config", None)
    llm_enabled = bool(getattr(llm_config, "enabled", False)) if llm_config is not None else False
    llm_ran = run_pii and _any_detection(detections, lambda d: d.llm_score is not None)
    llm_extra = {}
    if llm_config is not None:
        llm_extra = {
            "provider": getattr(llm_config, "provider", None),
            "model": getattr(llm_config, "model_name", None),
            "endpoint": getattr(llm_config, "endpoint_url", None),
        }
    entries.append(_engine_entry(
        "llm_refiner", "LiteLLM refiner", "llm",
        pkg="litellm", ran=llm_ran,
        reason=None if llm_ran else ("pii.llm.enabled=false" if not llm_enabled else "not triggered"),
        extra=llm_extra,
    ))

    phone_ran = run_pii and _any_detection(
        detections,
        lambda d: d.phone_score is not None or d.phone_valid_rate is not None,
    )
    use_phonenumbers = getattr(config, "use_phonenumbers", None)
    entries.append(_engine_entry(
        "phone", "libphonenumber", "validator",
        pkg="phonenumbers", ran=phone_ran,
        reason=None if phone_ran else (
            "pii.use_phonenumbers=false" if use_phonenumbers is False else "no phone-shaped column"
        ),
        extra={"default_region": "EG"},
    ))

    learned_ran = run_pii and _any_detection(detections, lambda d: d.learned_score is not None)
    entries.append(_engine_entry(
        "learned", "Structured learned classifier", "classifier",
        pkg=None, ran=learned_ran,
        reason=None if learned_ran else "pii.learned.enabled=false",
    ))

    entries.append(_regex_catalog_entry(ran=presidio_ran or run_pii))

    profile = getattr(result, "profile", None)
    run_profile = bool(getattr(config, "run_profile", False))
    profiler_engine = getattr(config, "profiler_engine", "great_expectations") or "great_expectations"
    ge_ran = run_profile and profiler_engine == "great_expectations" and profile is not None
    entries.append(_engine_entry(
        "ge_profiler", "Great Expectations", "profiler",
        pkg="great_expectations", ran=ge_ran,
        reason=None if ge_ran else (
            "run_profile=false" if not run_profile else f"profiler_engine={profiler_engine!r}"
        ),
    ))

    duckdb_ran = run_profile and profiler_engine == "duckdb" and profile is not None
    entries.append(_engine_entry(
        "duckdb_profiler", "DuckDB SUMMARIZE", "profiler",
        pkg="duckdb", ran=duckdb_ran,
        reason=None if duckdb_ran else (
            "run_profile=false" if not run_profile else f"profiler_engine={profiler_engine!r}"
        ),
    ))

    om_ran = run_profile and profiler_engine == "openmetadata" and profile is not None
    entries.append(_engine_entry(
        "om_profiler", "OpenMetadata", "profiler",
        pkg=None, ran=om_ran,
        reason=None if om_ran else (
            "run_profile=false" if not run_profile else f"profiler_engine={profiler_engine!r}"
        ),
    ))
    ydata_ran = run_profile and profiler_engine in {"ydata", "ydata_profiling"} and profile is not None
    entries.append(_engine_entry(
        "ydata_profiler", "YData Profiling", "profiler",
        pkg=None, ran=ydata_ran,
        reason=None if ydata_ran else (
            "run_profile=false" if not run_profile else f"profiler_engine={profiler_engine!r}"
        ),
    ))

    for engine_id, name in (
        ("nid", "Egyptian National ID"),
        ("imei", "IMEI Luhn validator"),
        ("imsi", "IMSI validator"),
        ("geo", "Geo pair validator"),
    ):
        ran = run_pii and _any_detection(
            detections,
            lambda d, eid=engine_id: bool(
                (getattr(d, "engine_evidence", None) or {}).get(eid, {}).get("ran")
                or (getattr(d, "engine_states", None) or {}).get(eid, {}).get("ran")
            ),
        )
        entries.append(_engine_entry(
            engine_id, name, "validator",
            pkg=None, ran=ran,
            reason=None if ran else ("run_pii=false" if not run_pii else "did not run"),
        ))

    return entries


def _engine_entry(
    engine_id: str, name: str, kind: str, *, pkg: Optional[str], ran: bool,
    reason: Optional[str], extra: Optional[dict] = None,
) -> dict:
    entry: dict[str, Any] = {
        "engine_id": engine_id,
        "id": engine_id,
        "name": name,
        "kind": kind,
        "version": _pkg_version(pkg) if pkg else "",
        "location": _pkg_location(pkg) if pkg else "",
        "ran": bool(ran),
    }
    if extra:
        entry.update({k: v for k, v in extra.items() if v is not None})
    if not ran:
        entry["reason"] = reason or "did not run"
    return entry


def _regex_catalog_entry(*, ran: bool) -> dict:
    from redibis import __version__

    entries_count = 0
    catalog_sha256 = ""
    try:
        from redibis.pii.regex_catalog import CATALOG

        entries_count = len(CATALOG)
        catalog_sha256 = _hash_module_source("redibis.pii.regex_catalog")
    except ImportError:
        pass
    entry = {
        "id": "regex_catalog",
        "name": "Redibis regex catalog",
        "kind": "regex",
        "version": __version__,
        "location": "redibis/pii/regex_catalog.py",
        "entries": entries_count,
        "catalog_sha256": catalog_sha256,
        "ran": bool(ran),
    }
    if not ran:
        entry["reason"] = "run_pii=false"
    return entry


def _hash_module_source(module_name: str) -> str:
    import importlib

    try:
        mod = importlib.import_module(module_name)
        source_path = getattr(mod, "__file__", None)
        if not source_path:
            return ""
        with open(source_path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (ImportError, OSError):
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# T4 — rule normalisation (header.rules — text lives here ONLY)
# ─────────────────────────────────────────────────────────────────────────────

def _equation_expression(mode: str) -> str:
    if mode == "strict":
        return "ALL available engines vote yes at or above their threshold"
    if mode == "balanced":
        return "2-of-N engines vote yes OR one engine >= very_high_confidence_floor"
    if mode == "lenient":
        return "any single engine >= its threshold triggers detection"
    return "each engine evaluated independently against its own threshold"  # independent


def _equations_registry(result: Any, config: Any) -> list[dict]:
    detections = list(getattr(result, "pii_detections", None) or [])
    default_mode = getattr(config, "equation_mode", "independent") or "independent"
    modes = {d.equation_used for d in detections if getattr(d, "equation_used", None)}
    modes.add(default_mode)
    thresholds = {name: _threshold(config, name) for name in _DEFAULT_THRESHOLDS}
    return [
        {
            "id": f"eq.{mode}",
            "mode": mode,
            "default": mode == default_mode,
            "expression": _equation_expression(mode),
            "thresholds": thresholds,
        }
        for mode in sorted(modes)
    ]


def _validators_registry(config: Any) -> list[dict]:
    return [
        {
            "id": "v.eg_nid",
            "name": "validate_egypt_national_id",
            "params": {"nid_min": _threshold(config, "nid_min")},
        },
        {
            "id": "v.imei",
            "name": "validate_imei",
            "params": {"imei_min": _threshold(config, "imei_min")},
        },
        {
            "id": "v.imsi",
            "name": "validate_imsi",
            "params": {"imsi_min": _threshold(config, "imsi_min")},
        },
        {
            "id": "v.geo",
            "name": "validate_geo_pair",
            "params": {"geo_min": _threshold(config, "geo_min")},
        },
    ]


def _negative_signals_registry() -> list[dict]:
    return [
        {"id": "ns.timestamp_name", "pattern": _NS_TIMESTAMP_NAME_PATTERN},
    ]


def _rules_registry(result: Any, config: Any, *, pack_stack: Optional[dict]) -> dict:
    rulesets: list[dict] = []
    custom_rules: list[dict] = []
    if isinstance(pack_stack, dict):
        for pack in pack_stack.get("rulesets", []) or []:
            if isinstance(pack, dict):
                rulesets.append(pack)
        for rule in pack_stack.get("custom_rules", []) or []:
            if isinstance(rule, dict):
                custom_rules.append(rule)
    return {
        "equations": _equations_registry(result, config),
        "rulesets": rulesets,
        "custom_rules": custom_rules,
        "validators": _validators_registry(config),
        "negative_signals": _negative_signals_registry(),
    }


_VALIDATOR_FIELD_MAP: tuple[tuple[str, str, str], ...] = (
    # (validator id, valid_rate attr, checked-count attr)
    ("v.eg_nid", "nid_valid_rate", "nid_checked"),
    ("v.imei", "imei_valid_rate", "imei_checked"),
    ("v.imsi", "imsi_valid_rate", "imsi_checked"),
    ("v.geo", "geo_confidence", ""),
)


def _validator_results_for(detection: Any) -> dict:
    out: dict[str, dict] = {}
    for vid, rate_attr, checked_attr in _VALIDATOR_FIELD_MAP:
        rate = getattr(detection, rate_attr, None)
        if rate is None:
            continue
        checked = getattr(detection, checked_attr, None) if checked_attr else None
        entry: dict[str, Any] = {"rate": rate}
        if checked is not None:
            entry["checked"] = checked
        out[vid] = entry
    return out


def _negative_signals_fired_for(column: str) -> list[str]:
    return ["ns.timestamp_name"] if _NS_TIMESTAMP_NAME_RE.search(column) else []


def _rules_fired_for(detection: Any, *, custom_rule_ids: frozenset[str]) -> list[str]:
    """Only IDs that resolve into ``header.rules.custom_rules`` — see PLAN §8 test 2.

    ``edge_rule_ids`` (namespaced ``cr.<policy_name>.<rule_id>``, set by
    ``refine_detection`` in ``redibis.classification.edge_rules``) are the
    actual custom-rule identifiers; we still intersect with ``custom_rule_ids``
    (sourced from the resolved ``pack_stack``) so a stale or mismatched pack
    reference can never silently cite a rule that isn't in ``header.rules``.
    """
    fired = set(getattr(detection, "edge_rule_ids", None) or [])
    return sorted(fired & custom_rule_ids)


# ─────────────────────────────────────────────────────────────────────────────
# Column blocks — pii_evidence / pii_verdict / quality / samples
# ─────────────────────────────────────────────────────────────────────────────

def _pii_evidence_for(d: Any) -> dict:
    from redibis.evidence.engines import list_engine_records, merge_compat_blocks

    records = list_engine_records(d)
    compat = merge_compat_blocks(records)
    # Keep the original keyed blocks for replay; expose full NER hits too.
    gliner = compat.get("gliner") or {}
    if gliner.get("ran"):
        gliner["hits"] = list(getattr(d, "ner_hits", None) or [])
        compat["gliner"] = gliner
    return compat


def _pii_verdict_for(d: Any, config: Any, *, decided_at: Optional[str] = None) -> dict:
    equation_used = getattr(d, "equation_used", None) or getattr(config, "equation_mode", "independent")
    return {
        "derived": True,
        "equation_id": f"eq.{equation_used}",
        "decided_at": decided_at,
        "detected": bool(getattr(d, "detected", False)),
        "entity_type": getattr(d, "entity_type", None),
        "confidence": float(getattr(d, "confidence", 0.0) or 0.0),
        "deciding_engines": list(d.contributing_engines()) if hasattr(d, "contributing_engines") else [],
    }


def _quality_by_column(result: Any) -> dict[str, dict]:
    qa = getattr(result, "quality_gatekeeper", None)
    quality_results = getattr(result, "quality_results", None)
    if qa is None or quality_results is None:
        return {}
    try:
        report = qa._extract_report_data(quality_results)
        rows = report.get("results", []) or []
    except Exception:
        return {}

    by_col: dict[str, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        col = row.get("column") or (row.get("kwargs") or {}).get("column")
        if not col:
            continue
        by_col.setdefault(col, []).append(row)

    out: dict[str, dict] = {}
    for col, rows_ in by_col.items():
        expectations = []
        passed = 0
        failed = 0
        for row in rows_:
            success = bool(row.get("success"))
            passed += int(success)
            failed += int(not success)
            observed: dict[str, Any] = {}
            if row.get("unexpected_pct") is not None:
                observed["unexpected_percent"] = row.get("unexpected_pct")
            if row.get("observed_value") is not None:
                observed["value"] = row.get("observed_value")
            expectations.append({
                "type": row.get("rule"),
                "success": success,
                "observed": observed,
                "engine": "ge_profiler",
            })
        out[col] = {"expectations": expectations, "passed": passed, "failed": failed}
    return out


def _samples_block_for(
    column: str, samples: Optional[dict], *, options: BundleOptions, seed: Optional[int],
) -> dict:
    values: list = []
    if options.sample_mode != SampleMode.NONE and samples:
        values = list(samples.get(column, []) or [])
    return {
        "mode": options.sample_mode.value,
        "n": options.sample_n,
        "seed": seed,
        "values": values,
    }


def _scrub_profile_for_none_mode(profile: dict) -> dict:
    """Safety net: strip any residual literal values when ``sample_mode`` is ``none``."""
    if not isinstance(profile, dict):
        return profile
    scrubbed = dict(profile)
    freq = scrubbed.get("frequency")
    if isinstance(freq, dict) and freq.get("top_values"):
        scrubbed_freq = dict(freq)
        scrubbed_freq["top_values"] = [
            {k: v for k, v in item.items() if k != "value"}
            for item in freq["top_values"]
            if isinstance(item, dict)
        ]
        scrubbed["frequency"] = scrubbed_freq
    return scrubbed


def _infer_column_class(profile: dict) -> str:
    if not isinstance(profile, dict):
        return "categorical"
    if profile.get("temporal") is not None:
        return "temporal"

    charset = profile.get("charset") or {}
    length = profile.get("length") or {}
    freq = profile.get("frequency") or {}
    counts = profile.get("counts") or {}
    normalized_entropy = freq.get("normalized_entropy")
    distinct_rate = counts.get("distinct_rate")
    is_high_cardinality = (normalized_entropy is not None and normalized_entropy >= 0.9) or (
        distinct_rate is not None and distinct_rate >= 0.9
    )
    # Fixed-width, leading-zero digit strings (MSISDN, NID) are identifiers even
    # though they parse as numeric — leading_zeros_rate is the tell (PLAN §4.4).
    looks_like_coded_identifier = bool(length.get("fixed_width")) and (
        charset.get("leading_zeros_rate", 0.0) or 0.0
    ) > 0.5
    if looks_like_coded_identifier or (is_high_cardinality and counts.get("is_candidate_key")):
        return "identifier"
    if profile.get("numeric") is not None:
        return "measure"
    if is_high_cardinality:
        return "identifier"
    if freq.get("is_categorical"):
        return "categorical"
    return "free_text"


def _coverage_for(column: str, *, profiles: dict, quality_by_col: dict, pii_by_col: dict) -> dict:
    return {
        "profile": Coverage.EVALUATED.value if column in profiles else Coverage.SKIPPED.value,
        "quality": Coverage.EVALUATED.value if column in quality_by_col else Coverage.SKIPPED.value,
        "pii": Coverage.EVALUATED.value if column in pii_by_col else Coverage.SKIPPED.value,
    }


def _column_profiles_meta(result: Any) -> dict[str, Any]:
    """``column -> ColumnProfile`` from ``result.profile.column_profiles`` (position, types)."""
    profile = getattr(result, "profile", None)
    col_profiles = getattr(profile, "column_profiles", None) if profile else None
    out: dict[str, Any] = {}
    if not col_profiles:
        return out
    for i, cp in enumerate(col_profiles):
        out[getattr(cp, "column", "")] = (i, cp)
    return out


def _columns_block(
    result: Any,
    config: Any,
    *,
    profiles: dict,
    samples: Optional[dict],
    options: BundleOptions,
    seed: Optional[int],
    custom_rule_ids: frozenset[str] = frozenset(),
) -> dict:
    pii_by_col = {d.column: d for d in (getattr(result, "pii_detections", None) or [])}
    quality_by_col = _quality_by_column(result)
    profile_meta = _column_profiles_meta(result)
    pii_decided_at = (
        (getattr(result, "phase_timings", None) or {}).get("pii", {}).get("finished_at")
    )

    all_columns: list[str] = []
    for src in (profiles.keys(), pii_by_col.keys(), quality_by_col.keys(), profile_meta.keys()):
        for col in src:
            if col not in all_columns:
                all_columns.append(col)

    columns: dict[str, dict] = {}
    for column in all_columns:
        profile = profiles.get(column)
        if options.sample_mode == SampleMode.NONE and profile is not None:
            profile = _scrub_profile_for_none_mode(profile)

        block: dict[str, Any] = {
            "coverage": _coverage_for(
                column, profiles=profiles, quality_by_col=quality_by_col, pii_by_col=pii_by_col,
            ),
        }
        pos_meta = profile_meta.get(column)
        if pos_meta is not None:
            position, cp = pos_meta
            block["position"] = position
            block["physical_type"] = getattr(cp, "physical_type", None)
            block["logical_type"] = getattr(cp, "logical_type", None)
        if profile is not None:
            block["profile"] = profile
            block["inferred_class"] = _infer_column_class(profile)

        detection = pii_by_col.get(column)
        if detection is not None:
            block["pii_evidence"] = _pii_evidence_for(detection)
            block["rules_fired"] = _rules_fired_for(detection, custom_rule_ids=custom_rule_ids)
            block["negative_signals_fired"] = _negative_signals_fired_for(column)
            validator_results = _validator_results_for(detection)
            if validator_results:
                block["validator_results"] = validator_results
            block["pii_verdict"] = _pii_verdict_for(detection, config, decided_at=pii_decided_at)
        else:
            block["negative_signals_fired"] = _negative_signals_fired_for(column)

        if column in quality_by_col:
            block["quality"] = quality_by_col[column]

        block["samples"] = _samples_block_for(column, samples, options=options, seed=seed)

        columns[column] = block
    return columns


def _table_summary(result: Any) -> dict:
    detections = list(getattr(result, "pii_detections", None) or [])
    by_entity: dict[str, int] = {}
    for d in detections:
        if d.detected and d.entity_type:
            by_entity[d.entity_type] = by_entity.get(d.entity_type, 0) + 1
    return {
        "pii": {
            "columns_scanned": getattr(result, "pii_columns_scanned", 0),
            "columns_detected": getattr(result, "pii_columns_detected", 0),
            "by_entity": by_entity,
        },
        "quality": {
            "expectations": getattr(result, "quality_expectations", 0),
            "passed": getattr(result, "quality_passed", 0),
            "failed": getattr(result, "quality_failed", 0),
        },
        "arabic_aware_columns": getattr(result, "arabic_aware_columns", 0),
    }


def _sensitivity_block(options: BundleOptions) -> dict:
    is_raw = options.sample_mode == SampleMode.RAW
    return {
        "contains_raw_pii": is_raw,
        "sample_mode": options.sample_mode.value,
        "egress": "deny" if is_raw else "allow",
        "note": (
            "Unmasked values from PII-classified columns. Strip with "
            "`redibis scan evidence store` before persisting or sharing."
            if is_raw
            else "No raw source values present in this bundle."
        ),
    }


def _scan_types(config: Any) -> list[str]:
    types = []
    if getattr(config, "run_profile", False):
        types.append("profile")
    if getattr(config, "run_quality", False):
        types.append("quality")
    if getattr(config, "run_pii", False):
        types.append("pii")
    return types


def _table_block(result: Any, config: Any, *, columns: dict) -> dict:
    session_id = str(getattr(result, "session_id", "") or getattr(config, "session_id", "") or "")
    block = {
        "name": getattr(result, "table", ""),
        "run_id": getattr(result, "run_id", ""),
        "status": getattr(result, "status", ""),
        "scan_types": _scan_types(config),
        "column_count": getattr(result, "total_columns", 0) or len(columns),
    }
    if session_id:
        block["session_id"] = session_id
    return block


def build_evidence_bundle(
    result: "EngineScanResult",
    config: "ScanConfig",
    *,
    profiles: dict[str, dict],
    samples: Optional[dict[str, list]] = None,
    pack_stack: Optional[dict] = None,
    options: BundleOptions = BundleOptions(),
    seed: Optional[int] = None,
) -> dict:
    """Pure projection of a completed scan into the schema in PLAN §3.

    ``profiles`` and ``samples`` are pre-computed by the caller (which holds
    the DataFrame) — see ``redibis.scan.profile_metrics`` and
    ``redibis.scan.evidence_samples``. ``pack_stack`` is an opaque dict
    produced by task set 02; pass ``None`` until it exists.
    """
    rules = _rules_registry(result, config, pack_stack=pack_stack)
    custom_rule_ids = frozenset(r["id"] for r in rules["custom_rules"])
    columns = _columns_block(
        result, config, profiles=profiles, samples=samples, options=options, seed=seed,
        custom_rule_ids=custom_rule_ids,
    )

    sampling_config = getattr(config, "sampling_config", None)
    sampled_rows = getattr(result, "total_rows", 0)
    population_rows = getattr(sampling_config, "population_rows", None) if sampling_config else None
    header = {
        "provenance": _provenance(config, pack_stack=pack_stack),
        "timestamps": _timestamps(result),
        "engines": _engine_registry(result, config),
        "rules": rules,
        "sampling": {
            "strategy": getattr(sampling_config, "strategy", "none") if sampling_config else "none",
            "seed": seed,
            "requested_rows": getattr(sampling_config, "sample_size", None) if sampling_config else None,
            "sampled_rows": sampled_rows,
            "population_rows": population_rows if population_rows is not None else sampled_rows,
            "sampling_ratio": (
                (sampled_rows / population_rows) if population_rows else 1.0
            ),
        },
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "sensitivity": _sensitivity_block(options),
        "header": header,
        "table": _table_block(result, config, columns=columns),
        "columns": columns,
        "table_summary": _table_summary(result),
    }


def is_exportable(bundle: dict) -> bool:
    """True when ``bundle`` carries no raw source values (``masked``/``none`` mode).

    Used at the sensitivity fences (T9) — the air-gap packager and any catalog
    push path must refuse a bundle for which this returns ``False``.
    """
    sensitivity = bundle.get("sensitivity") or {}
    return sensitivity.get("sample_mode") in ("masked", "none") and not sensitivity.get(
        "contains_raw_pii", True
    )
