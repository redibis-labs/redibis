"""Golden promotion and vector-store integration tests."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from redibis.profiling.fingerprint import compute_fingerprint
from redibis.profiling.golden import GoldenColumn
from redibis.profiling.golden_writer import (
    golden_from_contract_prop,
    promote_approved_columns,
)
from redibis.profiling.vector_store import VectorConfig, reset_vector_store
from redibis.profiling.vectorize import FEATURE_DIM, fingerprint_to_vector
from redibis.profiling.backends.inmemory import InMemoryVectorStore
from redibis.services.session.state import ApprovedProperty, ApprovedSet, ScanSession
from redibis.store.golden_store import GoldenStore
from redibis.store.storage_backend import LocalBackend


@pytest.fixture
def local_backend(tmp_path):
    return LocalBackend(tmp_path)


@pytest.fixture
def sample_fp():
    return compute_fingerprint(
        pd.Series(["CUST001", "CUST002", "CUST003"]),
        column="customer_id",
    )


def test_golden_from_contract_prop_skips_without_fingerprint():
    assert golden_from_contract_prop(
        "t", "customer_id", {"name": "customer_id"},
        fingerprint=None,
    ) is None


def test_golden_from_contract_prop_rejects_stub_fingerprint():
    assert golden_from_contract_prop(
        "t", "customer_id", {"name": "customer_id"},
        fingerprint={"column": "customer_id", "dtype": "object"},
    ) is None


def test_golden_from_contract_prop_with_real_fingerprint(sample_fp):
    golden = golden_from_contract_prop(
        "t", "customer_id",
        {"name": "customer_id", "classification": "internal", "description": "Customer key"},
        fingerprint=sample_fp,
    )
    assert golden is not None
    assert golden.glossary == "Customer key"
    assert len(golden.embedding) == FEATURE_DIM
    assert sum(abs(x) for x in golden.embedding) > 0


def test_promote_quality_column_not_pii_only(local_backend, sample_fp, tmp_path):
    """Quality-approved non-PII columns must enter the golden set."""
    reset_vector_store()
    gs = GoldenStore(local_backend, "contracts")
    store = InMemoryVectorStore(gs)

    session = ScanSession(
        session_id="s1",
        table_name="sales.orders",
        data_path=str(tmp_path / "x.csv"),
    )
    session.approved = ApprovedSet(items=[
        ApprovedProperty(
            prop_id="q1",
            kind="quality",
            column="customer_id",
            status="merged",
            payload={"rule": "uniqueCheck"},
        ),
    ])
    session.profiler = type("P", (), {
        "structural_fingerprints": [sample_fp],
    })()

    mock_store = type("CS", (), {
        "backend": local_backend,
        "bucket": "contracts",
        "get_active": lambda _self, _t: {
            "schema": [{"properties": [
                {"name": "customer_id", "logicalType": "string",
                 "classification": "internal", "description": "Order customer key",
                 "quality": [{"rule": "uniqueCheck"}]},
            ]}],
        },
    })()

    with patch("redibis.profiling.golden_writer.get_vector_store", return_value=store):
        n = promote_approved_columns(session, mock_store)

    assert n == 1
    assert store.count() == 1
    golden = store._cols["sales.orders::customer_id"]
    assert golden.classification == "internal"
    assert golden.glossary == "Order customer key"
    assert golden.entity_type is None


def test_promote_glossary_kind_column(local_backend, sample_fp, tmp_path):
    reset_vector_store()
    gs = GoldenStore(local_backend, "contracts")
    store = InMemoryVectorStore(gs)

    session = ScanSession(
        session_id="s2",
        table_name="sales.orders",
        data_path=str(tmp_path / "x.csv"),
    )
    session.approved = ApprovedSet(items=[
        ApprovedProperty(
            prop_id="g1",
            kind="glossary",
            column="customer_id",
            status="merged",
            payload={
                "name": "customer_id",
                "logicalType": "string",
                "classification": "internal",
                "description": "Stable customer identifier",
            },
        ),
    ])
    session.profiler = type("P", (), {"structural_fingerprints": [sample_fp]})()

    mock_store = type("CS", (), {
        "backend": local_backend,
        "bucket": "contracts",
        "get_active": lambda _self, _t: {
            "schema": [{"properties": [
                {"name": "customer_id", "description": "Stable customer identifier"},
            ]}],
        },
    })()

    with patch("redibis.profiling.golden_writer.get_vector_store", return_value=store):
        n = promote_approved_columns(session, mock_store)

    assert n == 1
    assert store._cols["sales.orders::customer_id"].glossary == "Stable customer identifier"


def test_inmemory_upsert_many_rebuilds_once(local_backend, sample_fp):
    gs = GoldenStore(local_backend, "contracts")
    store = InMemoryVectorStore(gs)
    rebuilds = 0
    original = store._rebuild_index

    def counting_rebuild():
        nonlocal rebuilds
        rebuilds += 1
        original()

    store._rebuild_index = counting_rebuild  # type: ignore[method-assign]

    cols = []
    for i in range(5):
        vec = fingerprint_to_vector(sample_fp)
        cols.append(GoldenColumn(
            table_name="t",
            column_name=f"col_{i}",
            embedding=vec,
            fingerprint=sample_fp.to_dict(),
        ))
    store.upsert_many(cols)
    assert store.count() == 5
    assert rebuilds == 1


def test_vector_config_precedence(monkeypatch):
    monkeypatch.delenv("REDIBIS_VECTOR_BACKEND", raising=False)
    monkeypatch.setenv("REDIBIS_VECTOR_BACKEND", "pgvector")
    cfg = VectorConfig.from_env({"vector": {"backend": "inmemory", "dim": 32}})
    assert cfg.backend == "inmemory"
    assert cfg.dim == 32
