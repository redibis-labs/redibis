"""Phase 2 — pushdown profiler, column fingerprints, metadata tier composition."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from redibis.config import ProfilingConfig, SourceConfig
from redibis.memory.fingerprint import (
    build_fingerprints,
    compute_format_signature,
    value_to_shape,
)
from redibis.metadata.base import (
    CatalogColumnStats,
    ColumnCatalogInfo,
    SourceMetadata,
)
from redibis.models import ColumnProfile
from redibis.profiling.base import ProfileResult
from redibis.profiling.metadata_tiers import apply_catalog_props_to_partial
from redibis.profiling.pushdown import PushdownAggregateProfiler
from redibis.quality.rule_set import QualityRuleSet
from redibis.services import pipeline


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "national_id": ["29001011401234", "29001011401235"],
        "email": ["user@example.com", "other@example.com"],
        "score": [1, 2],
    })


def test_value_to_shape_masks_digits_and_letters():
    assert value_to_shape("29001011401234") == "D" * 14
    assert value_to_shape("user@example.com") == "LLLL@LLLLLLL.LLL"


def test_compute_format_signature_email_and_catalog_pattern():
    sig, _ = compute_format_signature(["user@example.com", "a@b.co"])
    assert sig in ("email", "email_address")

    nid_sig, _ = compute_format_signature(["29001011401234", "29001011401235"])
    assert "national_id" in nid_sig or nid_sig.startswith("D")


def test_fingerprint_generalizes_tags(sample_df):
    source_meta = SourceMetadata(
        engine="hive",
        columns=[
            ColumnCatalogInfo(
                name="national_id", ordinal=0, native_type="string", nullable=False,
            ),
        ],
        table_props={"Owner": "data_team", "Retention": "90"},
        column_stats={
            "national_id": CatalogColumnStats(ndv=2, stats_fresh=True),
        },
    )
    profiles = [
        ColumnProfile(
            column="national_id",
            dtype="object",
            cardinality_ratio=1.0,
            avg_value_length=14.0,
            null_rate=0.0,
            name_hint_score=0.8,
            arabic_fraction=0.0,
            triage_score=0.9,
            send_to_detector=True,
            logical_type="string",
            physical_type="string",
        ),
    ]
    fps = build_fingerprints(
        sample_df[["national_id"]],
        source_meta,
        profiles,
        table="telecom.customers",
        domain="telecom",
    )
    fp = fps[0]
    assert fp.logical_type.generalizes == "stable"
    assert fp.logical_type.source == "hms"
    assert fp.stats["distinct_count"].generalizes == "approximate"
    assert fp.stats["null_rate"].generalizes == "sample_only"
    card = fp.to_column_card()
    assert "29001011401234" not in card
    assert "national_id" in card


def test_fingerprint_numeric_min_max_are_sample_only(sample_df):
    source_meta = SourceMetadata(engine="hive", columns=[], column_stats={})
    profiles = [
        ColumnProfile(
            column="score",
            dtype="int64",
            cardinality_ratio=1.0,
            avg_value_length=1.0,
            null_rate=0.0,
            name_hint_score=0.0,
            arabic_fraction=0.0,
            triage_score=0.0,
            send_to_detector=False,
            logical_type="integer",
            physical_type="int64",
        ),
    ]
    fps = build_fingerprints(
        sample_df[["score"]],
        source_meta,
        profiles,
        table="db.t",
    )
    fp = fps[0]
    assert fp.stats["min"].generalizes == "sample_only"
    assert fp.stats["max"].generalizes == "sample_only"
    card = fp.to_column_card()
    assert "min(" not in card
    assert "max(" not in card


def test_pushdown_only_queries_stale_columns():
    source_meta = SourceMetadata(
        engine="hive",
        columns=[
            ColumnCatalogInfo(name="fresh_col", ordinal=0, native_type="int", nullable=True),
            ColumnCatalogInfo(name="stale_col", ordinal=1, native_type="string", nullable=True),
        ],
        column_stats={
            "fresh_col": CatalogColumnStats(ndv=10, stats_fresh=True),
            "stale_col": CatalogColumnStats(stats_fresh=False),
        },
    )
    spark = MagicMock()
    row = MagicMock()
    row.__getitem__.side_effect = lambda i: [5, 0, "a", "z"][i]
    spark.sql.return_value.collect.return_value = [row]

    profiler = PushdownAggregateProfiler(
        SourceConfig(engine="hive"),
        ProfilingConfig(),
        spark=spark,
    )
    filled = profiler.fill_gaps(source_meta, "db.tbl")

    assert set(filled) == {"stale_col"}
    sql_blob = " ".join(profiler.last_sql)
    assert "stale_col" in sql_blob
    assert "fresh_col" not in sql_blob
    assert "APPROX_COUNT_DISTINCT" in sql_blob


def test_pushdown_honors_exact_distinct_config():
    source_meta = SourceMetadata(
        engine="postgres",
        columns=[ColumnCatalogInfo(name="c", ordinal=0, native_type="text", nullable=True)],
        column_stats={"c": CatalogColumnStats(stats_fresh=False)},
    )
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [(3,), (0,), ("a", "z")]

    cfg = ProfilingConfig()
    cfg.pushdown.enabled = True
    cfg.pushdown.approx_distinct = False
    profiler = PushdownAggregateProfiler(
        SourceConfig(engine="postgres"),
        cfg,
        connection=conn,
    )
    profiler.fill_gaps(source_meta, "public.t")

    ndv_sql = profiler.last_sql[0]
    assert "COUNT(DISTINCT" in ndv_sql
    assert "APPROX" not in ndv_sql


def test_pushdown_works_with_sqlite3_connection():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (c TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [("a",), ("b",), ("a",), (None,)])
    conn.commit()

    source_meta = SourceMetadata(
        engine="sqlite",
        columns=[ColumnCatalogInfo(name="c", ordinal=0, native_type="text", nullable=True)],
        column_stats={"c": CatalogColumnStats(stats_fresh=False)},
    )
    profiler = PushdownAggregateProfiler(
        SourceConfig(engine="sqlite"),
        ProfilingConfig(),
        connection=conn,
    )
    filled = profiler.fill_gaps(source_meta, "t")

    assert filled["c"].ndv == 2
    assert filled["c"].num_nulls == 1
    assert filled["c"].min == "a"
    assert filled["c"].max == "b"
    assert "FILTER" not in " ".join(profiler.last_sql)


def test_pushdown_sqlite_connection_overrides_postgres_engine():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (c TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    conn.commit()

    source_meta = SourceMetadata(
        engine="postgres",
        columns=[ColumnCatalogInfo(name="c", ordinal=0, native_type="text", nullable=True)],
        column_stats={"c": CatalogColumnStats(stats_fresh=False)},
    )
    profiler = PushdownAggregateProfiler(
        SourceConfig(engine="postgres"),
        ProfilingConfig(),
        connection=conn,
    )
    profiler.fill_gaps(source_meta, "t")
    assert "SUM(CASE WHEN" in profiler.last_sql[1]
    assert "FILTER" not in " ".join(profiler.last_sql)


@patch("redibis.services.pipeline.get_profiler")
def test_profile_dataframe_metadata_disabled_unchanged(mock_get, sample_df):
    fake = ProfileResult(
        column_profiles=[],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(),
    )
    mock_profiler = MagicMock()
    mock_profiler.profile.return_value = fake
    mock_get.return_value = mock_profiler

    out = pipeline.profile_dataframe(
        sample_df,
        "tbl",
        profiling=ProfilingConfig(),
        source=SourceConfig(engine="hive"),
    )
    assert out.fingerprints == []
    assert out.source_metadata is None


@patch("redibis.profiling.metadata_tiers.get_metadata_retriever")
@patch("redibis.services.pipeline.get_profiler")
def test_profile_dataframe_with_metadata_enabled(mock_get, mock_retriever, sample_df):
    fake = ProfileResult(
        column_profiles=[
            ColumnProfile(
                column="email",
                dtype="object",
                cardinality_ratio=1.0,
                avg_value_length=10.0,
                null_rate=0.0,
                name_hint_score=0.5,
                arabic_fraction=0.0,
                triage_score=0.5,
                send_to_detector=False,
            ),
        ],
        arabic_columns={},
        triage_signals=[],
        suggested_rules=QualityRuleSet(),
    )
    mock_profiler = MagicMock()
    mock_profiler.profile.return_value = fake
    mock_get.return_value = mock_profiler

    meta = SourceMetadata(
        engine="hive",
        columns=[ColumnCatalogInfo("email", 0, "string", True)],
        table_props={"Owner": "ops", "Retention": "90"},
        column_stats={"email": CatalogColumnStats(ndv=2, stats_fresh=True)},
    )
    mock_retriever.return_value.get_source_metadata.return_value = meta

    cfg = ProfilingConfig()
    cfg.metadata.enabled = True
    out = pipeline.profile_dataframe(
        sample_df,
        "telecom.customers",
        profiling=cfg,
        source=SourceConfig(engine="hive"),
        table="telecom.customers",
        domain="telecom",
        spark=MagicMock(),
    )

    assert len(out.fingerprints) == 3
    assert out.source_metadata is meta
    assert out.fingerprints[1].format_signature.value in ("email", "email_address")


def test_apply_catalog_props_to_partial_sets_owner_and_retention():
    partial = {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "database_name": "telecom",
        "table_name": "customers",
    }
    meta = SourceMetadata(
        table_props={"Owner": "data_steward", "Retention": "90"},
        engine="hive",
    )
    out = apply_catalog_props_to_partial(partial, meta)
    assert out["owner"] == "data_steward"
    assert out["slaProperties"][0]["property"] == "retention"
    assert out["slaProperties"][0]["value"] == 90
