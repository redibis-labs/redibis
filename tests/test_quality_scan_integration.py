"""
Quality scan integration tests — Python API against golden e-shop fixtures.

Exercises profiling (GE + OpenMetadata), quality gatekeeper, rule customization,
sampling (pandas + Spark), contract/subcontract publishing (LocalBackend as
MinIO stand-in), and report generation (GE Data Docs + redibis HTML/YAML).

Run:
  pytest tests/test_quality_scan_integration.py -v
  pytest tests/test_quality_scan_integration.py -v -k "ge_profiler or customize"
  pytest tests/test_quality_scan_integration.py -v -m slow   # GE Data Docs copy
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from redibis.cli.main import _build_backend, _run_scan
from redibis.config import ProfilingConfig
from redibis.contracts.rule_code_parser import parse_ge_rules
from redibis.contracts.rules import extract_rules, regenerate
from redibis.profiling import get_profiler
from redibis.profiling.base import ProfileResult
from redibis.quality.rule_set import QualityRuleSet
from redibis.quality.sampling import PandasTableSampler, SamplingConfig, TableSampler
from redibis.scan.base import Scan
from redibis.scan.quality_phase import run_quality_phase
from redibis.services import pipeline
from redibis.services.scan_service import ScanConfig, ScanService
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend
from redibis.store.subcontract_store import SubcontractStore

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tests/fixtures/golden"
MERCHANT_CSV = GOLDEN_DIR / "realistic_merchant_seller_registry.csv"

SAMPLE_ROWS = 20
TABLE = "golden.merchant_seller_registry"

EXPECTED_QUALITY_ARTIFACTS = (
    "quality_contract.yaml",
    "interactive_review.html",
    "triage_report.html",
)

pytestmark = [pytest.mark.integration]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def merchant_df() -> pd.DataFrame:
    assert MERCHANT_CSV.is_file(), f"missing golden fixture: {MERCHANT_CSV}"
    return pd.read_csv(MERCHANT_CSV, nrows=SAMPLE_ROWS)


@pytest.fixture
def merchant_sample_csv(tmp_path, merchant_df) -> Path:
    out = tmp_path / "merchant_sample.csv"
    merchant_df.to_csv(out, index=False)
    return out


@pytest.fixture
def backend(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def store(backend):
    return ContractStore(backend, bucket="active-contracts")


@pytest.fixture
def sub_store(backend):
    return SubcontractStore(
        backend,
        pii_bucket="pii-contracts",
        quality_bucket="quality-contracts",
    )


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run_artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _quality_scan_config(
    table: str = TABLE,
    *,
    profiler_engine: str = "great_expectations",
    generate_ge_docs: bool = False,
    automerge: str = "none",
    sampling_config: SamplingConfig | None = None,
    artifacts_dir: Path | None = None,
    run_id: str = "quality_run_test",
) -> ScanConfig:
    return ScanConfig(
        table=table,
        run_pii=False,
        run_profile=True,
        run_quality=True,
        profiler_engine=profiler_engine,
        generate_ge_docs=generate_ge_docs,
        validate_contracts=False,
        automerge=automerge,
        sampling_config=sampling_config,
        artifacts_dir=artifacts_dir,
        run_id=run_id,
    )


def _run_scan_service(
    df: pd.DataFrame,
    backend: LocalBackend,
    store: ContractStore,
    sub_store: SubcontractStore,
    run_dir: Path,
    **config_kwargs,
):
    config = _quality_scan_config(artifacts_dir=run_dir, **config_kwargs)
    service = ScanService(
        backend,
        store,
        runs_bucket="pii-reports",
        sub_store=sub_store,
    )
    return service.scan_dataframe(df, config), config


def _parsed_rules_to_rule_set(parsed: dict) -> QualityRuleSet:
    rules = []
    for r in parsed.get("rules", []):
        kwargs = dict(r.get("kwargs") or {})
        col = r.get("column")
        if col and "column" not in kwargs:
            kwargs["column"] = col
        rules.append({
            "rule": r["expectation_type"],
            "column": col,
            "kwargs": kwargs,
        })
    return QualityRuleSet(rules=rules)


def _assert_quality_artifacts(artifacts_dir: Path, *, expect_ge_docs: bool = False) -> None:
    assert artifacts_dir.is_dir(), f"missing artifacts dir: {artifacts_dir}"
    names = {p.name for p in artifacts_dir.iterdir()}
    for key in EXPECTED_QUALITY_ARTIFACTS:
        assert key in names, f"expected artifact {key!r} in {sorted(names)}"
    quality_reports = list(artifacts_dir.glob("quality-report-*.html"))
    assert quality_reports, "expected redibis quality-report-*.html"
    assert "Quality Report" in quality_reports[0].read_text(encoding="utf-8")
    if expect_ge_docs:
        ge_index = artifacts_dir / "ge_report" / "index.html"
        assert ge_index.is_file(), "GE Data Docs index.html missing"
        assets = list((artifacts_dir / "ge_report").rglob("*.css"))
        assert assets, "GE Data Docs should include CSS assets"


# ── Profilers ─────────────────────────────────────────────────────────────────


def test_ge_profiler_on_golden_merchant(merchant_df):
    """GE profiler suggests expectations on golden merchant data."""
    pytest.importorskip("great_expectations")
    profile = pipeline.profile_dataframe(
        merchant_df,
        "merchant_seller_registry",
        profiling=ProfilingConfig(engine="great_expectations"),
    )
    assert isinstance(profile, ProfileResult)
    assert profile.raw.get("engine") == "great_expectations"
    assert len(profile.suggested_rules.rules) >= 1
    assert len(profile.triage_signals) >= 1
    assert isinstance(profile.expectations, list)


def test_openmetadata_profiler_on_golden_merchant(merchant_df):
    """OpenMetadata profiler computes metrics and suggests portable rules."""
    profiler = get_profiler(ProfilingConfig(engine="open_metadata"))
    result = profiler.profile(merchant_df, dataset_name="merchant_seller_registry")

    assert result.raw["engine"] == "open_metadata"
    assert "om_metrics" in result.raw
    assert result.native_report_html
    assert "merchant_seller_registry" in result.native_report_html
    assert len(result.suggested_rules.rules) >= 1
    rule_names = {r["rule"] for r in result.suggested_rules.rules}
    assert "expect_column_values_to_not_be_null" in rule_names


# ── Full quality scan + artifacts ─────────────────────────────────────────────


def test_quality_scan_service_generates_contract_and_reports(
    merchant_df, backend, store, sub_store, run_dir,
):
    """ScanService quality path writes ODCS partial + redibis HTML reports."""
    pytest.importorskip("great_expectations")
    result, _ = _run_scan_service(
        merchant_df, backend, store, sub_store, run_dir,
    )
    assert result.status == "success"
    assert result.quality_expectations > 0
    assert result.quality_passed >= 0

    _assert_quality_artifacts(run_dir)
    contract = yaml.safe_load((run_dir / "quality_contract.yaml").read_text(encoding="utf-8"))
    assert contract["apiVersion"] == "v3.0.1"
    assert contract["schema"][0]["physicalName"] == TABLE

    assert backend.exists("quality-contracts", f"{TABLE}/{result.run_id}.yaml")
    assert store.get_active(TABLE) is None


@pytest.mark.slow
def test_quality_scan_generates_ge_data_docs(
    merchant_df, backend, store, sub_store, run_dir,
):
    """``generate_ge_docs=True`` copies GE Data Docs into ge_report/."""
    pytest.importorskip("great_expectations")
    result, _ = _run_scan_service(
        merchant_df,
        backend, store, sub_store, run_dir,
        generate_ge_docs=True,
    )
    assert result.status == "success"
    _assert_quality_artifacts(run_dir, expect_ge_docs=True)


# ── Customize rules: remove, paste, rerun ────────────────────────────────────


def test_remove_profiler_expectations_and_rerun_gatekeeper(merchant_df, run_dir):
    """Profile → trim suggested rules → validate with fewer expectations."""
    pytest.importorskip("great_expectations")
    config = _quality_scan_config()
    scan = Scan(config)
    profile = scan.profile(merchant_df, tbl_name="merchant_seller_registry")
    suggested = scan.suggest_quality_rules()
    assert len(suggested.rules) >= 2

    trimmed = QualityRuleSet(rules=list(suggested.rules[:2]))
    removed = suggested.remove_where(rule=suggested.rules[-1]["rule"])
    assert removed >= 0

    _, qa, quality_results, stats = run_quality_phase(
        merchant_df,
        config,
        profile,
        db_name="golden",
        tbl_name="merchant_seller_registry",
        run_dir=run_dir,
        rule_set=trimmed,
    )
    assert stats["total"] == len(trimmed.rules)
    assert quality_results.success is True
    contract = qa.export_quality_contract(
        database_name="golden",
        table_name="merchant_seller_registry",
    )
    assert contract["apiVersion"] == "v3.0.1"
    assert contract["schema"][0]["physicalName"] == TABLE
    schema0 = contract["schema"][0]
    assert schema0.get("properties") or schema0.get("quality")


def test_paste_custom_quality_rules_and_validate(merchant_df, run_dir):
    """Parse pasted GE code (no exec) and run only those expectations."""
    pytest.importorskip("great_expectations")
    code = """
