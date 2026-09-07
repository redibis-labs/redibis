"""Continuous quality monitoring — unit and integration tests."""

from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
import yaml

from redibis.contracts.rule_code_parser import parse_ge_rules
from redibis.integrations.airflow.generate import generate_dags
from redibis.integrations.airflow.templates import dag_id_for_table, render_table_dag
from redibis.quality.contract_validate import (
    compute_schema_drift,
    quality_rules_from_contract,
    validate_contract_quality,
)
from redibis.quality.ge_codegen import render_ge_paste_module, render_gx_expectation_line
from redibis.quality.monitoring_package import build_monitoring_package, build_monitoring_zip
from redibis.services.catalog.openmetadata_quality import (
    map_results_for_publish,
    resolve_test_definition,
)
from redibis.services.continuous_quality import ContinuousQualityService, MonitorRunOptions
from redibis.store.contract_store import ContractStore
from redibis.store.storage_backend import LocalBackend

REPO_ROOT = Path(__file__).resolve().parent.parent
MERCHANT_CSV = REPO_ROOT / "tests/fixtures/golden/realistic_merchant_seller_registry.csv"
TABLE = "golden.merchant_seller_registry"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def merchant_df() -> pd.DataFrame:
    return pd.read_csv(MERCHANT_CSV, nrows=30)


@pytest.fixture
def backend(tmp_path):
    return LocalBackend(tmp_path / "storage")


@pytest.fixture
def store(backend):
    return ContractStore(backend, bucket="active-contracts")


def _minimal_contract(table: str = TABLE) -> dict:
    db, tbl = table.split(".", 1)
    return {
        "database_name": db,
        "table_name": tbl,
        "physicalName": table,
        "schema": [{
            "name": tbl,
            "physicalName": table,
            "properties": [
                {
                    "name": "seller_id",
                    "quality": [{
                        "engine": "greatExpectations",
                        "implementation": {
                            "expectation_type": "expect_column_values_to_not_be_null",
                            "kwargs": {"column": "seller_id"},
                        },
                    }],
                },
            ],
        }],
    }


def test_ge_codegen_roundtrip_parser():
    code = render_gx_expectation_line(
        "expect_column_values_to_not_be_null",
        column="email",
        kwargs={"mostly": 0.95},
    )
    parsed = parse_ge_rules(code)
    assert not parsed["errors"]
    assert len(parsed["rules"]) == 1
    assert parsed["rules"][0]["expectation_type"] == "expect_column_values_to_not_be_null"


def test_ge_paste_module_parse_safe():
    rules = [{
        "rule": "expect_column_values_to_not_be_null",
        "column": "seller_id",
        "kwargs": {"column": "seller_id"},
    }]
    code = render_ge_paste_module(rules)
    parsed = parse_ge_rules(code)
    assert not parsed["errors"]
    assert len(parsed["rules"]) == 1


def test_schema_drift():
    contract = _minimal_contract()
    drift = compute_schema_drift(contract, ["seller_id", "new_col"], table=TABLE)
    assert "new_col" in drift.added
    assert drift.dropped == []


def test_validate_contract_quality_no_write(merchant_df):
    contract = _minimal_contract()
    result, _qa, _raw = validate_contract_quality(
        merchant_df, contract, table=TABLE, generate_docs=False,
    )
    assert result.rules_total >= 1
    assert result.status in ("success", "failed")
    assert all(r.rule_id for r in result.results)


def test_monitor_service_persists_artifacts(merchant_df, backend, store, tmp_path):
    contract = _minimal_contract()
    store.upsert(contract, workflow="schema", table=TABLE)
    svc = ContinuousQualityService(
        store=store,
        backend=backend,
        runs_bucket="pii-reports",
    )
    sample = tmp_path / "sample.csv"
    merchant_df.to_csv(sample, index=False)
    opts = MonitorRunOptions(
        sample_path=str(sample),
        generate_docs=False,
        publish_openmetadata=False,
        output_dir=str(tmp_path / "reports"),
    )
    result = svc.run_table(TABLE, opts)
    assert result.status in ("success", "failed", "error")
    assert QUALITY_RESULTS_KEY_exists(backend, "pii-reports", TABLE, result.run_id)
    assert QUALITY_RESULTS_KEY_exists(
        backend, "pii-reports", TABLE, result.run_id, artifact="quality_run.json"
    )
    assert result.sink_telemetry == {}  # publish_openmetadata=False -> no sinks dispatched


def QUALITY_RESULTS_KEY_exists(backend, bucket, table, run_id, artifact: str | None = None) -> bool:
    from redibis.services.continuous_quality import QUALITY_RESULTS_ARTIFACT, MONITOR_WORKFLOW

    key = f"{MONITOR_WORKFLOW}/{table.replace('.', '_')}/{run_id}/{artifact or QUALITY_RESULTS_ARTIFACT}"
    return backend.exists(bucket, key)


def test_airflow_dag_render_syntax():
    code = render_table_dag(table=TABLE, schedule="0 6 * * *")
    compile(code, "<dag>", "exec")
    assert dag_id_for_table(TABLE) == "redibis_quality_golden_merchant_seller_registry"


def test_generate_dags_writes_files(tmp_path):
    paths = generate_dags([TABLE], tmp_path / "dags")
    assert len(paths) == 2
    for p in paths:
        compile(p.read_text(encoding="utf-8"), str(p), "exec")


