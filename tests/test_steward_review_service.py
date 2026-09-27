"""Steward Review read model — overview + column page."""

from __future__ import annotations

import tempfile

from redibis.store.contract_store import ContractStore
from redibis.store.generation_ledger import Generation
from redibis.store.storage_backend import LocalBackend
from redibis.services.steward_review_service import StewardReviewService


def _contract():
    return {
        "apiVersion": "v3.0.1",
        "kind": "DataContract",
        "name": "customers_contract",
        "database_name": "db",
        "table_name": "customers",
        "version": "1.0.0",
        "schema": [{
            "name": "customers",
            "description": "customers",
            "properties": [
                {"name": "id", "logicalType": "integer"},
                {"name": "msisdn", "logicalType": "string", "entity_type": "PHONE_NUMBER",
                 "classification": "pii_personal", "tags": ["pii"]},
            ],
        }],
    }


def test_overview_and_column_payload():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="pii", source="regex", value={"is_pii": True},
                       confidence=0.81, run_id="r1", ts="t"),
            Generation(field="pii", source="ner", value={"is_pii": False},
                       confidence=0.22, run_id="r1", ts="t"),
        ])
        svc = StewardReviewService(store)
        ov = svc.overview("db.customers")
        assert ov["stats"]["columns_total"] == 2
        assert ov["stats"]["engine_agreement"]["contested"] >= 1
        assert ov["guarantee"]["total"] == 2
        page = svc.column("db.customers", "msisdn", actor="ada")
        pii_gens = page["generations"]["pii"]
        sources = {g["source"] for g in pii_gens}
        assert {"regex", "ner"} <= sources
        assert page["agreement"]["pii"] == "contested"
        assert page["profile"]["samples"] is None
        ov_id = next(c for c in ov["columns"] if c["column"] == "id")
        assert ov_id["agreement"] == "no_evidence"
        assert ov["stats"]["engine_agreement"]["no_evidence"] >= 1


def test_agreement_is_no_evidence_when_no_engine_voted():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        page = StewardReviewService(store).column("db.customers", "id", actor="ada")
        assert page["agreement"]["pii"] == "no_evidence"


def test_human_only_generation_is_no_evidence_not_unanimous():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "id", [
            Generation(field="pii", source="human", value={"is_pii": False},
                       confidence=1.0, run_id="steward", ts="t"),
        ])
        page = StewardReviewService(store).column("db.customers", "id", actor="ada")
        assert page["agreement"]["pii"] == "no_evidence"
        assert page["agreement"]["pii"] != "unanimous"


def test_stats_count_no_evidence_separately():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        ov = StewardReviewService(store).overview("db.customers")
        agr = ov["stats"]["engine_agreement"]
        assert "no_evidence" in agr
        assert agr["no_evidence"] == 2
        assert agr["unanimous"] == 0


def test_engine_accept_rationales_hidden_when_no_evidence():
    from redibis.review.rationale import codes_for_choice

    assert "engine_correct" not in codes_for_choice("regex", agreement="no_evidence")
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        page = StewardReviewService(store).column("db.customers", "id", actor="ada")
        assert page["agreement"]["pii"] == "no_evidence"
        for src, codes in page["rationale_codes"].items():
            assert "engine_correct" not in codes, src


