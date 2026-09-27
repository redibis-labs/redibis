"""Guarded profile store — consent, role, logging, residency."""

from __future__ import annotations

import logging
import tempfile

from redibis.memory.consent import SamplingConsentStore
from redibis.store.contract_store import ContractStore
from redibis.store.profile_store import RESIDENCY_CONTAINS_VALUES, RESIDENCY_PORTABLE, ProfileStore
from redibis.store.storage_backend import LocalBackend


class _FakeProfile:
    def __init__(self):
        self.column_profiles = [
            type("CP", (), {
                "column": "email",
                "logical_type": "string",
                "physical_type": "string",
                "null_rate": 0.0,
                "cardinality_ratio": 0.9,
                "avg_value_length": 12,
                "arabic_fraction": 0.0,
                "constant": False,
                "near_constant": False,
            })()
        ]
        self.raw = {}


def test_samples_are_absent_without_consent_not_empty():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        consent = SamplingConsentStore(backend, "c")
        store = ProfileStore(backend, "c", consent=consent)
        store.write(
            "db.t", "r1",
            profile_result=_FakeProfile(),
            samples={"email": ["a@b.com"]},
            consent=consent,
        )
        sample_key = "_meta/profiles/db.t/r1/samples/email.json"
        assert not backend.exists("c", sample_key)
        man = store.manifest("db.t", "r1")
        assert man["residency"] == RESIDENCY_PORTABLE
        assert "email" not in man["samples_written"]


def test_explorer_role_never_receives_samples_even_with_consent():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        consent = SamplingConsentStore(backend, "c")
        consent.set_approved("db.t", "email", approved=True, approved_by="ada")
        store = ProfileStore(backend, "c", consent=consent)
        store.write("db.t", "r1", profile_result=_FakeProfile(),
                    samples={"email": ["a@b.com"]}, consent=consent)
        got = store.read("db.t", "r1", "email", actor="bob", include_samples=True, allow_samples=False)
        assert got["samples"] is None
        assert got["samples_withheld"] == "role"


def test_explorer_does_not_receive_value_bearing_profile_stats():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ProfileStore(backend, "c")
        store.write("db.t", "r1", profile_result={"columns": {"nid": {
            "null_rate": 0.0,
            "ndv": 100,
            "min": "29501011234567",
            "max": "29501019999999",
            "mean": "29501015555555",
            "histogram": [{"label": "29501011234567", "count": 3}],
        }}})
        explorer = store.read("db.t", "r1", "nid", actor="bob", allow_samples=False)
        stats = explorer["stats"]
        assert stats.get("null_rate") == 0.0
        assert stats.get("ndv") == 100
        for key in ("min", "max", "mean", "histogram"):
            assert key not in stats
        admin = store.read("db.t", "r1", "nid", actor="ada", allow_samples=True)
        assert admin["stats"]["min"] == "29501011234567"


def test_sample_read_is_logged_without_values(caplog):
    caplog.set_level(logging.INFO, logger="redibis.decision")
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        consent = SamplingConsentStore(backend, "c")
        consent.set_approved("db.t", "email", approved=True, approved_by="ada")
        store = ProfileStore(backend, "c", consent=consent)
        store.write("db.t", "r1", profile_result=_FakeProfile(),
                    samples={"email": ["secret-value-xyz"]}, consent=consent)
        store.read("db.t", "r1", "email", actor="ada", include_samples=True, allow_samples=True)
        text = caplog.text
        assert "secret-value-xyz" not in text
        assert "profile.samples_read" in text or "samples_read" in text or "DECISION" in text


def test_residency_is_contains_values_only_when_samples_written():
    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        consent = SamplingConsentStore(backend, "c")
        consent.set_approved("db.t", "email", approved=True, approved_by="ada")
        store = ProfileStore(backend, "c", consent=consent)
        man = store.write("db.t", "r1", profile_result=_FakeProfile(),
                          samples={"email": ["a@b.com"]}, consent=consent)
        assert man["residency"] == RESIDENCY_CONTAINS_VALUES


