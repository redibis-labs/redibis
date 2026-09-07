from redibis.models import PIIDetection
from redibis.pii.report_writer import render_pii_detection_report_html


def test_html_report_shows_all_engine_states_and_phone_result():
    detection = PIIDetection(
        column="contact_phone",
        detected=True,
        entity_type="PHONE_NUMBER",
        confidence=0.96,
        presidio_score=0.88,
        presidio_pattern="msisdn_egypt_any_format",
        phone_score=0.96,
        phone_entity="PHONE_NUMBER",
        msisdn_valid_rate=0.96,
        phone_valid_rate=1.0,
        phone_mobile_rate=1.0,
        phone_regions={"EG": 8},
        engine_states={
            "regex": {"enabled": True, "ran": True, "status": "matched"},
            "ner": {"enabled": True, "available": True, "ran": False, "status": "skipped_high_regex"},
            "phone": {"enabled": True, "ran": True, "status": "matched"},
        },
    )

    html = render_pii_detection_report_html([detection], "telecom.customers")

    assert "regex</strong>: matched" in html
    assert "ner</strong>: skipped_high_regex" in html
    assert "llm</strong>: not_run" in html
    assert "phone</strong>: matched" in html
    assert "msisdn 96.0%" in html
    assert "valid 100.0%" in html
    assert "regions EG" in html


def test_html_report_renders_decision_rule_and_evidence_cards():
    detection = PIIDetection(
        column="email_address",
        detected=True,
        entity_type="EMAIL_ADDRESS",
        confidence=0.88,
        decision_path="skipped_by_triage",
        regex_hits=[
            {
                "pattern_name": "email_like",
                "regex": r"<[^>]+>@example\.com",
                "entity_type": "EMAIL_ADDRESS",
                "score": 0.92,
                "match_rate": 0.75,
                "collision_group": "contact",
                "validator": "email",
            }
        ],
        ner_hits=[
            {
                "model": "gliner-small",
                "label": "EMAIL_ADDRESS",
                "score": 0.81,
                "match_rate": 0.60,
            }
        ],
    )
    detection.decision_rule = "regex >= 0.80"

    html = render_pii_detection_report_html([detection], "telecom.customers")

    assert "regex &gt;= 0.80" in html
    assert "🔍 Detection Evidence" in html
    assert "email_like" in html
    assert "&lt;[^&gt;]+&gt;@example\\.com" in html
    assert "gliner-small" in html