def test_empty_role_never_receives_samples():
    from redibis.memory.consent import SamplingConsentStore
    from redibis.store.profile_store import ProfileStore

    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ContractStore(backend, "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        consent = SamplingConsentStore(backend, "c")
        consent.set_approved("db.customers", "msisdn", approved=True, approved_by="ada")
        profiles = ProfileStore(backend, "c", consent=consent)
        profiles.write(
            "db.customers", "r1",
            profile_result={"columns": {"msisdn": {"null_rate": 0.0}}},
            samples={"msisdn": ["01012345678"]},
            consent=consent,
        )
        page = StewardReviewService(store, profiles=profiles).column(
            "db.customers", "msisdn", actor="cli", role="", include_samples=True,
        )
        assert page["profile"]["samples"] is None
        assert page["profile"]["samples_withheld"] == "role"


def test_column_shows_engine_verdicts_with_confidence_pct():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="pii", source="regex", value={"is_pii": True, "entity_type": "PHONE_NUMBER"},
                       confidence=0.80, run_id="r1", ts="t"),
            Generation(field="pii", source="ner", value={"is_pii": True, "entity_type": "PHONE_NUMBER"},
                       confidence=0.70, run_id="r1", ts="t"),
            Generation(field="pii", source="phone", value={"is_pii": True},
                       confidence=0.91, run_id="r1", ts="t"),
        ])
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        by_src = {e["source"]: e for e in page["engines"]["pii"]}
        assert by_src["regex"]["confidence_pct"] == 80
        assert by_src["ner"]["confidence_pct"] == 70
        assert by_src["phone"]["is_pii"] is True
        assert by_src["llm"]["present"] is False
        assert by_src["llm"]["label"] == "LLM enrich"
        assert by_src["llm_synthesis"]["label"] == "Deep Enrich"


def test_definition_candidates_include_enrich_and_synthesis():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="definition", source="llm", value="mobile subscriber number from enrich",
                       confidence=0.9, run_id="enrich", ts="t"),
            Generation(field="definition", source="llm_synthesis",
                       value="MSISDN synthesised from schema requirements",
                       confidence=0.7, run_id="synth", ts="t"),
        ])
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        srcs = {d["source"]: d["value"] for d in page["definition_candidates"] if not d.get("custom")}
        assert "mobile subscriber number from enrich" in srcs["llm"]
        assert "synthesised" in srcs["llm_synthesis"]
        assert page["agreement"]["definition"] == "contested"
        assert any(d.get("custom") for d in page["definition_candidates"])


def test_profile_falls_back_to_ledger_and_contract_type():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="logical_type", source="profile", value="string",
                       confidence=None, run_id="r1", ts="t",
                       detail={"null_rate": 0.02, "ndv": 900, "format_signature": "01XXXXXXXXX"}),
        ])
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        stats = page["profile"]["stats"]
        assert stats["null_rate"] == 0.02
        assert stats["ndv"] == 900
        assert stats["logical_type"] == "string"
        assert page["profile"]["format_signature"] == "01XXXXXXXXX"


def test_profile_normalizes_alias_keys_from_store_and_telemetry():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.profiles.write(
            "db.customers", "r1",
            profile_result={"columns": {"msisdn": {
                "nulls_fraction": 0.1,
                "nunique": 42,
                "cardinality_ratio": 0.4,
                "dtype": "string",
                "mean_length": 11,
            }}},
        )
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        stats = page["profile"]["stats"]
        assert stats["null_rate"] == 0.1
        assert stats["ndv"] == 42
        assert stats["ndv_ratio"] == 0.4
        assert stats["logical_type"] == "string"
        assert stats["avg_value_length"] == 11


def test_table_owner_edit_persists_on_overview():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        ov = svc.decide_table(
            "db.customers", "owner",
            {"decision": "edit", "value": "ada@example.com",
             "rationale_code": "domain_knowledge", "rationale_text": "steward edit"},
            actor="ada",
        )
        assert ov["table_section"]["owner"] == "ada@example.com"
        active = store.get_active("db.customers")
        # ODCS: ownership lives in top-level team, not owner fields.
        assert "owner" not in active
        assert "owner" not in active["schema"][0]
        owners = [
            m for m in (active.get("team") or [])
            if isinstance(m, dict) and str(m.get("role") or "").lower() == "owner"
        ]
        assert owners
        assert owners[0]["name"] == "ada@example.com"
        assert owners[0]["username"] == "ada@example.com"