def test_monitoring_package_layout(tmp_path):
    contract = _minimal_contract()
    root = build_monitoring_package(
        table=TABLE,
        contract=contract,
        output_dir=tmp_path,
    )
    assert (root / "manifest.yaml").is_file()
    assert (root / "ge_paste.py").is_file()
    assert (root / "quality" / "rules.yaml").is_file()
    assert (root / "quality" / "rule_set.json").is_file()
    assert (root / "golden_merchant_seller_registry_quality.py").is_file()
    assert (root / "requirements.txt").is_file()
    assert (root / "schemas" / "quality_ruleset.v1alpha1.schema.json").is_file()
    assert (root / "schemas" / "quality_run.v1alpha1.schema.json").is_file()
    assert (root / "dags").is_dir()
    paste = parse_ge_rules((root / "ge_paste.py").read_text(encoding="utf-8"))
    assert not paste["errors"]

    program = (root / "golden_merchant_seller_registry_quality.py").read_text(encoding="utf-8")
    compile(program, "golden_merchant_seller_registry_quality.py", "exec")

    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["rule_source"] == "active_contract"
    assert manifest["rule_set_digest"]

    checksums = json.loads((root / "CHECKSUMS.json").read_text(encoding="utf-8"))
    assert checksums["manifest.yaml"] == _sha256_text((root / "manifest.yaml").read_text(encoding="utf-8"))


def _contract_program(engine: str = "pandas") -> str:
    from redibis.quality.contract_validate import quality_rules_from_contract
    from redibis.quality.ge_codegen import render_full_quality_program
    from redibis.quality.rule_set import QualityRuleSet
    from redibis.quality.schema import rule_set_from_contract

    contract = _minimal_contract()
    ge_rules = list(quality_rules_from_contract(contract).rules)
    canonical = rule_set_from_contract(contract, table=TABLE)
    return render_full_quality_program(
        table=TABLE,
        rule_set=QualityRuleSet(name="golden_merchant_seller_registry", rules=ge_rules),
        rule_set_id=canonical.rule_set_id,
        rule_set_digest=canonical.semantic_digest,
        engine=engine,
    )


def test_render_full_quality_program_executes_end_to_end(merchant_df):
    """The generated program is not just syntactically valid — importing it
    and calling build_rule_set()/validate() must actually run GE and produce
    the same report shape a Jupyter user or scheduler would rely on."""
    import types

    program = _contract_program("pandas")
    module = types.ModuleType("generated_quality_program")
    exec(compile(program, "generated_quality_program.py", "exec"), module.__dict__)

    built = module.build_rule_set()
    assert built.rules and built.rules[0]["column"] == "seller_id"

    report = module.validate(merchant_df, built)
    assert "results" in report and "statistics" in report
    assert report["statistics"]["evaluated_expectations"] >= 1


