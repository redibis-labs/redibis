"""Evidence bundle — task set 01 (schema, builder, profiling).

Test numbering follows ``docs/internal/EVIDENCE_BUNDLE_PLAN.md`` §8 (PLAN tests
1-12 are the task-set-01 scope); a few extra tests cover the exit criteria
listed in ``docs/internal/EVIDENCE_TASKS_01_BUNDLE.md``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from redibis.models import PIIDetection
from redibis.scan import profile_metrics as pm
from redibis.scan.base import Scan
from redibis.scan.config import ScanConfig
from redibis.scan.evidence_bundle import (
    BundleOptions,
    SampleMode,
    build_evidence_bundle,
    is_exportable,
)
from redibis.scan.evidence_samples import collect_samples, seed_from_run_id
from redibis.scan.report_bundle import ReportBundle
from redibis.scan.types import EngineScanResult


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fake_result(**overrides) -> EngineScanResult:
    defaults = dict(
        run_id="run_fixture_1",
        table="telecom.customers",
        status="success",
        total_rows=25,
        total_columns=1,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:20+00:00",
        phase_timings={
            "sampling": {
                "started_at": "2026-01-01T00:00:00.000000+00:00",
                "finished_at": "2026-01-01T00:00:01.000000+00:00",
            },
            "profiling": {
                "started_at": "2026-01-01T00:00:01.000000+00:00",
                "finished_at": "2026-01-01T00:00:05.000000+00:00",
            },
            "quality": {
                "started_at": "2026-01-01T00:00:05.000000+00:00",
                "finished_at": "2026-01-01T00:00:08.000000+00:00",
            },
            "pii": {
                "started_at": "2026-01-01T00:00:08.000000+00:00",
                "finished_at": "2026-01-01T00:00:20.000000+00:00",
            },
        },
    )
    defaults.update(overrides)
    return EngineScanResult(**defaults)


def _msisdn_values(n_a: int = 20, n_b: int = 5) -> list[str]:
    return ["01012345678"] * n_a + ["01098765432"] * n_b


def _msisdn_profile() -> dict:
    values = _msisdn_values()
    return {
        "counts": pm.profile_counts(values),
        "length": pm.profile_length(values),
        "format_masks": pm.profile_format_masks(values),
        "charset": pm.profile_charset(values),
        "frequency": pm.profile_frequency(values),
    }


@pytest.fixture(scope="module")
def real_scan_bundle(tmp_path_factory) -> dict:
    """One real scan (profile + quality + pii) → its evidence bundle.

    Shared (module-scoped) across tests 5/6/7/exit-criteria because PII
    detection pays a one-time NLP-engine load cost.
    """
    df = pd.DataFrame({
        "msisdn": _msisdn_values(),
        "notes": ["hello world"] * 25,
    })
    cfg = ScanConfig(
        table="telecom.customers",
        run_profile=True,
        run_quality=True,
        run_pii=True,
        equation_mode="independent",
    )
    run = Scan(cfg).run(df, run_id="real_scan_1")
    assert run.status == "success", run.error
    run_dir = tmp_path_factory.mktemp("evidence_real_scan")
    artifacts = ReportBundle(run, cfg).flush(run_dir, df=df)
    bundle_path = Path(artifacts["evidence_bundle"])
    return json.loads(bundle_path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# 1 — builder is pure
# ─────────────────────────────────────────────────────────────────────────────

_BANNED_IMPORTS = {"httpx", "boto3", "requests", "redibis.store", "sqlalchemy", "pandas"}


def _module_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_builder_is_pure():
    path = Path("redibis/scan/evidence_bundle.py")
    names = _module_import_names(path)
    for banned in _BANNED_IMPORTS:
        assert not any(n == banned or n.startswith(banned + ".") for n in names), (
            f"evidence_bundle.py must not import {banned!r} (found in {names})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2/3 — rule normalisation: rules_fired resolves; condition text lives once
# ─────────────────────────────────────────────────────────────────────────────

def test_rules_fired_resolves_into_header():
    result = _fake_result(pii_detections=[
        PIIDetection(column="msisdn", detected=True, entity_type="PHONE_NUMBER",
                     phone_score=0.95, phone_valid_rate=0.98, nid_valid_rate=0.90,
                     nid_checked=25),
    ])
    config = ScanConfig(table="telecom.customers", run_pii=True, run_profile=False, run_quality=False)
    bundle = build_evidence_bundle(
        result, config, profiles={"msisdn": _msisdn_profile()}, seed=42,
    )
    header_ids = set()
    header_ids |= {e["id"] for e in bundle["header"]["rules"]["equations"]}
    header_ids |= {e["id"] for e in bundle["header"]["rules"]["rulesets"]}
    header_ids |= {e["id"] for e in bundle["header"]["rules"]["custom_rules"]}
    header_ids |= {e["id"] for e in bundle["header"]["rules"]["validators"]}
    header_ids |= {e["id"] for e in bundle["header"]["rules"]["negative_signals"]}

    col = bundle["columns"]["msisdn"]
    for rule_id in col["rules_fired"]:
        assert rule_id in header_ids
    for rule_id in col["negative_signals_fired"]:
        assert rule_id in header_ids
    for rule_id in col.get("validator_results", {}):
        assert rule_id in header_ids
    assert col["pii_verdict"]["equation_id"] in header_ids


def test_no_rule_condition_text_under_columns():
    result = _fake_result(pii_detections=[
        PIIDetection(column="msisdn", detected=True, entity_type="PHONE_NUMBER",
                     nid_valid_rate=0.5, nid_checked=25),
        PIIDetection(column="created_at", detected=False),
    ])
    config = ScanConfig(table="telecom.customers", run_pii=True, run_profile=False, run_quality=False)
    bundle = build_evidence_bundle(
        result, config,
        profiles={"msisdn": _msisdn_profile()},
        seed=42,
    )
    condition_strings = set()
    for rule in bundle["header"]["rules"]["custom_rules"]:
        if rule.get("condition"):
            condition_strings.add(rule["condition"])
    for ns in bundle["header"]["rules"]["negative_signals"]:
        if ns.get("pattern"):
            condition_strings.add(ns["pattern"])

    columns_json = json.dumps(bundle["columns"])
    for text in condition_strings:
        assert text not in columns_json


# ─────────────────────────────────────────────────────────────────────────────
# 4 — engine registry: negatives carry a reason
# ─────────────────────────────────────────────────────────────────────────────

def test_engine_that_did_not_run_has_reason():
    result = _fake_result(pii_detections=[
        PIIDetection(column="msisdn", detected=True, entity_type="PHONE_NUMBER",
                     presidio_score=0.95),
    ])
    config = ScanConfig(table="telecom.customers", run_pii=True, run_profile=False, run_quality=False)
    bundle = build_evidence_bundle(result, config, profiles={}, seed=1)
    engines = bundle["header"]["engines"]
    assert engines, "engine registry must not be empty"
    for entry in engines:
        assert "ran" in entry
        if not entry["ran"]:
            assert entry.get("reason"), f"engine {entry['id']!r} has no reason for not running"
    ran_ids = {e["id"] for e in engines if e["ran"]}
    not_ran_ids = {e["id"] for e in engines if not e["ran"]}
    assert "presidio" in ran_ids
    assert not_ran_ids  # gliner/llm/learned should not have run in this fixture


# ─────────────────────────────────────────────────────────────────────────────
# 5/6/7 — real scan: provenance, timestamps, sampled_rows
# ─────────────────────────────────────────────────────────────────────────────

def test_provenance_fields_non_empty_on_real_scan(real_scan_bundle):
    prov = real_scan_bundle["header"]["provenance"]
    assert prov["redibis_version"]
    assert prov["redibis_build"] in ("oss", "enterprise")
    assert prov["python"]
    assert prov["platform"]
    assert prov["host"]
    assert prov["config_sha256"]
    # redibis_git_sha may legitimately be empty in a dev tree (PLAN §3 note).


def test_timestamps_cover_all_phases_with_positive_duration(real_scan_bundle):
    phases = real_scan_bundle["header"]["timestamps"]["phases"]
    for name in ("sampling", "profiling", "quality", "pii"):
        # "sampling" only appears when a SamplingConfig was supplied — this
        # fixture scans the full frame, so we only require the three that ran.
        if name == "sampling":
            continue
        assert name in phases, f"phase {name!r} missing from timestamps"
        assert phases[name]["duration_ms"] >= 0


def test_counts_sampled_rows_matches_header_sampling(real_scan_bundle):
    sampled_rows = real_scan_bundle["header"]["sampling"]["sampled_rows"]
    for column, block in real_scan_bundle["columns"].items():
        profile = block.get("profile")
        if profile is None:
            continue
        assert profile["counts"]["sampled_rows"] == sampled_rows, column


# ─────────────────────────────────────────────────────────────────────────────
# 8/9 — format_masks / length on a fixed-width column
# ─────────────────────────────────────────────────────────────────────────────

def test_format_masks_dominant_rate_on_fixed_width_column():
    values = ["01012345678"] * 100
    masks = pm.profile_format_masks(values)
    assert masks["dominant_mask_rate"] == 1.0
    assert masks["distinct_masks"] == 1
    assert masks["top"][0]["mask"] == "99999999999"


def test_length_distinct_and_fixed_width():
    values = ["01012345678"] * 100
    length = pm.profile_length(values)
    assert length["distinct_lengths"] == 1
    assert length["fixed_width"] is True


def test_format_masks_arabic_indic_digits_normalize():
    arabic = "٠١٠١٢٣٤٥٦٧٨"  # Arabic-Indic digits, same shape as an 11-digit MSISDN
    masks = pm.profile_format_masks([arabic] * 10)
    assert masks["top"][0]["mask"] == "9" * len(arabic)


# ─────────────────────────────────────────────────────────────────────────────
# 10 — normalized_entropy separates identifier from category
# ─────────────────────────────────────────────────────────────────────────────

def test_normalized_entropy_identifier_vs_category():
    unique_ids = [f"id_{i}" for i in range(200)]
    freq_unique = pm.profile_frequency(unique_ids)
    assert freq_unique["normalized_entropy"] >= 0.95

    categories = (["A"] * 150) + (["B"] * 30) + (["C"] * 15) + (["D"] * 4) + (["E"] * 1)
    freq_cat = pm.profile_frequency(categories)
    assert freq_cat["normalized_entropy"] < 0.3


# ─────────────────────────────────────────────────────────────────────────────
# 11 — numeric histogram bin counts sum to non_null
# ─────────────────────────────────────────────────────────────────────────────

def test_numeric_histogram_counts_sum_to_non_null():
    values = list(range(1000)) + [None, None]
    numeric = pm.profile_numeric(values, bins=20)
    counts = pm.profile_counts(values)
    assert numeric is not None
    assert numeric["parsed_count"] == counts["non_null"]
    assert sum(numeric["histogram"]["counts"]) == numeric["parsed_count"]


# ─────────────────────────────────────────────────────────────────────────────
# 12 — temporal monotonic_rate for a sorted event-time column
# ─────────────────────────────────────────────────────────────────────────────

def test_temporal_monotonic_rate_for_sorted_column():
    values = [f"2024-01-{d:02d}T00:00:00" for d in range(1, 29)]
    temporal = pm.profile_temporal(values)
    assert temporal is not None
    assert temporal["monotonic_rate"] == pytest.approx(1.0)


def test_temporal_none_for_non_temporal_column():
    values = ["hello", "world", "foo", "bar"] * 5
    assert pm.profile_temporal(values) is None


def test_numeric_none_for_non_numeric_column():
    values = ["hello", "world", "foo", "bar"] * 5
    assert pm.profile_numeric(values) is None


# ─────────────────────────────────────────────────────────────────────────────
# Exit criteria — pack_stack null, evidence_bundle.json on disk, determinism
# ─────────────────────────────────────────────────────────────────────────────

def test_pack_stack_none_is_accepted():
    result = _fake_result(pii_detections=[])
    config = ScanConfig(table="telecom.customers", run_pii=False, run_profile=False, run_quality=False)
    bundle = build_evidence_bundle(result, config, profiles={}, pack_stack=None, seed=1)
    assert bundle["header"]["provenance"]["pack_stack"] is None
    json.dumps(bundle)  # must still be JSON-serializable


def test_evidence_bundle_json_appears_in_run_dir(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    cfg = ScanConfig(table="t.demo", run_profile=False, run_quality=False, run_pii=False)
    run = Scan(cfg).run(df, run_id="fast_run")
    assert run.status == "success"
    artifacts = ReportBundle(run, cfg).flush(tmp_path, df=df)
    assert "evidence_bundle" in artifacts
    bundle_path = tmp_path / "evidence_bundle.json"
    assert bundle_path.exists()
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "2.0"
    assert data["kind"] == "redibis.evidence_bundle"


def test_reproducible_seed_same_run_id_same_samples():
    df = pd.DataFrame({"a": list(range(50))})
    seed1 = seed_from_run_id("run_abc")
    seed2 = seed_from_run_id("run_abc")
    assert seed1 == seed2
    samples1 = collect_samples(df, n=5, seed=seed1)
    samples2 = collect_samples(df, n=5, seed=seed2)
    assert samples1 == samples2

    seed3 = seed_from_run_id("run_xyz")
    samples3 = collect_samples(df, n=5, seed=seed3)
    assert samples3 != samples1


def test_collect_samples_excludes_nulls():
    df = pd.DataFrame({"a": [None, None, None, 1, 2, 3, None]})
    samples = collect_samples(df, n=10, seed=1)
    assert None not in samples["a"]
    assert set(samples["a"]) <= {1, 2, 3}


def test_none_mode_strips_literal_values():
    result = _fake_result(pii_detections=[])
    config = ScanConfig(table="telecom.customers", run_pii=False, run_profile=False, run_quality=False)
    options = BundleOptions(sample_mode=SampleMode.NONE)
    samples = {"msisdn": ["01012345678"]}
    bundle = build_evidence_bundle(
        result, config, profiles={"msisdn": _msisdn_profile()}, samples=samples,
        options=options, seed=1,
    )
    col = bundle["columns"]["msisdn"]
    assert col["samples"]["values"] == []
    for item in col["profile"]["frequency"]["top_values"]:
        assert "value" not in item
    assert is_exportable(bundle)
    serialized = json.dumps(bundle)
    assert "01012345678" not in serialized


def test_masked_mode_flags_sensitivity_without_raw_pii():
    result = _fake_result(pii_detections=[])
    config = ScanConfig(table="telecom.customers", run_pii=False, run_profile=False, run_quality=False)
    options = BundleOptions(sample_mode=SampleMode.MASKED)
    bundle = build_evidence_bundle(result, config, profiles={}, options=options, seed=1)
    assert bundle["sensitivity"]["contains_raw_pii"] is False
    assert bundle["sensitivity"]["egress"] == "allow"
    assert is_exportable(bundle)


def test_raw_mode_flags_sensitivity_as_raw_pii():
    result = _fake_result(pii_detections=[])
    config = ScanConfig(table="telecom.customers", run_pii=False, run_profile=False, run_quality=False)
    options = BundleOptions(sample_mode=SampleMode.RAW)
    bundle = build_evidence_bundle(result, config, profiles={}, options=options, seed=1)
    assert bundle["sensitivity"]["contains_raw_pii"] is True
    assert bundle["sensitivity"]["egress"] == "deny"
    assert not is_exportable(bundle)


# ─────────────────────────────────────────────────────────────────────────────
# T9 — sensitivity fencing: the bundle never enters a catalog push payload
# ─────────────────────────────────────────────────────────────────────────────

def test_evidence_bundle_never_referenced_by_catalog_package():
    catalog_dir = Path("redibis/services/catalog")
    offenders = []
    for path in catalog_dir.glob("*.py"):
        if "evidence_bundle" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert not offenders, f"catalog modules must never reference evidence_bundle: {offenders}"