def test_edit_pii_off_and_batch_save():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="pii", source="regex", value={"is_pii": True},
                       confidence=0.8, run_id="r1", ts="t"),
        ])
        svc = StewardReviewService(store)
        page = svc.decide_many("db.customers", "msisdn", [
            {"field": "pii", "decision": "edit", "chosen_source": "human",
             "value": {"is_pii": False}, "rationale_code": "false_positive_identifier_not_personal",
             "rationale_text": "internal routing key, not personal"},
            {"field": "definition", "decision": "edit", "chosen_source": "human",
             "value": "internal routing key", "rationale_code": "business_definition_supplied",
             "rationale_text": "steward authored"},
        ], actor="ada")
        assert page["current"]["pii"] is False
        assert "internal routing key" in (page["current"]["definition"] or "")
        dec = store.get_pii_decisions("db.customers")["msisdn"]
        assert dec["status"] == "not_pii"


def test_export_verdicts_without_finalize():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        svc.decide("db.customers", "msisdn", "pii", {
            "decision": "edit", "chosen_source": "human",
            "value": {"is_pii": True, "entity_type": "PHONE_NUMBER"},
            "rationale_code": "domain_knowledge",
            "rationale_text": "looks like a phone",
        }, actor="ada")
        payload = svc.export_verdicts("db.customers", actor="ada")
        assert payload["kind"] == "redibis.steward_verdicts"
        assert payload["entries"]
        assert any(e.get("column") == "msisdn" for e in payload["entries"])


def test_telemetry_backfill_missing_engines():
    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        store.metadata.merge_column_telemetry("db.customers", {
            "msisdn": {
                "presidio_score": 0.8,
                "gliner_score": 0.7,
                "phone_score": 0.9,
                "entity_type": "PHONE_NUMBER",
                "discovery_engines": ["regex", "ner", "phone"],
            }
        })
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        srcs = {e["source"] for e in page["engines"]["pii"] if e["present"]}
        assert {"regex", "ner", "phone"} <= srcs
        assert next(e for e in page["engines"]["pii"] if e["source"] == "regex")["confidence_pct"] == 80


