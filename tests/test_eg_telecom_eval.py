"""Egypt telecom eval harness — per-entity precision/recall/F1 gates."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd
import pytest

from redibis.config import PIIConfig
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii

_LABELS = Path(__file__).resolve().parent / "data" / "eg_telecom_eval" / "labels.json"


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def test_eg_telecom_eval_f1_thresholds():
    pytest.importorskip("presidio_analyzer")
    spec = json.loads(_LABELS.read_text(encoding="utf-8"))
    rows: dict[str, list] = {}
    labels: dict[str, dict] = {}
    for col in spec["columns"]:
        name = col["name"]
        rows[name] = col["values"] * 7
        labels[name] = col

    max_len = max(len(v) for v in rows.values())
    for name, vals in rows.items():
        if len(vals) < max_len:
            reps = (max_len + len(vals) - 1) // len(vals)
            rows[name] = (vals * reps)[:max_len]

    df = pd.DataFrame(rows)
    pii_cfg = PIIConfig(use_phonenumbers=False, geo_require_pair=True)
    dets = detect_pii(df, columns=list(df.columns), engines="regex", pii_config=pii_cfg)
    verdicts = {
        d.column: decide_pii(d, "independent", pii_cfg.thresholds)
        for d in dets
    }

    entity_tp: dict[str, int] = defaultdict(int)
    entity_fp: dict[str, int] = defaultdict(int)
    entity_fn: dict[str, int] = defaultdict(int)

    for col, meta in labels.items():
        v = verdicts[col]
        expected_pii = bool(meta["is_pii"])
        expected_entity = meta.get("entity")
        predicted_pii = v.detected
        predicted_entity = v.entity_type if v.detected else None

        if expected_pii and expected_entity:
            if predicted_pii and predicted_entity == expected_entity:
                entity_tp[expected_entity] += 1
            elif predicted_pii and predicted_entity != expected_entity:
                entity_fp[predicted_entity or "UNKNOWN"] += 1
                entity_fn[expected_entity] += 1
            else:
                entity_fn[expected_entity] += 1
        else:
            if predicted_pii:
                entity_fp[predicted_entity or "UNKNOWN"] += 1

    for entity, gates in spec["thresholds"].items():
        tp = entity_tp.get(entity, 0)
        fp = entity_fp.get(entity, 0)
        expected_cols = sum(
            1 for c in spec["columns"]
            if c.get("entity") == entity and c.get("is_pii")
        )
        fn = entity_fn.get(entity, 0)
        prec, rec, f1 = _prf(tp, fp, fn)
        assert prec >= gates["precision_min"], f"{entity} precision {prec:.2f} < {gates['precision_min']}"
        assert rec >= gates["recall_min"], f"{entity} recall {rec:.2f} < {gates['recall_min']} (tp={tp} expected={expected_cols})"
        assert f1 >= gates["f1_min"], f"{entity} f1 {f1:.2f} < {gates['f1_min']}"


def test_telecom_evidence_raw_sample_default():
    from redibis.pii.telecom_evidence import build_telecom_evidence

    values = ["+201007746235", "+201112345678"]
    profile = build_telecom_evidence("mobile", values, sample_policy="raw", sample_n=2)
    assert profile["sample_policy"] == "raw"
    assert profile["sample"] == values[:2]
    assert profile["msisdn_valid_rate"] == 1.0
    assert "phonenumbers" in profile


def test_telecom_evidence_masked_policy_recorded_not_applied():
    from redibis.pii.telecom_evidence import build_telecom_evidence

    masked = ["***6235", "***5678"]
    profile = build_telecom_evidence(
        "mobile", masked, sample_policy="masked", sample_n=2
    )
    assert profile["sample_policy"] == "masked"
    assert profile["sample"] == masked
    assert profile["msisdn_valid_rate"] == 0.0


def test_deep_scan_telecom_producer_writes_evidence(tmp_path):
    pytest.importorskip("presidio_analyzer")
    import pandas as pd
    from redibis.agents.deep_scan import _produce_telecom_evidence
    from redibis.agents.deep_profile import ProfilingCapability

    df = pd.DataFrame({"mobile": ["+201007746235", "+201112345678"]})
    cap = ProfilingCapability(
        id="telecom.evidence",
        engine="telecom",
        label="Telecom",
        description="",
        params=(),
        output="telecom_evidence",
        cost="low",
    )
    art = _produce_telecom_evidence(
        df=df,
        table="telecom.customers",
        run_id="run-1",
        span_id="",
        cap=cap,
        config=None,
        params={"sample_policy": "raw"},
        run_dir=tmp_path,
    )
    json_path, md_path = art.write(tmp_path)
    assert json_path.exists()
    assert md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["columns"]["mobile"]["sample_policy"] == "raw"
    assert "+201007746235" in payload["columns"]["mobile"]["sample"]