qa.add_gx_expectation(
    expectation_name='expect_column_values_to_not_be_null',
    column='merchant_email')
qa.add_gx_expectation(
    expectation_name='expect_column_values_to_not_be_null',
    column='merchant_mobile')
qa.add_gx_expectation(
    expectation_name='expect_table_row_count_to_be_between',
    min_value=1, max_value=1000)
"""
    parsed = parse_ge_rules(code)
    assert parsed["errors"] == []
    custom = _parsed_rules_to_rule_set(parsed)

    config = _quality_scan_config()
    scan = Scan(config)
    profile = scan.profile(merchant_df, tbl_name="merchant_seller_registry")

    _, qa, quality_results, stats = run_quality_phase(
        merchant_df,
        config,
        profile,
        db_name="golden",
        tbl_name="merchant_seller_registry",
        run_dir=run_dir,
        rule_set=custom,
    )
    assert stats["total"] == 3
    assert quality_results.success is True

    props = {
        p["name"]: p
        for p in qa.export_quality_contract(
            database_name="golden",
            table_name="merchant_seller_registry",
        )["schema"][0]["properties"]
    }
    assert "merchant_email" in props
    assert any(
        r.get("rule") == "missingCount" or "not_be_null" in str(r)
        for r in props["merchant_email"].get("quality", [])
    )


def test_suppress_quality_rule_after_merge(merchant_df, backend, store, sub_store, run_dir):
    """Merged contract keeps suppressed rules removed across rescans."""
    pytest.importorskip("great_expectations")
    result, config = _run_scan_service(
        merchant_df,
        backend,
        store,
        sub_store,
        run_dir,
        automerge="quality",
    )
    assert result.status == "success"
    assert result.quality_contract_version

    active = store.get_active(TABLE)
    assert active is not None
    rules = extract_rules(active)
    assert rules

    victim = rules[0]
    store.suppress_quality_rule(TABLE, victim.rule_id, column=victim.column)

    # Re-scan reintroduces profiler rules; overlay should still win
    run_dir2 = run_dir.parent / "run_rescan"
    run_dir2.mkdir(exist_ok=True)
    result2, _ = _run_scan_service(
        merchant_df,
        backend,
        store,
        sub_store,
        run_dir2,
        automerge="quality",
        run_id="quality_rescan",
    )
    assert result2.status == "success"
    active2 = store.get_active(TABLE)
    remaining_ids = {r.rule_id for r in extract_rules(active2)}
    assert victim.rule_id not in remaining_ids


# ── Sampling: pandas + Spark ──────────────────────────────────────────────────


def test_pandas_sampler_fixed_rows(merchant_df):
    """PandasTableSampler mirrors Spark output contract for local dev."""
    cfg = SamplingConfig(strategy="fixed_rows", fixed_row_count=5, seed=7)
    out = PandasTableSampler(cfg).from_dataframe(merchant_df)
    assert len(out) == 5
    assert set(out.columns) == set(merchant_df.columns)


def test_pandas_sampler_statistical(merchant_df):
    cfg = SamplingConfig(strategy="statistical", sample_fraction=0.5, seed=3)
    out = PandasTableSampler(cfg).from_dataframe(merchant_df)
    assert 1 <= len(out) <= len(merchant_df)


@pytest.fixture
def spark_session():
    """Local Spark session; skips when Java/PySpark is unavailable."""
    pytest.importorskip("pyspark")
    try:
        from pyspark.sql import SparkSession

        spark = (
            SparkSession.builder
            .master("local[1]")
            .appName("redibis_quality_sampler_test")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
    except Exception as exc:
        pytest.skip(f"Spark not available in this environment: {exc}")
    yield spark
    spark.stop()


def test_spark_table_sampler_fixed_rows(merchant_df, spark_session):
    """Spark TableSampler samples a temp view (cluster-free local[1])."""
    spark_session.createDataFrame(merchant_df).createOrReplaceTempView("golden_merchant")
    sampler = TableSampler(
        spark_session,
        SamplingConfig(strategy="fixed_rows", fixed_row_count=8, seed=11),
    )
    out = sampler.sample("golden_merchant")
    assert len(out) <= 8
    assert "merchant_email" in out.columns


def test_quality_scan_with_pandas_sampler(
    merchant_df, backend, store, sub_store, run_dir,
):
    """ScanConfig.sampling_config applies PandasTableSampler before profiling."""
    pytest.importorskip("great_expectations")
    cfg = SamplingConfig(strategy="fixed_rows", fixed_row_count=10, seed=1)
    result, _ = _run_scan_service(
        merchant_df,
        backend,
        store,
        sub_store,
        run_dir,
        sampling_config=cfg,
    )
    assert result.status == "success"
    assert result.total_rows == 10
    assert result.quality_expectations > 0


# ── Publish accepted quality contract (MinIO stand-in) ────────────────────────


def test_publish_quality_subcontract_to_storage(
    merchant_df, backend, store, sub_store, run_dir,
):
    """Quality subcontract lands in quality-contracts; runs bucket gets manifest."""
    pytest.importorskip("great_expectations")
    result, config = _run_scan_service(
        merchant_df, backend, store, sub_store, run_dir,
    )
    assert result.status == "success"
    assert backend.exists("quality-contracts", f"{TABLE}/{result.run_id}.yaml")

    sub = sub_store.get("quality", TABLE, result.run_id)
    assert sub is not None
    assert sub.payload.get("schema")
    assert sub.status != "merged"

    prefix = f"scan/golden_merchant_seller_registry/{result.run_id}/"
    assert backend.exists("pii-reports", f"{prefix}run_manifest.json")


def test_merge_quality_contract_to_active_storage(
    merchant_df, backend, store, sub_store, run_dir,
):
    """automerge=quality publishes accepted contract to active-contracts (MinIO path)."""
    pytest.importorskip("great_expectations")
    result, _ = _run_scan_service(
        merchant_df,
        backend,
        store,
        sub_store,
        run_dir,
        automerge="quality",
    )
    assert result.status == "success"
    assert result.quality_contract_version

    active = store.get_active(TABLE)
    assert active is not None
    assert active["schema"][0]["physicalName"] == TABLE
    assert extract_rules(active)

    sub = sub_store.get("quality", TABLE, result.run_id)
    assert sub.status == "merged"
    assert sub.contract_uuid == active.get("contract_uuid")
    assert backend.exists(
        "active-contracts",
        f"active/{TABLE}.yaml",
    )


def test_regenerate_ge_suite_from_active_contract(
    merchant_df, backend, store, sub_store, run_dir,
):
    """Accepted ODCS quality rules round-trip to a GE ExpectationSuite dict."""
    pytest.importorskip("great_expectations")
    _run_scan_service(
        merchant_df,
        backend,
        store,
        sub_store,
        run_dir,
        automerge="quality",
    )
    active = store.get_active(TABLE)
    rules = extract_rules(active)
    ge_suite = regenerate(active, "great_expectations")
    assert isinstance(ge_suite, dict)
    assert rules
    assert ge_suite.get("expectations") or ge_suite.get("meta")


# ── CLI path (same as redibis scan --mode quality) ───────────────────────────


def _build_quality_cli_args(tmp_path: Path, csv_path: Path, **kwargs) -> SimpleNamespace:
    output_root = tmp_path / "scan_output"
    return SimpleNamespace(
        file=str(csv_path),
        table=kwargs.get("table", TABLE),
        mode=kwargs.get("mode", "quality"),
        automerge=kwargs.get("automerge", "none"),
        equation="independent",
        pii_engines="regex",
        gliner_model="",
        ner_model="",
        no_ge_docs=kwargs.get("no_ge_docs", True),
        no_validate=True,
        session_id=None,
        scan_output_dir=str(output_root),
        output_dir=str(output_root),
        use_s3=False,
        s3_endpoint=None,
        s3_runs_bucket="pii-reports",
        s3_contracts_bucket="active-contracts",
        s3_pii_runs_bucket="pii-contracts",
        s3_quality_runs_bucket="quality-contracts",
        config=None,
        profiler_engine=kwargs.get("profiler_engine", "great_expectations"),
    )


def test_cli_quality_scan_mode(merchant_sample_csv, tmp_path):
    """``redibis scan --mode quality`` on golden merchant CSV."""
    pytest.importorskip("great_expectations")
    args = _build_quality_cli_args(tmp_path, merchant_sample_csv)
    backend = _build_backend(args)
    store = ContractStore(backend, bucket=args.s3_contracts_bucket)
    sub_store = SubcontractStore(
        backend,
        pii_bucket=args.s3_pii_runs_bucket,
        quality_bucket=args.s3_quality_runs_bucket,
    )
    rc = _run_scan(args, store, backend)
    assert rc == 0

    output_root = Path(args.scan_output_dir)
    session_dir = next(d for d in output_root.iterdir() if (d / "session.json").is_file())
    run_id = json.loads((session_dir / "session.json").read_text())["latest_run_id"]
    artifacts_dir = session_dir / "runs" / run_id / "artifacts"

    _assert_quality_artifacts(artifacts_dir)
    manifest = json.loads(
        (session_dir / "runs" / run_id / "run_manifest.json").read_text(encoding="utf-8"),
    )
    assert manifest["status"] == "success"
    assert manifest["counts"]["quality_expectations"] > 0
    assert backend.exists("quality-contracts", f"{TABLE}/{run_id}.yaml")


def test_cli_quality_scan_openmetadata_profiler(merchant_sample_csv, tmp_path):
    """``redibis scan --mode quality --profiler-engine open_metadata``."""
    # OM profiles metrics; GE gatekeeper still validates expectations.
    pytest.importorskip("great_expectations")
    args = _build_quality_cli_args(
        tmp_path,
        merchant_sample_csv,
        profiler_engine="open_metadata",
    )
    backend = _build_backend(args)
    store = ContractStore(backend, bucket=args.s3_contracts_bucket)
    sub_store = SubcontractStore(
        backend,
        pii_bucket=args.s3_pii_runs_bucket,
        quality_bucket=args.s3_quality_runs_bucket,
    )
    rc = _run_scan(args, store, backend)
    assert rc == 0

    output_root = Path(args.scan_output_dir)
    session_dir = next(d for d in output_root.iterdir() if (d / "session.json").is_file())
    run_id = json.loads((session_dir / "session.json").read_text())["latest_run_id"]
    artifacts_dir = session_dir / "runs" / run_id / "artifacts"
    assert (artifacts_dir / "quality_contract.yaml").exists()
    assert backend.exists("quality-contracts", f"{TABLE}/{run_id}.yaml")