def test_a1_export_loads_as_scan_verdict_package():
    from redibis.review.verdict_package import load_verdict_package, steward_verdicts_to_package

    with tempfile.TemporaryDirectory() as tmp:
        store = ContractStore(LocalBackend(tmp), "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        svc = StewardReviewService(store)
        svc.decide("db.customers", "msisdn", "pii", {
            "decision": "edit", "chosen_source": "human",
            "value": {"is_pii": True, "entity_type": "PHONE_NUMBER"},
            "rationale_code": "domain_knowledge",
            "rationale_text": "phone",
        }, actor="ada")
        a1 = svc.export_verdicts("db.customers", actor="ada")
        assert a1["kind"] == "redibis.steward_verdicts"
        pkg = steward_verdicts_to_package(a1)
        assert pkg.kind == "redibis.verdict_package"
        by_col = pkg.by_column("db.customers")
        assert by_col["msisdn"].status == "pii"
        assert by_col["msisdn"].entity_type == "PHONE_NUMBER"
        path = tmp + "/a1.json"
        import json
        from pathlib import Path
        Path(path).write_text(json.dumps(a1), encoding="utf-8")
        loaded = load_verdict_package(path)
        assert loaded.by_column("db.customers")["msisdn"].status == "pii"



# ── profile_status: why the Profile panel is (or is not) populated ─────────────

def test_has_profile_stats_ignores_logical_type():
    from redibis.services.steward_review_service import _has_profile_stats

    assert not _has_profile_stats({"logical_type": "string"})
    assert not _has_profile_stats({})
    assert _has_profile_stats({"logical_type": "string", "null_rate": 0.0})
    assert _has_profile_stats({"ndv": 12})


def _store(tmp):
    store = ContractStore(LocalBackend(tmp), "c")
    store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
    return store


def _write_legacy_manifest(store, run_id, *, engines, extra=None):
    """Write a manifest + empty column record the way pre-fix scans did."""
    backend = store.profiles.backend
    bucket = store.profiles.bucket
    backend.put_json(bucket, store.profiles._column_key("db.customers", run_id, "msisdn"),
                     {"quality": []})
    man = {"table": "db.customers", "run_id": run_id, "engines": engines,
           "residency": "portable", "columns_written": ["msisdn"], "samples_written": [],
           "digests": {}, "written_at": "2026-01-01T00:00:00Z"}
    man.update(extra or {})
    backend.put_json(bucket, store.profiles._manifest_key("db.customers", run_id), man)


def test_column_payload_reports_no_run_when_no_profile_record():
    with tempfile.TemporaryDirectory() as tmp:
        page = StewardReviewService(_store(tmp)).column("db.customers", "msisdn", actor="ada")
        prof = page["profile"]
        assert prof["profile_status"] == "no_run"
        assert prof["profile_run_id"] == ""
        assert prof["samples_withheld"] == ""
        # logical_type still shown from the contract, but it is not a profile.
        assert prof["stats"]["logical_type"] == "string"


def test_column_payload_reports_not_run_when_profiling_skipped():
    from types import SimpleNamespace

    import pandas as pd

    from redibis.services.scan_service import ScanService

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        svc = ScanService(backend=store.backend, store=store)
        # An earlier quality-only run with no profiler output.
        run_result = SimpleNamespace(
            status="success", profile=None,
            quality_results={"msisdn": [{"type": "not_null", "passed": True}]},
        )
        cfg = SimpleNamespace(table="db.customers", scan_types=["quality"])
        svc._write_guarded_profiles(cfg, "run-q", run_result,
                                    pd.DataFrame({"msisdn": ["0100"]}))
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        # quality-only runs do not record the "profile" engine
        assert "profile" not in page["profile"]["profile_engines"]
        assert page["profile"]["profile_status"] == "not_run"


def test_legacy_record_with_empty_engines_classifies_as_not_run():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _write_legacy_manifest(store, "run-42", engines=[])
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        assert page["profile"]["profile_status"] == "not_run"
        assert page["profile"]["profile_run_id"] == "run-42"
        assert page["profile"]["profile_engines"] == []


def test_column_payload_reports_ok_after_a_profiling_scan():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.profiles.write(
            "db.customers", "r2",
            profile_result={"columns": {"msisdn": {"null_rate": 0.0, "ndv": 10}}},
            engines=["profile"],
        )
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        prof = page["profile"]
        assert prof["profile_status"] == "ok"
        assert prof["profile_engines"] == ["profile"]
        assert prof["stats"]["null_rate"] == 0.0


def test_write_failure_surfaces_as_write_failed_in_the_column_payload():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.profiles.mark_write_error("db.customers", "r3", "boom")
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        assert page["profile"]["profile_status"] == "write_failed"
        assert page["profile"]["profile_error"] == "boom"


def test_ledger_fallback_promotes_not_run_to_ok():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _write_legacy_manifest(store, "run-42", engines=[])
        store.generation_ledger.append("db.customers", "msisdn", [
            Generation(field="logical_type", source="profile", value="string",
                       confidence=None, run_id="r0", ts="t", detail={"null_rate": 0.0}),
        ])
        page = StewardReviewService(store).column("db.customers", "msisdn", actor="ada")
        assert page["profile"]["profile_status"] == "ok"


def test_samples_withheld_is_only_about_samples():
    from redibis.memory.consent import SamplingConsentStore
    from redibis.store.profile_store import ProfileStore

    with tempfile.TemporaryDirectory() as tmp:
        backend = LocalBackend(tmp)
        store = ContractStore(backend, "c")
        store.upsert(_contract(), table="db.customers", workflow="manual", run_id="r1")
        consent = SamplingConsentStore(backend, "c")  # nothing approved
        profiles = ProfileStore(backend, "c", consent=consent)
        profiles.write(
            "db.customers", "r1",
            profile_result={"columns": {"msisdn": {"null_rate": 0.0}}},
            samples={"msisdn": ["01012345678"]}, consent=consent, engines=["profile"],
        )
        page = StewardReviewService(store, profiles=profiles).column(
            "db.customers", "msisdn", actor="ada", role="admin", include_samples=True,
        )
        assert page["profile"]["profile_status"] == "ok"
        assert page["profile"]["samples_withheld"] == "no consent"
        assert page["profile"]["samples"] is None
