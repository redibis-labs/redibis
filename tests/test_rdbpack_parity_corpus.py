"""Phase 0 parity corpus — freeze detector verdicts for Pack work.

Snapshot entity / detected / confidence (and key evidence fields) on a small
English + Arabic-column table. Later pack phases must keep this corpus
bit-identical under default configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from redibis.config import PIIConfig
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii

CORPUS_PATH = (
    Path(__file__).resolve().parent / "data" / "packs" / "parity_corpus_v1.json"
)

# Fixed synthetic table: English names + Arabic context-hint columns.
_CORPUS_COLUMNS: dict[str, list] = {
    "email": ["alice@example.com", "bob@example.org", "carol@corp.eg"] * 4,
    "msisdn_plus20": ["+201007746235", "+201112345678", "+201512345678"] * 4,
    "imsi_eg": ["602010123456789", "602020123456789", "602030123456789"] * 4,
    "imei_valid": ["356938035643809", "356938035643809", "356938035643809"] * 4,
    "national_id": ["29001011234567", "29501011234567", "30001011234567"] * 4,
    "order_id": ["ORD00001", "ORD00002", "ORD00003"] * 4,
    "age": [25, 30, 35, 40] * 3,
    "رقم_قومي": ["29001011234567", "29501011234567", "30001011234567"] * 4,
    "هاتف": ["01007746235", "01012345678", "01112345678"] * 4,
}


def _round4(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def build_parity_snapshot() -> dict:
    """Run regex-only detect + decide on the corpus table."""
    df = pd.DataFrame(_CORPUS_COLUMNS)
    pii_cfg = PIIConfig(use_phonenumbers=False, geo_require_pair=True)
    dets = detect_pii(df, columns=list(df.columns), engines="regex", pii_config=pii_cfg)
    columns: dict[str, dict] = {}
    for det in dets:
        verdict = decide_pii(det, "independent", pii_cfg.thresholds)
        columns[det.column] = {
            "detected": bool(verdict.detected),
            "entity_type": verdict.entity_type,
            "confidence": _round4(verdict.confidence) or 0.0,
            "presidio_pattern": det.presidio_pattern,
            "presidio_match_rate": _round4(det.presidio_match_rate),
            "sample_match_rate": _round4(det.sample_match_rate),
        }
    return {
        "version": 1,
        "equation": "independent",
        "engines": "regex",
        "pii_config": {
            "use_phonenumbers": False,
            "geo_require_pair": True,
        },
        "columns": columns,
    }


def test_parity_corpus_matches_golden():
    """Phase 0 gate: current detector output must match the committed snapshot."""
    pytest.importorskip("presidio_analyzer")
    assert CORPUS_PATH.is_file(), f"missing golden corpus: {CORPUS_PATH}"
    golden = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    actual = build_parity_snapshot()
    assert actual["columns"] == golden["columns"], (
        "PII parity corpus drifted — update tests/data/packs/parity_corpus_v1.json "
        "deliberately if the change is intentional"
    )


def test_parity_corpus_has_arabic_and_english_columns():
    pytest.importorskip("presidio_analyzer")
    snap = build_parity_snapshot()
    assert "email" in snap["columns"]
    assert "هاتف" in snap["columns"]
    assert "رقم_قومي" in snap["columns"]
    assert snap["columns"]["email"]["detected"] is True
    assert snap["columns"]["هاتف"]["detected"] is True
