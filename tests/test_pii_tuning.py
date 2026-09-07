"""PII tuning report and LLM bundle."""

from __future__ import annotations

from pathlib import Path

from redibis.models import PIIDetection
from redibis.pii.tuning import build_pii_tuning_bundle, build_regex_tuning_report


def _det(
    column: str,
    *,
    pattern: str,
    score: float,
    match_rate: float = 1.0,
    detected: bool = True,
) -> PIIDetection:
    return PIIDetection(
        column=column,
        detected=detected,
        presidio_score=score,
        presidio_pattern=pattern,
        regex_hits=[
            {
                "pattern_name": pattern,
                "regex": r"\d+",
                "entity_type": "PHONE_NUMBER",
                "score": score,
                "match_rate": match_rate,
                "group": "structured",
            }
        ],
    )


def test_build_regex_tuning_report_never_fired_and_over_firing():
    detections = [
        _det("phone_a", pattern="noisy_pat", score=0.55, match_rate=0.3),
        _det("phone_b", pattern="noisy_pat", score=0.52, match_rate=0.4),
        _det("phone_c", pattern="noisy_pat", score=0.51, match_rate=0.35),
        _det("phone_d", pattern="noisy_pat", score=0.50, match_rate=0.2),
        _det("phone_e", pattern="noisy_pat", score=0.48, match_rate=0.25),
        _det("phone_f", pattern="rare_pat", score=0.92),
    ]
    report = build_regex_tuning_report(detections, over_fire_column_threshold=5)
    assert "noisy_pat" in report["over_firing"]
    assert "rare_pat" in report["fired"]
    assert isinstance(report["never_fired"], list)
    assert len(report["never_fired"]) > 0


def test_build_pii_tuning_bundle_writes_four_files(tmp_path):
    detections = [_det("col1", pattern="rare_pat", score=0.9)]
    root = build_pii_tuning_bundle(detections, tmp_path, table="db.t")
    assert (root / "catalog.json").is_file()
    assert (root / "ner_models.json").is_file()
    assert (root / "firing_report.json").is_file()
    assert (root / "tuning_prompt.md").is_file()
    assert (root / "apply_template.json").is_file()

    prompt = (root / "tuning_prompt.md").read_text(encoding="utf-8")
    assert "RegexOverrides" in prompt
    assert "ner_label_groups" in prompt
    assert "+201" not in prompt  # no raw phone samples
