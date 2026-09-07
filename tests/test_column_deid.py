"""Plan C — column de-id via shared DeidApplier."""

from __future__ import annotations

import pandas as pd
import pytest

from redibis.masking.engine import RunKeys
from redibis.pii.deid.applier import DeidApplier
from redibis.pii.deid.policy import DeidPolicy, EntityRule
from redibis.pii.scan.column_scanner import ColumnScanner
from redibis.pii.scan.result import Detection, DetectionResult


def _column_result(
    column: str,
    et: str = "EMAIL_ADDRESS",
    *,
    score: float = 0.95,
    detected: bool = True,
) -> DetectionResult:
    return DetectionResult(
        kind="column",
        detections=(
            Detection(
                entity_type=et,
                score=score,
                engine="regex",
                start=None,
                end=None,
                text=column,
                detected=detected,
            ),
        ),
        entity_counts={et: 1},
        ruleset_id="t",
        ruleset_version="1",
        language="en",
    )


def test_column_redact():
    df = pd.DataFrame({"email": ["a@b.co", "c@d.co"], "ok": ["x", "y"]})
    result = _column_result("email")
    out = DeidApplier().apply(df, result, DeidPolicy.redact_all())
    assert out.kind == "column"
    assert out.deidentified_frame is not None
    assert list(out.deidentified_frame["email"]) == ["[EMAIL_ADDRESS]", "[EMAIL_ADDRESS]"]
    assert list(out.deidentified_frame["ok"]) == ["x", "y"]
    assert out.applied[0].column == "email"
    assert "a@b.co" not in str(out.to_dict()["deidentified_frame"])


def test_column_fail_closed_no_policy():
    df = pd.DataFrame({"email": ["a@b.co"]})
    result = _column_result("email")
    with pytest.raises(ValueError, match="fail-closed|required"):
        DeidApplier().apply(df, result, None)  # type: ignore[arg-type]


def test_column_keys_never_in_output():
    df = pd.DataFrame({"email": ["a@b.co"]})
    result = _column_result("email")
    keys = RunKeys.mint(seed="col-deid-secret-seed")
    out = DeidApplier().apply(df, result, DeidPolicy.redact_all(), keys=keys)
    dumped = str(out.to_dict())
    assert "master_key" not in dumped
    assert keys.seed not in dumped
    assert keys.master_key_hex not in dumped
    assert out.run_key_ref == keys.run_id


def test_text_column_parity_same_value():
    """Same value + keys + strategy → identical transform (engine parity)."""
    value = "SECRET1234"
    keys = RunKeys.mint(seed="parity-seed")
    policy = DeidPolicy(
        id="p",
        default=EntityRule(
            entity_type="*",
            strategy="mask",
            params={"keep_last": 4, "keep_first": 0},
        ),
    )
    text = f"xx {value} yy"
    span_result = DetectionResult(
        kind="span",
        detections=(
            Detection("CREDIT_CARD", 0.95, "regex", 3, 13, value),
        ),
        entity_counts={"CREDIT_CARD": 1},
        ruleset_id="t",
        ruleset_version="1",
        language="en",
    )
    span_out = DeidApplier().apply(text, span_result, policy, keys=keys)
    assert span_out.deidentified_text.startswith("xx ")
    assert span_out.deidentified_text.endswith(" yy")
    span_transformed = span_out.deidentified_text[3:-3]

    df = pd.DataFrame({"cc": [value]})
    col_result = _column_result("cc", "CREDIT_CARD")
    col_out = DeidApplier().apply(df, col_result, policy, keys=keys)
    col_val = str(col_out.deidentified_frame["cc"].iloc[0])
    assert col_val == span_transformed
    assert value not in col_val


def test_override_beats_default_on_column():
    df = pd.DataFrame({"email": ["a@b.co"]})
    result = _column_result("email")
    policy = DeidPolicy(
        id="p",
        default=EntityRule(entity_type="*", strategy="redact"),
        overrides=(
            EntityRule(entity_type="EMAIL_ADDRESS", strategy="passthrough"),
        ),
    )
    out = DeidApplier().apply(df, result, policy)
    assert out.deidentified_frame["email"].iloc[0] == "a@b.co"


def test_only_detected_skips_evidence_only():
    df = pd.DataFrame({"email": ["a@b.co"]})
    result = _column_result("email", detected=False)
    out = DeidApplier().apply(
        df, result, DeidPolicy.redact_all(), only_detected=True
    )
    assert out.deidentified_frame["email"].iloc[0] == "a@b.co"
    assert out.applied == ()


def test_column_scanner_scan_and_deidentify():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"email": ["person@example.com"] * 8, "id": list(range(8))})
    policy = DeidPolicy.redact_all("scan-deid")
    scanner = ColumnScanner()
    result, deid = scanner.scan_and_deidentify(
        df, policy, columns=["email", "id"], engines="regex"
    )
    assert result.kind == "column"
    assert deid.kind == "column"
    assert deid.deidentified_frame is not None
    # email column should be touched if detected as PII
    email_det = next((d for d in result.detections if d.text == "email"), None)
    if email_det and email_det.detected:
        assert "person@example.com" not in list(deid.deidentified_frame["email"])