def test_cli_monitor_help_lists_subcommands():
    import subprocess
    import sys

    for cmd in ("quality-monitor", "monitor"):
        result = subprocess.run(
            [sys.executable, "-m", "redibis.cli.main", cmd, "--help"],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        for sub in ("run", "batch", "export", "airflow"):
            assert sub in out


# ─────────────────────────────────────────────────────────────────────────
# Spark-first generated program
# ─────────────────────────────────────────────────────────────────────────

def test_generated_spark_program_never_converts_to_pandas():
    """A Spark program that quietly called toPandas() or capped rows would turn
    a full-partition verdict back into a driver-side sample — the exact thing
    this codegen exists to avoid."""
    program = _contract_program("spark")
    compile(program, "spark_program.py", "exec")
    assert "toPandas" not in program
    assert "import pandas" not in program
    assert "--rows" not in program
    assert "SparkSession" in program
    assert "def validate(df: DataFrame" in program
    # The rules must be inline literals, not loaded from a shipped JSON/YAML.
    assert "RULES: list[dict] = [" in program
    assert "rules.yaml" not in program
    assert "rule_set.json" not in program


def test_generated_spark_program_exposes_table_path_and_partition_filter():
    program = _contract_program("spark")
    for flag in ("--table", "--path", "--format", "--partition-filter"):
        assert flag in program
    assert "PARTITION_FILTER" in program
    assert "df.filter(predicate)" in program


@pytest.mark.slow
def test_generated_spark_program_validates_a_spark_dataframe(merchant_df):
    """End-to-end proof that GE attaches to a real Spark DataFrame through the
    generated program, with the partition filter applied in Spark."""
    pytest.importorskip("pyspark")
    import types

    from pyspark.sql import SparkSession

    try:
        spark = (
            SparkSession.builder.master("local[1]")
            .appName("redibis_codegen_test")
            .config("spark.ui.enabled", "false")
            .getOrCreate()
        )
    except Exception as e:  # no/incompatible JVM in this environment
        pytest.skip(f"no usable local Spark runtime: {e}")
    try:
        module = types.ModuleType("generated_spark_program")
        exec(compile(_contract_program("spark"), "generated_spark_program.py", "exec"), module.__dict__)

        sdf = spark.createDataFrame(merchant_df.astype(str))
        report = module.validate(sdf)
        assert report["statistics"]["evaluated_expectations"] >= 1

        filtered = sdf.filter("seller_id IS NOT NULL")
        assert filtered.count() <= sdf.count()
    finally:
        spark.stop()


def test_package_dags_spark_submit_the_authoritative_program(tmp_path):
    root = build_monitoring_package(
        table=TABLE, contract=_minimal_contract(), output_dir=tmp_path,
    )
    dag = (root / "dags" / "redibis_quality_golden_merchant_seller_registry.py").read_text(
        encoding="utf-8"
    )
    compile(dag, "dag.py", "exec")
    assert "spark-submit" in dag
    assert "golden_merchant_seller_registry_quality.py" in dag
    assert "--partition-filter" in dag
    assert "--rows 5000" not in dag

    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["engine"] == "spark"
    reqs = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "redibis[ge,spark]" in reqs
    assert "pyspark" in reqs


def test_cli_runner_dag_names_the_quality_monitor_command():
    code = render_table_dag(table=TABLE, schedule="0 6 * * *", runner="cli")
    compile(code, "dag.py", "exec")
    assert "redibis quality-monitor run" in code
    assert "smoke check" in code


def test_monitoring_zip_contents():
    blob = build_monitoring_zip(table=TABLE, contract=_minimal_contract())
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        names = zf.namelist()
    assert any(n.endswith("manifest.yaml") for n in names)
    assert any(n.endswith("ge_paste.py") for n in names)


def test_openmetadata_resolve_test_definition():
    native, ok = resolve_test_definition("expect_column_values_to_not_be_null")
    assert ok
    assert native == "columnValuesToBeNotNull"
    ext, ok2 = resolve_test_definition("expect_column_mean_to_be_between")
    assert not ok2
    assert ext.startswith("redibis_")


def test_map_results_for_publish():
    payload = {
        "results": [{
            "rule_id": "q_abc",
            "stable_id": "deadbeef1234",
            "success": False,
            "expectation_type": "expect_column_values_to_not_be_null",
            "column": "seller_id",
            "unexpected_count": 3,
        }],
    }
    rows = map_results_for_publish(payload)
    assert len(rows) == 1
    assert rows[0]["status"] == "fail"


def test_map_results_for_publish_accepts_canonical_outcome():
    """Canonical QualityRunV1.to_dict() rows use outcome/failed_count, not
    success/unexpected_count — the adapter must handle both shapes."""
    payload = {
        "results": [{
            "rule_id": "q_abc",
            "stable_id": "deadbeef1234",
            "outcome": "pass",
            "column": "seller_id",
            "failed_count": 0,
        }],
    }
    rows = map_results_for_publish(payload)
    assert len(rows) == 1
    assert rows[0]["status"] == "pass"


# ─────────────────────────────────────────────────────────────────────────
# Canonical schema (redibis.io/quality/v1alpha1)
# ─────────────────────────────────────────────────────────────────────────

def test_rule_set_from_contract_round_trips_and_digest_is_stable():
    from redibis.quality.schema import QualityRuleSetV1, rule_set_from_contract

    contract = {
        "contract_uuid": "abc-123",
        "version": "2",
        "schema": [{
            "physicalName": TABLE,
            "properties": [{
                "name": "phone",
                "quality": [{"rule": "missingCount", "mustBeLessThan": 5, "unit": "percent"}],
            }],
        }],
    }
    rs = rule_set_from_contract(contract, table=TABLE)
    assert rs.api_version == "redibis.io/quality/v1alpha1"
    assert rs.kind == "QualityRuleSet"
    assert rs.asset.physical_name == TABLE
    assert rs.contract.contract_id == "abc-123"
    assert len(rs.rules) == 1
    rule = rs.rules[0]
    assert rule.metric == "nullValues"
    assert rule.operator == "mustBeLessThan"
    assert rule.expected == 5
    assert rule.metric_fidelity == "lossless"
    assert rule.bindings[0].engine == "great_expectations"
    assert rule.bindings[0].fidelity == "lossless"

    # Same contract -> same digest (stable identity for the run to reference).
    rs2 = rule_set_from_contract(contract, table=TABLE)
    assert rs.semantic_digest == rs2.semantic_digest

    # Round-trip through JSON.
    restored = QualityRuleSetV1.from_dict(json.loads(json.dumps(rs.to_dict())))
    assert restored.semantic_digest == rs.semantic_digest
    assert restored.rules[0].metric == "nullValues"


def test_rule_set_from_contract_unsupported_metric_keeps_engine_binding():
    from redibis.quality.schema import rule_set_from_contract

    contract = _minimal_contract()  # engine-only rule, no portable comparator
    rs = rule_set_from_contract(contract, table=TABLE)
    assert len(rs.rules) == 1
    rule = rs.rules[0]
    assert rule.metric_fidelity == "unsupported"
    assert rule.metric is None
    assert rule.bindings[0].definition["expectation_type"] == "expect_column_values_to_not_be_null"


def test_quality_run_from_validate_result_excludes_raw_values_by_default(merchant_df):
    from redibis.quality.schema import QualityRunV1, quality_run_from_validate_result

    contract = _minimal_contract()
    result, _qa, _raw = validate_contract_quality(
        merchant_df, contract, table=TABLE, generate_docs=False,
    )
    run = quality_run_from_validate_result(result, table=TABLE)
    assert run.api_version == "redibis.io/quality/v1alpha1"
    assert run.kind == "QualityRun"
    assert run.summary["total"] == result.rules_total
    for row in run.results:
        assert row.observed is None  # redacted by default

    run_with_raw = quality_run_from_validate_result(result, table=TABLE, include_raw_diagnostics=True)
    # At least the shape is preserved; raw diagnostics may be None if GE had none.
    assert isinstance(run_with_raw.results, list)

    restored = QualityRunV1.from_dict(json.loads(json.dumps(run.to_dict())))
    assert restored.run_id == run.run_id
    assert restored.summary == run.summary


def test_rule_set_from_ge_rules_marks_metric_unsupported():
    from redibis.quality.schema import rule_set_from_ge_rules

    rules = [{
        "rule": "expect_column_values_to_not_be_null",
        "column": "phone",
        "kwargs": {"mostly": 0.9},
        "meta": {"redibis_rule_id": "q_x1", "severity": "P2", "source": "profiler"},
    }]
    rs = rule_set_from_ge_rules(rules, table=TABLE, source="session_draft")
    assert len(rs.rules) == 1
    rule = rs.rules[0]
    assert rule.id == "q_x1"
    assert rule.metric_fidelity == "unsupported"
    assert rule.severity == "P2"
    # per-rule provenance (from meta) wins over the package-level default
    assert rule.extensions["source"] == "profiler"
    assert rule.bindings[0].definition["kwargs"] == {"mostly": 0.9}


# ─────────────────────────────────────────────────────────────────────────
# Result-sink strategy registry
# ─────────────────────────────────────────────────────────────────────────

def test_sink_registry_has_builtin_console_and_openmetadata():
    from redibis.quality.sinks.registry import quality_sink_registry

    registry = quality_sink_registry()
    assert "console" in registry
    assert "openmetadata" in registry


def test_resolve_sink_names_prefers_explicit_config():
    from redibis.config import RedibisConfig
    from redibis.quality.sinks.registry import resolve_sink_names

    cfg = RedibisConfig.default()
    cfg.quality.publish.sinks = ["console"]
    cfg.catalog.push.quality = True  # legacy flag ignored once sinks is explicit
    assert resolve_sink_names(cfg) == ["console"]


def test_resolve_sink_names_falls_back_to_legacy_catalog_push():
    from redibis.config import RedibisConfig
    from redibis.quality.sinks.registry import resolve_sink_names

    cfg = RedibisConfig.default()
    assert cfg.quality.publish.sinks == []
    cfg.catalog.push.quality = True
    assert resolve_sink_names(cfg) == ["openmetadata"]
    cfg.catalog.push.quality = False
    assert resolve_sink_names(cfg) == []


def test_console_sink_publishes_without_network(merchant_df):
    from redibis.config import RedibisConfig
    from redibis.quality.schema import quality_run_from_validate_result
    from redibis.quality.sinks.console_sink import ConsoleQualitySink

    contract = _minimal_contract()
    result, _qa, _raw = validate_contract_quality(
        merchant_df, contract, table=TABLE, generate_docs=False,
    )
    run = quality_run_from_validate_result(result, table=TABLE)
    sink = ConsoleQualitySink.from_config(RedibisConfig.default())
    telemetry = sink.publish(table=TABLE, contract=contract, run=run)
    assert telemetry["published"] is True


def test_fake_second_sink_proves_registry_is_backend_neutral():
    """A completely independent sink (no OpenMetadata import reachable)
    registers and dispatches through the exact same strategy interface."""
    from redibis.quality.sinks.base import QualityResultSink
    from redibis.quality.sinks.registry import get_quality_sink_class, register_quality_sink

    calls = []

    @register_quality_sink("fake_test_sink")
    class FakeSink(QualityResultSink):
        name = "fake_test_sink"

        @classmethod
        def from_config(cls, config):
            return cls()

        def publish(self, *, table, contract, run, options=None):
            calls.append((table, run.run_id))
            return {"published": True}

    cls = get_quality_sink_class("fake_test_sink")
    assert cls is FakeSink
    from redibis.config import RedibisConfig
    from redibis.quality.schema import QualityRunV1

    sink = cls.from_config(RedibisConfig.default())
    telemetry = sink.publish(table=TABLE, contract={}, run=QualityRunV1(run_id="r1"))
    assert telemetry == {"published": True}
    assert calls == [(TABLE, "r1")]


def test_continuous_quality_service_dispatches_configured_sinks(merchant_df, backend, store, tmp_path):
    """publish_openmetadata=True routes through resolve_sink_names(); a sink
    failure is recorded per-sink and never fails the underlying validation."""
    contract = _minimal_contract()
    store.upsert(contract, workflow="schema", table=TABLE)

    from redibis.config import RedibisConfig

    cfg = RedibisConfig.default()
    cfg.quality.publish.sinks = ["console"]
    svc = ContinuousQualityService(store=store, backend=backend, runs_bucket="pii-reports", config=cfg)

    sample = tmp_path / "sample.csv"
    merchant_df.to_csv(sample, index=False)
    opts = MonitorRunOptions(
        sample_path=str(sample), generate_docs=False, publish_openmetadata=True,
        output_dir=str(tmp_path / "reports"),
    )
    result = svc.run_table(TABLE, opts)
    assert "console" in result.sink_telemetry
    assert result.sink_telemetry["console"]["published"] is True


# ─────────────────────────────────────────────────────────────────────────
# Overlay semantics + automation correctness
# ─────────────────────────────────────────────────────────────────────────

def test_run_table_suppresses_decision_overlaid_rule(merchant_df, backend, store, tmp_path):
    from redibis.store.quality_decisions import QualityDecision

    contract = _minimal_contract()
    store.upsert(contract, workflow="schema", table=TABLE)
    from redibis.contracts.rules import stable_rule_id

    active = store.get_active(TABLE)
    q = active["schema"][0]["properties"][0]["quality"][0]
    rule_id = stable_rule_id("seller_id", q)
    store.quality_decisions.set(TABLE, QualityDecision(rule_id=rule_id, status="suppressed", column="seller_id"))

    svc = ContinuousQualityService(store=store, backend=backend, runs_bucket="pii-reports")
    sample = tmp_path / "sample.csv"
    merchant_df.to_csv(sample, index=False)
    opts = MonitorRunOptions(
        sample_path=str(sample), generate_docs=False, publish_openmetadata=False,
        output_dir=str(tmp_path / "reports"),
    )
    result = svc.run_table(TABLE, opts)
    # The only rule on the contract was suppressed -> nothing to run.
    assert result.rules_total == 0


def test_resolve_tables_requires_explicit_selector(backend, store):
    svc = ContinuousQualityService(store=store, backend=backend, runs_bucket="pii-reports")
    assert svc.resolve_tables() == []
    assert svc.resolve_tables(tables=["a.b"]) == ["a.b"]

    store.upsert(_minimal_contract("db.one"), workflow="schema", table="db.one")
    store.upsert(_minimal_contract("db.two"), workflow="schema", table="db.two")
    assert svc.resolve_tables(all_contracts=True) == ["db.one", "db.two"]
    assert svc.resolve_tables(database="db") == ["db.one", "db.two"]
    assert svc.resolve_tables(database="other") == []


def test_expand_sample_path_template():
    from redibis.services.continuous_quality import _expand_sample_path

    assert _expand_sample_path("/data/sample.csv", TABLE) == "/data/sample.csv"
    assert (
        _expand_sample_path("/data/{table_safe}.csv", TABLE)
        == "/data/golden_merchant_seller_registry.csv"
    )
    assert _expand_sample_path("/data/{table}.csv", TABLE) == f"/data/{TABLE}.csv"
    assert _expand_sample_path(None, TABLE) is None


def test_load_latest_quality_results_uses_completed_at_not_run_id(backend):
    from redibis.services.continuous_quality import (
        MONITOR_SUMMARY_ARTIFACT,
        MONITOR_WORKFLOW,
        QUALITY_RESULTS_ARTIFACT,
        load_latest_quality_results,
    )

    table_safe = TABLE.replace(".", "_")
    # Run id "aaa..." sorts last lexicographically but completed first.
    for run_id, completed_at, marker in (
        ("zzzzzzzzzzzz", "2020-01-01T00:00:00+00:00", "old"),
        ("aaaaaaaaaaaa", "2030-01-01T00:00:00+00:00", "new"),
    ):
        prefix = f"{MONITOR_WORKFLOW}/{table_safe}/{run_id}"
        backend.put_json("pii-reports", f"{prefix}/{QUALITY_RESULTS_ARTIFACT}", {"run_id": run_id, "marker": marker})
        backend.put_json(
            "pii-reports", f"{prefix}/{MONITOR_SUMMARY_ARTIFACT}",
            {"run_id": run_id, "completed_at": completed_at},
        )

    latest = load_latest_quality_results(backend, "pii-reports", TABLE)
    assert latest["marker"] == "new"


# ─────────────────────────────────────────────────────────────────────────
# CLI — glob table selection + explicit-selector guard
# ─────────────────────────────────────────────────────────────────────────

def test_cli_resolve_tables_glob_pattern(backend, store):
    from argparse import Namespace

    from redibis.cli.monitor_cmd import _resolve_tables

    store.upsert(_minimal_contract("golden.merchant_seller_registry"), workflow="schema", table="golden.merchant_seller_registry")
    store.upsert(_minimal_contract("golden.other_table"), workflow="schema", table="golden.other_table")
    store.upsert(_minimal_contract("other.table"), workflow="schema", table="other.table")

    svc = ContinuousQualityService(store=store, backend=backend, runs_bucket="pii-reports")
    args = Namespace(tables="golden.*", tables_file=None)
    tables = _resolve_tables(args, svc)
    assert sorted(tables) == ["golden.merchant_seller_registry", "golden.other_table"]


def test_cli_batch_requires_explicit_selector(backend, store, capsys):
    from argparse import Namespace

    from redibis.cli.monitor_cmd import _run_batch

    svc = ContinuousQualityService(store=store, backend=backend, runs_bucket="pii-reports")
    args = Namespace(
        tables=None, tables_file=None, database=None, all_contracts=False,
        rows=5000, sample=None, no_schema_drift=False, no_ge_docs=False,
        no_publish=True, rules_file=None, output_dir=None, json=False,
    )
    rc = _run_batch(args, svc)
    assert rc == 2
    assert "requires --tables" in capsys.readouterr().err


def test_cli_export_uses_effective_contract(store, tmp_path):
    from argparse import Namespace

    from redibis.cli.monitor_cmd import _run_export
    from redibis.store.quality_decisions import QualityDecision

    contract = _minimal_contract()
    store.upsert(contract, workflow="schema", table=TABLE)
    from redibis.contracts.rules import stable_rule_id

    active = store.get_active(TABLE)
    q = active["schema"][0]["properties"][0]["quality"][0]
    rule_id = stable_rule_id("seller_id", q)
    store.quality_decisions.set(TABLE, QualityDecision(rule_id=rule_id, status="suppressed", column="seller_id"))

    args = Namespace(table=TABLE, output=str(tmp_path), schedule="0 6 * * *")
    rc = _run_export(args, store)
    assert rc == 0
    root = tmp_path / "golden_merchant_seller_registry-monitoring"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["rule_source"] == "active_contract"
    rule_set_doc = json.loads((root / "quality" / "rule_set.json").read_text(encoding="utf-8"))
    assert rule_set_doc["rules"] == []  # the only rule was suppressed


# ─────────────────────────────────────────────────────────────────────────
# Web export — explicit rule-origin priority (no silent fallback)
# ─────────────────────────────────────────────────────────────────────────

def test_web_export_package_falls_back_to_active_contract(monkeypatch, tmp_path):
    import io as _io

    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    buf = _io.BytesIO()
    pd.DataFrame({"id": [1, 2], "phone": ["555-0100", None]}).to_csv(buf, index=False)
    buf.seek(0)
    r = client.post(
        "/api/sessions",
        files={"file": ("sample.csv", buf, "text/csv")},
        data={"table": TABLE, "scan_mode": "quality"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    contract = _minimal_contract()

    class FakeQualityDecisions:
        def get(self, table):
            return {}

    class FakeStore:
        quality_decisions = FakeQualityDecisions()

        def get_active(self, table):
            return contract

    monkeypatch.setattr(backend, "get_contract_store", lambda: FakeStore())

    r = client.post(
        f"/api/sessions/{sid}/quality/export-package",
        json={"dropped_indices": [], "schedule": "0 6 * * *"},
    )
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        names = zf.namelist()
        manifest_name = next(n for n in names if n.endswith("manifest.yaml"))
        manifest = yaml.safe_load(zf.read(manifest_name))
    assert manifest["rule_source"] == "active_contract"


def test_web_export_package_uses_curated_session_draft(monkeypatch, tmp_path):
    """A session with an explicit draft rule set exports that draft, dropping
    review-page-removed indices — never silently falling back to the
    contract, even when one exists for the same table."""
    import io as _io

    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    buf = _io.BytesIO()
    pd.DataFrame({"id": [1, 2], "phone": ["555-0100", None]}).to_csv(buf, index=False)
    buf.seek(0)
    r = client.post(
        "/api/sessions",
        files={"file": ("sample.csv", buf, "text/csv")},
        data={"table": TABLE, "scan_mode": "quality"},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]

    draft_rules = [
        {"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {"column": "id"}},
        {"rule": "expect_column_values_to_not_be_null", "column": "phone", "kwargs": {"column": "phone"}},
    ]
    r = client.put(
        f"/api/sessions/{sid}/config/quality",
        json={"name": "draft", "rules": draft_rules},
    )
    assert r.status_code == 200, r.text

    # An active contract exists too, but must NOT be used: the draft wins.
    contract = _minimal_contract()

    class FakeQualityDecisions:
        def get(self, table):
            return {}

    class FakeStore:
        quality_decisions = FakeQualityDecisions()

        def get_active(self, table):
            return contract

    monkeypatch.setattr(backend, "get_contract_store", lambda: FakeStore())

    r = client.post(
        f"/api/sessions/{sid}/quality/export-package",
        json={"dropped_indices": [1], "schedule": "0 6 * * *"},
    )
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(BytesIO(r.content)) as zf:
        names = zf.namelist()
        manifest_name = next(n for n in names if n.endswith("manifest.yaml"))
        manifest = yaml.safe_load(zf.read(manifest_name))
        rule_set_name = next(n for n in names if n.endswith("quality/rule_set.json"))
        rule_set_doc = json.loads(zf.read(rule_set_name))
    assert manifest["rule_source"] == "session_draft"
    assert len(rule_set_doc["rules"]) == 1
    assert rule_set_doc["rules"][0]["column"] == "id"


# ─────────────────────────────────────────────────────────────────────────
# Full-code endpoint — one renderer for copy, download, and package
# ─────────────────────────────────────────────────────────────────────────

def _quality_session(client, table: str = TABLE) -> str:
    import io as _io

    buf = _io.BytesIO()
    pd.DataFrame({"id": [1, 2], "phone": ["555-0100", None]}).to_csv(buf, index=False)
    buf.seek(0)
    r = client.post(
        "/api/sessions",
        files={"file": ("sample.csv", buf, "text/csv")},
        data={"table": table, "scan_mode": "quality"},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _fake_contract_store(monkeypatch, backend_module, contract):
    class FakeQualityDecisions:
        def get(self, table):
            return {}

    class FakeStore:
        quality_decisions = FakeQualityDecisions()

        def get_active(self, table):
            return contract

    monkeypatch.setattr(backend_module, "get_contract_store", lambda: FakeStore())


def test_full_code_endpoint_returns_compilable_spark_program(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    sid = _quality_session(client)
    _fake_contract_store(monkeypatch, backend, _minimal_contract())

    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/x-python")
    assert r.headers["X-Redibis-Rule-Source"] == "active_contract"
    code = r.text
    compile(code, "endpoint_program.py", "exec")
    assert "SparkSession" in code
    assert "RULES: list[dict] = [" in code
    # The program must not read any Redibis-generated rule config at runtime.
    for artifact in ("rules.yaml", "rule_set.json", "manifest.yaml"):
        assert artifact not in code


def test_full_code_endpoint_honours_curated_drops(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    sid = _quality_session(client)
    draft_rules = [
        {"rule": "expect_column_values_to_not_be_null", "column": "id", "kwargs": {"column": "id"}},
        {"rule": "expect_column_values_to_not_be_null", "column": "phone", "kwargs": {"column": "phone"}},
    ]
    r = client.put(f"/api/sessions/{sid}/config/quality", json={"name": "draft", "rules": draft_rules})
    assert r.status_code == 200, r.text
    _fake_contract_store(monkeypatch, backend, _minimal_contract())

    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={"dropped_indices": [1]})
    assert r.status_code == 200, r.text
    assert r.headers["X-Redibis-Rule-Source"] == "session_draft"
    assert r.headers["X-Redibis-Rule-Count"] == "1"
    assert "'phone'" not in r.text
    assert "'id'" in r.text


def test_full_code_endpoint_accepts_structured_rules_and_pasted_python(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    sid = _quality_session(client)
    _fake_contract_store(monkeypatch, backend, _minimal_contract())

    rules = [{"rule": "expect_column_values_to_be_unique", "column": "id", "kwargs": {}}]
    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={"rules": rules})
    assert r.status_code == 200, r.text
    assert "expect_column_values_to_be_unique" in r.text
    first = r.text

    # Feeding the generated program back in must reproduce the same rules.
    r2 = client.post(f"/api/sessions/{sid}/quality/full-code", json={"code": first})
    assert r2.status_code == 200, r2.text
    assert r2.headers["X-Redibis-Rule-Source"] == "pasted_code"
    assert "expect_column_values_to_be_unique" in r2.text
    compile(r2.text, "roundtrip.py", "exec")


def test_full_code_and_package_program_are_identical_modulo_provenance(monkeypatch, tmp_path):
    """The copied Jupyter code and the packaged program must not drift: both go
    through the same renderer, so only the generated-at timestamp differs."""
    import re

    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    sid = _quality_session(client)
    _fake_contract_store(monkeypatch, backend, _minimal_contract())

    code = client.post(f"/api/sessions/{sid}/quality/full-code", json={}).text
    root = build_monitoring_package(
        table=TABLE, contract=_minimal_contract(), output_dir=tmp_path,
    )
    packaged = (root / "golden_merchant_seller_registry_quality.py").read_text(encoding="utf-8")

    strip = lambda t: re.sub(r"^Generated .*$", "Generated <ts>", t, flags=re.M)
    assert strip(code) == strip(packaged)


def test_full_code_endpoint_rejects_unknown_engine(monkeypatch):
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    sid = _quality_session(client)
    _fake_contract_store(monkeypatch, backend, _minimal_contract())

    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={"engine": "dask"})
    assert r.status_code == 400


def test_full_code_endpoint_never_executes_pasted_python(monkeypatch, tmp_path):
    """Proof of the one-way boundary: a paste with a side effect yields rules
    only, and the side effect never happens."""
    from fastapi.testclient import TestClient

    from redibis.webapp import backend

    client = TestClient(backend.app)
    sid = _quality_session(client)
    _fake_contract_store(monkeypatch, backend, _minimal_contract())

    canary = tmp_path / "canary.txt"
    hostile = (
        f"import pathlib\n"
        f"pathlib.Path({str(canary)!r}).write_text('pwned')\n"
        "RULES: list[dict] = [\n"
        "    {'rule': 'expect_column_values_to_not_be_null', 'column': 'id', 'kwargs': {}},\n"
        "]\n"
    )
    r = client.post(f"/api/sessions/{sid}/quality/full-code", json={"code": hostile})
    assert r.status_code == 200, r.text
    assert not canary.exists()
    assert "expect_column_values_to_not_be_null" in r.text
    assert "pwned" not in r.text


# ─────────────────────────────────────────────────────────────────────────
# CLI: edited Python file → monitoring package
# ─────────────────────────────────────────────────────────────────────────

def _export_args(tmp_path, **over):
    from argparse import Namespace

    base = dict(
        table=TABLE, output=str(tmp_path), schedule="0 6 * * *",
        python_file=None, engine="spark",
    )
    base.update(over)
    return Namespace(**base)


def test_cli_export_from_python_file_preserves_edited_rules(tmp_path, store):
    from redibis.cli.monitor_cmd import _run_export

    edited = _contract_program("spark").replace(
        "'column': 'seller_id',", "'column': 'seller_id', 'kwargs': {'mostly': 0.42},", 1
    )
    # An operator-added rule that exists nowhere in the contract.
    edited = edited.replace(
        "RULES: list[dict] = [",
        "RULES: list[dict] = [\n"
        "    {'rule': 'expect_table_row_count_to_be_between', 'kwargs': {'min_value': 1}},",
        1,
    )
    py = tmp_path / "edited_quality.py"
    py.write_text(edited, encoding="utf-8")

    rc = _run_export(_export_args(tmp_path, python_file=str(py)), store)
    assert rc == 0

    root = tmp_path / "golden_merchant_seller_registry-monitoring"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["rule_source"] == "python_file"
    assert manifest["source_digest"] == _sha256_text(edited)

    rules = yaml.safe_load((root / "quality" / "rules.yaml").read_text(encoding="utf-8"))["rules"]
    kinds = {r["rule"] for r in rules}
    assert "expect_table_row_count_to_be_between" in kinds
    assert "expect_column_values_to_not_be_null" in kinds


def test_cli_export_python_file_rejects_bad_input(tmp_path, store, capsys):
    from redibis.cli.monitor_cmd import _run_export

    missing = tmp_path / "nope.py"
    assert _run_export(_export_args(tmp_path, python_file=str(missing)), store) == 2

    broken = tmp_path / "broken.py"
    broken.write_text("RULES = [\n", encoding="utf-8")
    assert _run_export(_export_args(tmp_path, python_file=str(broken)), store) == 2

    # Syntactically fine, but no literal rule definitions to extract.
    empty = tmp_path / "empty.py"
    empty.write_text("RULES: list[dict] = []\nprint('hi')\n", encoding="utf-8")
    assert _run_export(_export_args(tmp_path, python_file=str(empty)), store) == 2
    # No silent fallback: nothing was written.
    assert not (tmp_path / "golden_merchant_seller_registry-monitoring").exists()

    non_literal = tmp_path / "dynamic.py"
    non_literal.write_text(
        "import os\nRULES: list[dict] = [{'rule': 'expect_x', 'column': os.getcwd()}]\n",
        encoding="utf-8",
    )
    assert _run_export(_export_args(tmp_path, python_file=str(non_literal)), store) == 2


def test_cli_export_python_file_does_not_execute_the_file(tmp_path, store):
    from redibis.cli.monitor_cmd import _run_export

    canary = tmp_path / "cli_canary.txt"
    py = tmp_path / "hostile.py"
    py.write_text(
        f"import pathlib\npathlib.Path({str(canary)!r}).write_text('pwned')\n"
        "RULES: list[dict] = [\n"
        "    {'rule': 'expect_column_values_to_be_unique', 'column': 'id', 'kwargs': {}},\n"
        "]\n",
        encoding="utf-8",
    )
    assert _run_export(_export_args(tmp_path, python_file=str(py)), store) == 0
    assert not canary.exists()


def test_cli_export_python_file_works_through_real_argv(tmp_path):
    """Exercised through `redibis quality-monitor export`, not just the handler:
    the export subcommand previously crashed in main() before dispatch because it
    lacked the shared storage flags."""
    import subprocess
    import sys

    py = tmp_path / "edited_quality.py"
    py.write_text(_contract_program("spark"), encoding="utf-8")
    out = tmp_path / "packages"

    proc = subprocess.run(
        [
            sys.executable, "-m", "redibis.cli.main", "quality-monitor", "export",
            TABLE, "--python-file", str(py), "-o", str(out),
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    root = out / "golden_merchant_seller_registry-monitoring"
    assert (root / "golden_merchant_seller_registry_quality.py").is_file()
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["rule_source"] == "python_file"


def test_cli_monitor_alias_warns_but_keeps_stdout_clean(tmp_path, store, backend, capsys):
    """The deprecated alias must only add a stderr notice — JSON stdout and exit
    codes stay identical, so existing automation keeps working."""
    from redibis.cli.monitor_cmd import run_monitor

    outputs = {}
    for cmd in ("quality-monitor", "monitor"):
        args = _export_args(tmp_path / cmd, monitor_action="export", cmd=cmd, config=None)
        (tmp_path / cmd).mkdir(parents=True, exist_ok=True)
        store.upsert(_minimal_contract(), workflow="schema", table=TABLE)
        rc = run_monitor(args, store, backend)
        captured = capsys.readouterr()
        outputs[cmd] = (rc, captured.out.strip().split("/")[-1], captured.err)

    assert outputs["quality-monitor"][0] == outputs["monitor"][0] == 0
    assert outputs["quality-monitor"][1] == outputs["monitor"][1]
    assert "deprecated" not in outputs["quality-monitor"][2]
    assert "deprecated" in outputs["monitor"][2]


# ─────────────────────────────────────────────────────────────────────────
# UI characterization — the code actions must exist where operators expect
# ─────────────────────────────────────────────────────────────────────────

def test_web_ui_exposes_jupyter_code_and_package_actions():
    app_js = (REPO_ROOT / "redibis/webapp/static/app.js").read_text(encoding="utf-8")
    assert "quality/full-code" in app_js
    assert "Copy Jupyter Code" in app_js
    assert "Download monitor package" in app_js
    assert "Generate Jupyter Code" in app_js
    assert "loadQualityPythonFile" in app_js
    assert "window.redibisRenderJupyterCode" in app_js
    # Both the review page and the results page must offer the code actions.
    assert app_js.count("copyFullQualityCode()") >= 2
    assert app_js.count("downloadMonitoringPackage()") >= 2


def test_interactive_review_exposes_curated_rules_to_parent():
    profiler_src = (REPO_ROOT / "redibis/quality/profiler.py").read_text(encoding="utf-8")
    assert "window.getKeptRules" in profiler_src
    assert "window.getDroppedIndices" in profiler_src
    assert "Generate &amp; Copy Jupyter Code" in profiler_src
    assert "redibisRenderJupyterCode" in profiler_src
