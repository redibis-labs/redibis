"""Generations ledger — append-only engine opinions."""

from __future__ import annotations

import tempfile

from redibis.models import PIIDetection
from redibis.pii.contract_writer import PIIContractWriter
from redibis.store.generation_ledger import (
    Generation,
    GenerationLedger,
    generations_from_detection,
    generations_from_telemetry,
)
from redibis.store.storage_backend import LocalBackend


def test_ledger_is_append_only():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = GenerationLedger(LocalBackend(tmp), "contracts")
        g1 = Generation(field="pii", source="regex", value={"is_pii": True},
                        confidence=0.8, run_id="r1", ts="2026-01-01T00:00:00+00:00")
        g2 = Generation(field="pii", source="llm", value={"is_pii": True},
                        confidence=0.9, run_id="r2", ts="2026-01-02T00:00:00+00:00")
        ledger.append("db.t", "msisdn", [g1])
        ledger.append("db.t", "msisdn", [g2])
        hist = ledger.get("db.t", "msisdn")
        assert len(hist) == 2
        assert hist[0].run_id == "r1"
        assert hist[1].run_id == "r2"


def test_every_engine_that_ran_leaves_a_generation_even_when_not_detected():
    det = PIIDetection(
        column="notes", detected=False, entity_type=None, confidence=0.1,
        presidio_score=0.05, gliner_score=0.02, llm_score=0.1,
        llm_verdict="REJECTED", llm_reasoning="looks like a quantity 12345678901",
    )
    gens = generations_from_detection(det, run_id="r1", fingerprint_key="fp1")
    sources = {g.source for g in gens if g.field == "pii"}
    assert "regex" in sources
    assert "ner" in sources
    assert "llm" in sources
    assert all(_pii(g) is False or g.source == "llm" for g in gens if g.field == "pii")


def test_llm_generation_carries_scrubbed_reasoning():
    det = PIIDetection(
        column="nid", detected=True, entity_type="EG_NATIONAL_ID", confidence=0.9,
        llm_score=0.88, llm_verdict="CONFIRMED",
        llm_reasoning="national id 29501011234567 for Ahmed",
    )
    gens = generations_from_detection(det, run_id="r1")
    llm = [g for g in gens if g.source == "llm" and g.field == "pii"][0]
    reasoning = (llm.detail or {}).get("reasoning") or ""
    assert "29501011234567" not in reasoning
    assert "redacted" in reasoning.lower() or "[" in reasoning


def test_generation_fingerprint_matches_column_fingerprint_at_time():
    g = Generation(
        field="pii", source="regex", value={"is_pii": True}, confidence=0.8,
        run_id="r1", ts="t", fingerprint_key="abc123",
    )
    with tempfile.TemporaryDirectory() as tmp:
        ledger = GenerationLedger(LocalBackend(tmp), "c")
        ledger.append("db.t", "col", [g])
        got = ledger.get("db.t", "col")[0]
        assert got.fingerprint_key == "abc123"


def test_cap_keeps_most_recent_per_field_source():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = GenerationLedger(LocalBackend(tmp), "c", cap=3)
        gens = [
            Generation(field="pii", source="regex", value=i, confidence=0.1,
                       run_id=f"r{i}", ts=f"2026-01-0{i}T00:00:00+00:00")
            for i in range(1, 6)
        ]
        ledger.append("db.t", "col", gens)
        hist = ledger.get("db.t", "col", field="pii")
        assert len(hist) == 3
        assert [g.run_id for g in hist] == ["r3", "r4", "r5"]


def test_existing_telemetry_is_backfilled_as_one_generation_per_engine():
    tel = {
        "entity_type": "PHONE_NUMBER",
        "confidence": 0.81,
        "discovery_engines": ["regex", "ner"],
        "presidio_score": 0.81,
        "gliner_score": 0.22,
        "run_id": "old",
    }
    gens = generations_from_telemetry("msisdn", tel, fingerprint_key="fp")
    sources = {g.source for g in gens if g.field == "pii"}
    assert "regex" in sources
    assert "ner" in sources


def test_contract_writer_emits_undetected_and_llm():
    writer = PIIContractWriter("db", "t", run_id="r1")
    writer.add_detection(PIIDetection(
        column="x", detected=False, presidio_score=0.01, llm_score=0.2,
        llm_verdict="REJECTED", llm_reasoning="id 1234567890",
    ))
    tel = writer.build_column_telemetry()
    assert "x" in tel
    assert tel["x"]["llm_score"] == 0.2
    assert "1234567890" not in str(tel["x"].get("llm_reasoning"))
    gens = writer.build_generations()
    assert gens["x"]


def test_append_scrubs_reasoning_regardless_of_producer():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = GenerationLedger(LocalBackend(tmp), "c")
        ledger.append("t.c", "msisdn", [Generation(
            field="pii", source="llm", value={"is_pii": True}, confidence=0.9,
            run_id="r1", ts="t",
            detail={"reasoning": "msisdn 01012345678 is a phone"},
        )])
        got = ledger.get("t.c", "msisdn")[-1].detail["reasoning"]
        assert "01012345678" not in got
        assert "redacted" in got.lower() or "[" in got


def test_append_scrubs_rationale_text_and_note():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = GenerationLedger(LocalBackend(tmp), "c")
        ledger.append("t.c", "col", [Generation(
            field="pii", source="human", value={"is_pii": False}, confidence=1.0,
            run_id="h", ts="t",
            detail={"rationale_text": "nid 29501011234567", "note": "call 01012345678",
                    "reason": "ssn 1234567890"},
        )])
        d = ledger.get("t.c", "col")[-1].detail
        for key in ("rationale_text", "note", "reason"):
            assert "29501011234567" not in str(d.get(key))
            assert "01012345678" not in str(d.get(key))
            assert "1234567890" not in str(d.get(key))


def test_append_scrubs_generation_value_text():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = GenerationLedger(LocalBackend(tmp), "c")
        ledger.append("t.c", "nid", [Generation(
            field="definition", source="llm",
            value={"text": "national id 29501011234567 for Ahmed"},
            confidence=0.9, run_id="r1", ts="t",
        )])
        got = ledger.get("t.c", "nid")[-1].value
        blob = str(got)
        assert "29501011234567" not in blob
        assert "redacted" in blob.lower() or "[" in blob


def _pii(g: Generation):
    v = g.value
    if isinstance(v, dict):
        return v.get("is_pii")
    return v


def test_generations_from_contract_column_records_definition():
    from redibis.store.generation_ledger import generations_from_contract_column

    gens = generations_from_contract_column(
        "email",
        {"name": "email", "description": "customer email", "classification": "pii_personal",
         "entity_type": "EMAIL_ADDRESS", "tags": ["pii"]},
        source="llm", run_id="enrich-1",
    )
    fields = {g.field: g for g in gens}
    assert fields["definition"].value == "customer email"
    assert fields["definition"].source == "llm"
    assert fields["pii"].value["is_pii"] is True