def test_metadata_no_longer_embeds_profiling_payloads():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert({
            "database_name": "db", "table_name": "t",
            "schema": [{"name": "t", "properties": [{"name": "email"}]}],
            "_column_telemetry": {
                "email": {
                    "confidence": 0.8,
                    "profiling": {"histogram": [1, 2, 3], "samples": ["x"]},
                    "samples": ["raw"],
                }
            },
        }, table="db.t", workflow="pii", run_id="r1")
        meta = store.get_metadata("db.t")
        col = (meta.get("columns") or {}).get("email") or {}
        assert "profiling" not in col
        assert "samples" not in col
        tel = store.metadata.get_column_telemetry("db.t")["email"]
        assert "profiling" not in tel
        assert "samples" not in tel


# ── Profile gap: PII-only scans must not write an empty profile record ─────────

def _scan_service(tmp):
    from redibis.services.scan_service import ScanService

    backend = LocalBackend(tmp)
    store = ContractStore(backend, "c")
    return ScanService(backend=backend, store=store)


def _df():
    import pandas as pd

    return pd.DataFrame({"msisdn": ["0100", "0101"], "charge": [1.0, 2.0]})


def _cfg(table="cdr", scan_types=None):
    from types import SimpleNamespace

    return SimpleNamespace(table=table, scan_types=scan_types or ["pii"])


def test_no_profile_record_when_profiling_did_not_run():
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        svc = _scan_service(tmp)
        run_result = SimpleNamespace(status="success", profile=None, quality_results=None)
        svc._write_guarded_profiles(_cfg(), "run-42", run_result, _df())
        assert svc.store.profiles.latest_run_id("cdr") is None
        assert svc.store.profiles.manifest("cdr", "run-42") is None


def test_profiling_scan_records_profile_engine():
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        svc = _scan_service(tmp)
        run_result = SimpleNamespace(
            status="success",
            profile={"columns": {"msisdn": {"null_rate": 0.0, "ndv": 2}}},
            quality_results=None,
        )
        svc._write_guarded_profiles(_cfg(scan_types=["profile"]), "run-1", run_result, _df())
        man = svc.store.profiles.manifest("cdr", "run-1")
        assert man["engines"] == ["profile"]
        assert "write_error" not in man


def test_guarded_profile_write_failure_is_recorded_on_the_manifest(monkeypatch):
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        svc = _scan_service(tmp)

        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(svc.store.profiles, "write", boom)
        run_result = SimpleNamespace(
            status="success", profile={"columns": {"msisdn": {"null_rate": 0.0}}},
            quality_results=None,
        )
        # Non-fatal: must not raise out of the scan.
        svc._write_guarded_profiles(_cfg(scan_types=["profile"]), "run-7", run_result, _df())
        man = svc.store.profiles.manifest("cdr", "run-7")
        assert man is not None
        assert "disk full" in man["write_error"]
        assert svc.store.profiles.latest_run_id("cdr") == "run-7"


def test_mark_write_error_preserves_an_existing_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(LocalBackend(tmp), "c")
        store.write("db.t", "r1", profile_result=_FakeProfile(), engines=["profile"])
        store.mark_write_error("db.t", "r1", "x" * 900)
        man = store.manifest("db.t", "r1")
        assert man["engines"] == ["profile"]
        assert man["columns_written"] == ["email"]
        assert len(man["write_error"]) == 500


def test_write_refuses_samples_only_records():
    import pytest

    with tempfile.TemporaryDirectory() as tmp:
        store = ProfileStore(LocalBackend(tmp), "c")
        with pytest.raises(ValueError):
            store.write("cdr", "run-42", profile_result=None, quality_result=None,
                        samples={"msisdn": ["0100"]}, engines=[])
        assert store.latest_run_id("cdr") is None


def test_write_failure_does_not_fail_the_scan(monkeypatch):
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        svc = _scan_service(tmp)

        def boom(*a, **k):
            raise RuntimeError("backend down")

        # Both the write and the error stamp fail: still no exception out.
        monkeypatch.setattr(svc.store.profiles, "write", boom)
        monkeypatch.setattr(svc.store.profiles, "mark_write_error", boom)
        run_result = SimpleNamespace(
            status="success", profile={"columns": {"msisdn": {"null_rate": 0.0}}},
            quality_results=None,
        )
        svc._write_guarded_profiles(_cfg(scan_types=["profile"]), "run-8", run_result, _df())
