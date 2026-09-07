"""Tests for structural column fingerprints and golden vector store."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from redibis.profiling.fingerprint import (
    FEATURE_SPEC_VERSION,
    compute_fingerprint,
    compute_table_fingerprints,
)
from redibis.profiling.golden import GoldenColumn
from redibis.profiling.vector_store import VectorConfig, build_vector_store, reset_vector_store, score_from_distance
from redibis.profiling.vectorize import FEATURE_DIM, fingerprint_to_vector
from redibis.profiling.backends.inmemory import InMemoryVectorStore
from redibis.store.fingerprint_store import FingerprintStore
from redibis.store.golden_store import GoldenStore
from redibis.store.storage_backend import LocalBackend


@pytest.fixture
def local_backend(tmp_path):
    return LocalBackend(tmp_path)


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "national_id": ["29001011401234", "29001011401235", "29001011401236"],
        "email": ["user@example.com", "other@example.com", "a@b.co"],
        "score": [1, 2, 3],
        "notes": [None, "", "hello world"],
    })


def test_fingerprint_deterministic(sample_df):
    s = sample_df["national_id"]
    fp1 = compute_fingerprint(s, column="national_id")
    fp2 = compute_fingerprint(s, column="national_id")
    assert fp1.to_dict() == fp2.to_dict()
    assert fp1.feature_spec_version == FEATURE_SPEC_VERSION


def test_fingerprint_vector_dim_stable(sample_df):
    fp = compute_fingerprint(sample_df["national_id"], column="national_id")
    vec = fingerprint_to_vector(fp)
    assert len(vec) == FEATURE_DIM
    assert abs(np.linalg.norm(vec) - 1.0) < 1e-5


def test_pii_reduction_strips_numeric_extrema(sample_df):
    fp = compute_fingerprint(sample_df["score"], column="score", is_pii=True)
    assert fp.is_pii_reduced is True
    assert fp.num_min is None
    assert fp.num_max is None
    assert fp.num_mean is None


def test_full_table_stats_override(sample_df):
    fp = compute_fingerprint(
        sample_df["national_id"],
        column="national_id",
        full_table_stats={"count": 1_000_000, "approx_distinct": 999_000},
    )
    assert fp.stats_source == "full"
    assert fp.distinct_ratio == pytest.approx(0.999, rel=1e-3)


def test_pattern_masks_k_anonymity(sample_df):
    fp = compute_fingerprint(sample_df["national_id"], column="national_id")
    assert fp.pattern_masks
    total_frac = sum(f for _, f in fp.pattern_masks)
    assert total_frac == pytest.approx(1.0, abs=0.01)


def test_compute_table_fingerprints_all_columns(sample_df):
    fps = compute_table_fingerprints(sample_df, pii_columns={"national_id"})
    assert len(fps) == 4
    nid = next(f for f in fps if f.column == "national_id")
    assert nid.is_pii_reduced is True


def test_fingerprint_store_round_trip(local_backend, sample_df):
    store = FingerprintStore(local_backend, "contracts")
    fps = compute_table_fingerprints(sample_df)
    store.write_table("telecom.customers", fps)
    loaded = store.get_column("telecom.customers", "email")
    assert loaded is not None
    assert loaded["column"] == "email"
    listed = store.list_table("telecom.customers")
    assert len(listed) == 4


def test_golden_store_round_trip(local_backend):
    gs = GoldenStore(local_backend, "contracts")
    col = GoldenColumn(
        table_name="t.customers",
        column_name="email",
        embedding=[0.1] * FEATURE_DIM,
        fingerprint={"column": "email"},
        classification="confidential",
        glossary="Customer email address",
    )
    gs.write(col)
    loaded = gs.get("t.customers", "email")
    assert loaded is not None
    assert loaded.glossary == "Customer email address"
    assert len(gs.list_all()) == 1


def test_inmemory_vector_store_search(local_backend):
    reset_vector_store()
    gs = GoldenStore(local_backend, "golden")
    store = InMemoryVectorStore(gs)
    fp = compute_fingerprint(
        pd.Series(["29001011401234", "29001011401235"]),
        column="national_id",
    )
    vec = fingerprint_to_vector(fp)
    golden = GoldenColumn(
        table_name="ref",
        column_name="national_id",
        embedding=vec,
        fingerprint=fp.to_dict(),
        entity_type="NATIONAL_ID",
    )
    store.upsert(golden)
    query_fp = compute_fingerprint(
        pd.Series(["39001011401299", "39001011401298"]),
        column="opaque_col",
    )
    matches = store.search(fingerprint_to_vector(query_fp), k=1)
    assert len(matches) == 1
    assert matches[0].score > 0.5
    assert matches[0].golden.entity_type == "NATIONAL_ID"


def test_vector_store_contract_both_backends(local_backend):
    """Contract test: inmemory always runs; pgvector skipped without PG_DSN."""
    import os
    reset_vector_store()
    gs = GoldenStore(local_backend, "contracts")
    backends = [InMemoryVectorStore(gs)]
    if os.getenv("PG_DSN"):
        from redibis.profiling.backends.pgvector import PgVectorStore
        backends.append(PgVectorStore(os.environ["PG_DSN"], gs))

    fp = compute_fingerprint(pd.Series(["a@b.co", "c@d.co"]), column="email")
    vec = fingerprint_to_vector(fp)
    golden = GoldenColumn(
        table_name="t",
        column_name="email",
        embedding=vec,
        fingerprint=fp.to_dict(),
    )
    for backend in backends:
        backend.load()
        backend.upsert(golden)
        assert backend.count() >= 1
        hits = backend.search(vec, k=1)
        assert hits
        assert hits[0].score >= score_from_distance(0.0, "cosine") - 0.01


def test_build_vector_store_factory(local_backend):
    reset_vector_store()
    gs = GoldenStore(local_backend, "contracts")
    cfg = VectorConfig(backend="inmemory")
    store = build_vector_store(cfg, gs)
    assert store.name == "inmemory"
