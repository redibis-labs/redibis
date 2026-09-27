"""Validate once, store anywhere, let consumers decide — across every result store.

Backends: local Parquet and SQLite always; Postgres when REDIBIS_TEST_POSTGRES_URL is
set; S3/MinIO when REDIBIS_TEST_S3_URI is set; Iceberg when PyIceberg's SQL catalog
is usable (PyIceberg + SQLAlchemy 2).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date

import pandas as pd
import pytest
import yaml

pytest.importorskip("great_expectations")

from redibis.cli.main import main as cli
from redibis.quality.authoring import draft_from_rules
from redibis.quality.results import (
    ConsumerPolicy,
    get_result_store,
    load_policies,
    rules_catalog,
    validate_partition,
)
from redibis.quality.results.records import partition_date_of, partition_spec
from redibis.quality.sql_rules import sql_rule

TABLE = "shop.orders"
DAYS = {"dt=2026-09-24": "good", "dt=2026-09-25": "good", "dt=2026-09-26": "bad"}


@pytest.fixture(autouse=True)
def _quiet():
    logging.getLogger("great_expectations").setLevel(logging.ERROR)


def orders(kind: str) -> pd.DataFrame:
    df = pd.DataFrame({
        "order_id": [f"O{i:04d}" for i in range(40)],
        "amount": [10.0 + i for i in range(40)],
        "channel": ["web", "app", "store", "web"] * 10,
        "payment": ["card", "wallet", "cash", "card"] * 10,
    })
    if kind == "bad":
        df.loc[3, "amount"] = -5.0
        df.loc[5, "order_id"] = None
        df.loc[0, "payment"] = "cash"          # web + cash
    return df


def contract() -> dict:
    d = draft_from_rules(TABLE, [
        {"rule": "expect_column_values_to_not_be_null", "column": "order_id", "kwargs": {}},
        {"rule": "expect_column_values_to_be_between", "column": "amount", "kwargs": {"min_value": 0}},
        {"rule": "expect_column_values_to_be_in_set", "column": "channel",
         "kwargs": {"value_set": ["web", "app", "store"]}},
        {"rule": "expect_table_row_count_to_be_between", "column": None,
         "kwargs": {"min_value": 10, "max_value": 100}},
    ])
    d.set_severity("P2", columns=["channel"])
    return d.payload


def _iceberg_uri(tmp_path):
    try:
        from pyiceberg.catalog.sql import SqlCatalog
    except Exception:  # noqa: BLE001
        pytest.skip("PyIceberg SQL catalog not available (needs pyiceberg + SQLAlchemy 2)")
    catalog = SqlCatalog("test", uri=f"sqlite:///{tmp_path}/catalog.db", warehouse=f"file://{tmp_path}/wh")
    return "iceberg://test?namespace=dq", {"catalog": catalog}


@pytest.fixture(params=["file", "sqlite", "postgres", "s3", "iceberg"])
def store(request, tmp_path):
    kind = request.param
    if kind == "file":
        return get_result_store(f"file://{tmp_path}/dq")
    if kind == "sqlite":
        return get_result_store(f"sqlite:///{tmp_path}/dq.db")
    if kind == "postgres":
        url = os.environ.get("REDIBIS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("set REDIBIS_TEST_POSTGRES_URL to test the Postgres store")
        return get_result_store(f"{url}?schema=dq_{uuid.uuid4().hex[:8]}")
    if kind == "s3":
        base = os.environ.get("REDIBIS_TEST_S3_URI")
        if not base:
            pytest.skip("set REDIBIS_TEST_S3_URI (e.g. s3://bucket/prefix?endpoint=http://minio:9000)")
        path, _, query = base.partition("?")
        return get_result_store(f"{path.rstrip('/')}/{uuid.uuid4().hex[:8]}" + (f"?{query}" if query else ""))
    uri, options = _iceberg_uri(tmp_path)
    return get_result_store(uri, **options)


@pytest.fixture()
def finance() -> ConsumerPolicy:
    return ConsumerPolicy.from_dict({
        "consumer": "finance", "table": TABLE, "window": {"last_days": 2},
        "rules": {"severities": ["P1"]},
        "custom_sql": [{"query": "SELECT COUNT(*) FROM ${object} WHERE channel = 'web' AND payment = 'cash'",
                        "description": "no cash on web"}],
    })


def _load(store, policies=()):
    for partition, kind in DAYS.items():
        validate_partition(orders(kind), TABLE, partition=partition, contract=contract(), store=store,
                           fingerprint=kind + partition, policies=list(policies))


# ── producer ─────────────────────────────────────────────────────────────────

def test_a_partition_is_validated_once(store, finance):
    first = validate_partition(orders("good"), TABLE, partition="2026-09-25", contract=contract(),
                               store=store, fingerprint="snap-1", policies=[finance])
    again = validate_partition(orders("good"), TABLE, partition="2026-09-25", contract=contract(),
                               store=store, fingerprint="snap-1", policies=[finance])
    assert not first.skipped and again.skipped and again.run_id == first.run_id
    assert first.status == "passed" and first.rules_total == 5 and first.partition_date == date(2026, 9, 25)

    new_data = validate_partition(orders("bad"), TABLE, partition="2026-09-25", contract=contract(),
                                  store=store, fingerprint="snap-2", policies=[finance])
    assert not new_data.skipped and new_data.status == "failed" and new_data.p1_failed == 3
    forced = validate_partition(orders("bad"), TABLE, partition="2026-09-25", contract=contract(),
                                store=store, fingerprint="snap-2", policies=[finance], force=True)
    assert not forced.skipped and forced.run_id != new_data.run_id
    fewer_rules = validate_partition(orders("bad"), TABLE, partition="2026-09-25", contract=contract(),
                                     store=store, fingerprint="snap-2")
    assert not fewer_rules.skipped and fewer_rules.rule_set_digest != new_data.rule_set_digest
    assert len(store.runs(TABLE, partitions=["2026-09-25"])) == 4
    assert len(store.runs(TABLE, partitions=["2026-09-25"], latest_only=True)) == 1


def test_rule_set_is_stored_for_consumers(store, finance):
    _load(store, [finance])
    rules = rules_catalog(store, TABLE)
    assert len(rules) == 5
    owners = dict(zip(rules["expectation"], rules["owner"]))
    assert owners["sql"] == "consumer:finance"
    assert owners["expect_column_values_to_be_between"] == "contract"
    assert set(rules_catalog(table=TABLE, contract=contract())["rule_id"]) < set(rules["rule_id"])


# ── consumers ────────────────────────────────────────────────────────────────

def test_window_and_requirement(store, finance):
    _load(store, [finance])
    ok = finance.evaluate(store, as_of=date(2026, 9, 25))          # 23 (missing), 24, 25
    assert not ok.accepted and [v.status for v in ok.verdicts] == ["missing", "passed", "passed"]
    ok.on_missing = "ignore"
    finance.on_missing = "ignore"
    assert finance.evaluate(store, as_of=date(2026, 9, 25)).accepted

    bad = finance.evaluate(store, as_of=date(2026, 9, 26))
    assert not bad.accepted and bad.accepted_partitions == ["dt=2026-09-24", "dt=2026-09-25"]
    failed = {f["rule"] for f in bad.verdicts[-1].failures}
    assert failed == {"sql", "expect_column_values_to_not_be_null", "expect_column_values_to_be_between"}
    assert bad.partition_filter("dt") == "dt IN ('2026-09-24', '2026-09-25')"

    finance.require = "any"
    assert finance.evaluate(store, as_of=date(2026, 9, 26)).accepted
    finance.require = "latest"
    assert not finance.evaluate(store, as_of=date(2026, 9, 26)).accepted


def test_other_windows(store):
    _load(store)
    today = ConsumerPolicy.from_dict({"consumer": "ml", "table": TABLE, "window": {"today": True}})
    assert today.evaluate(store, as_of=date(2026, 9, 25)).accepted
    assert not today.evaluate(store, as_of=date(2026, 9, 26)).accepted
    assert not today.evaluate(store, as_of=date(2026, 9, 27)).accepted          # not validated yet
    since = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE, "window": {"since": "2026-09-24"}})
    assert len(since.evaluate(store, as_of=date(2026, 9, 25)).verdicts) == 2
    explicit = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE,
                                         "window": {"partitions": ["dt=2026-09-24", "dt=2026-09-30"]}})
    assert [v.status for v in explicit.evaluate(store).verdicts] == ["passed", "missing"]
    latest = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE, "window": {"latest": 2},
                                       "require": "any"})
    d = latest.evaluate(store)
    assert [v.partition for v in d.verdicts] == ["dt=2026-09-25", "dt=2026-09-26"] and d.accepted


def test_each_consumer_checks_only_its_rules(store):
    _load(store)
    ids = rules_catalog(store, TABLE)
    by_exp = dict(zip(ids["expectation"].replace("", None).fillna(ids["rule_type"]), ids["rule_id"]))
    lenient = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE, "window": {"today": True},
                                        "rules": {"ids": [by_exp["row_count"]]}})
    assert lenient.evaluate(store, as_of=date(2026, 9, 26)).accepted            # bad day, row count fine
    typed = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE, "window": {"today": True},
                                      "rules": {"types": ["expect_column_values_to_be_in_set"],
                                                "columns": ["channel"]}})
    d = typed.evaluate(store, as_of=date(2026, 9, 26))
    assert d.accepted and d.rules_checked == 1
    table_level = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE, "window": {"today": True},
                                            "rules": {"columns": [None]}})
    assert table_level.evaluate(store, as_of=date(2026, 9, 26)).rules_checked == 1
    typo = ConsumerPolicy.from_dict({"consumer": "bi", "table": TABLE, "window": {"today": True},
                                     "rules": {"columns": ["amuont"]}})
    v = typo.evaluate(store, as_of=date(2026, 9, 25))
    assert not v.accepted and v.verdicts[0].status == "incomplete"


def test_a_new_custom_rule_is_incomplete_until_revalidated(store, finance):
    _load(store)                                   # validated before finance registered its SQL
    d = finance.evaluate(store, as_of=date(2026, 9, 25))
    assert not d.accepted and {v.status for v in d.verdicts[1:]} == {"incomplete"}
    validate_partition(orders("good"), TABLE, partition="dt=2026-09-25", contract=contract(),
                       store=store, fingerprint="gooddt=2026-09-25", policies=[finance])
    assert finance.evaluate(store, as_of=date(2026, 9, 25)).verdicts[2].status == "passed"


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_policy_files_and_store_resolution(tmp_path, monkeypatch, finance):
    folder = tmp_path / "policies"
    folder.mkdir()
    (folder / "finance.yaml").write_text(yaml.safe_dump(finance.to_dict()))
    (folder / "other.yml").write_text(yaml.safe_dump({"consumer": "x", "table": "shop.users"}))
    assert [p.consumer for p in load_policies(folder, table=TABLE)] == ["finance"]
    back = ConsumerPolicy.from_yaml(folder / "finance.yaml")
    assert back.custom_rule_ids() == finance.custom_rule_ids() and back.window.last_days == 2
    with pytest.raises(ValueError, match="require"):
        ConsumerPolicy.from_dict({"consumer": "x", "table": TABLE, "require": "most"})
    with pytest.raises(ValueError, match="no quality result store"):
        get_result_store("ftp://nowhere")
    monkeypatch.setenv("REDIBIS_QUALITY_RESULTS_STORE", f"file://{tmp_path}/env")
    assert get_result_store().uri.endswith("/env")
    assert partition_date_of("region=eg/dt=2026-09-26") == date(2026, 9, 26)
    assert partition_date_of("batch-17") is None
    assert sql_rule("SELECT 1")   # custom SQL uses the same rule helper as contracts


def test_spark_partition(tmp_path):
    pytest.importorskip("pyspark")
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder.master("local[1]").appName("redibis-results-test")
             .config("spark.ui.enabled", "false").getOrCreate())
    try:
        store = get_result_store(f"file://{tmp_path}/dq")
        run = validate_partition(spark.createDataFrame(orders("bad")), TABLE, partition="2026-09-26",
                                 contract=contract(), store=store, count_rows=True)
        assert run.engine == "spark" and run.row_count == 40 and run.p1_failed == 2
    finally:
        spark.stop()


def test_cli(tmp_path, capsys, finance):
    uri = f"file://{tmp_path}/dq"
    rules = tmp_path / "orders.rules.yaml"
    rules.write_text(yaml.safe_dump(contract()))
    policy = tmp_path / "finance.yaml"
    policy.write_text(yaml.safe_dump(finance.to_dict()))
    for partition, kind in DAYS.items():
        data = tmp_path / f"{kind}.parquet"
        orders(kind).to_parquet(data)
        code = cli(["quality-results", "validate", TABLE, "--partition", partition, "--input", str(data),
                    "--rules", str(rules), "--policies", str(policy), "--store", uri,
                    "--output-dir", str(tmp_path / "reports")])
        assert code == (0 if kind == "good" else 1)
    base = ["--store", uri, "--output-dir", str(tmp_path / "reports")]
    assert cli(["quality-results", "check", "--policy", str(policy), "--as-of", "2026-09-26", *base]) == 1
    assert "no cash on web" in capsys.readouterr().out
    finance.window.last_days, finance.on_missing = 1, "ignore"
    policy.write_text(yaml.safe_dump(finance.to_dict()))
    assert cli(["quality-results", "check", "--policy", str(policy), "--as-of", "2026-09-25", *base]) == 0
    assert cli(["quality-results", "rules", TABLE, *base]) == 0
    assert "consumer:finance" in capsys.readouterr().out
    assert cli(["quality-results", "status", TABLE, "--days", "100000", *base]) == 0
    assert cli(["quality-results", "check", "--policy", str(tmp_path / "none.yaml"), *base]) == 2
    assert cli(["quality-results", "latest", TABLE, *base]) == 1            # dt=2026-09-26 failed
    out = capsys.readouterr().out
    assert "[dt=2026-09-26] FAILED" in out and "expect_column_values_to_be_between" in out


# ── partitions: the caller names them, redibis records them ────────────────────

def test_partition_spec_records_column_value_and_a_sortable_timestamp():
    from datetime import date, datetime, timezone

    s = partition_spec("dt=2026-09-26")
    assert (s.partition, s.column, s.value, s.date) == ("dt=2026-09-26", "dt", "2026-09-26", date(2026, 9, 26))
    assert s.ts == datetime(2026, 9, 26, tzinfo=timezone.utc)
    s = partition_spec("dt=2026-09-26/hour=05")
    assert (s.column, s.value) == ("dt/hour", "2026-09-26/05")
    assert s.ts == datetime(2026, 9, 26, 5, tzinfo=timezone.utc)
    s = partition_spec({"dt": date(2026, 9, 26), "region": "eg"})
    assert (s.partition, s.column, s.value) == ("dt=2026-09-26/region=eg", "dt/region", "2026-09-26/eg")
    assert partition_spec("load_date=20260926").ts == datetime(2026, 9, 26, tzinfo=timezone.utc)
    assert partition_spec("event_ts=2026-09-26T13:30:00").ts.hour == 13
    s = partition_spec("region=eg")
    assert s.ts is None and s.date is None and s.column == "region"
    s = partition_spec("2026-09-26")
    assert s.column == "" and s.value == "2026-09-26" and s.ts is not None
    explicit = datetime(2026, 1, 2, 3)
    assert partition_spec("batch=7", partition_ts=explicit).ts == explicit.replace(tzinfo=timezone.utc)


def test_latest_is_the_newest_partition_not_the_last_validated(store):
    validate_partition(orders("good"), TABLE, partition={"dt": "2026-09-26", "hour": 5},
                       contract=contract(), store=store)
    validate_partition(orders("bad"), TABLE, partition={"dt": "2026-09-26", "hour": 7},
                       contract=contract(), store=store)
    validate_partition(orders("good"), TABLE, partition="dt=2026-09-25/hour=23",   # back-fill, last
                       contract=contract(), store=store)
    run, rows = store.latest(TABLE)
    assert (run.partition, run.partition_column, run.partition_value) == (
        "dt=2026-09-26/hour=7", "dt/hour", "2026-09-26/7")
    assert run.status == "failed" and run.partition_ts.hour == 7
    assert len(rows) == run.rules_total and set(rows["run_id"]) == {run.run_id}
    assert set(rows["partition_value"]) == {"2026-09-26/7"}
    runs = store.runs(TABLE)
    assert pd.to_datetime(runs["partition_ts"], utc=True).max().hour == 7
    empty_run, empty_rows = store.latest("shop.none")
    assert empty_run is None and empty_rows.empty
