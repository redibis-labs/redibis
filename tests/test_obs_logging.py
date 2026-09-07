"""Tests for redibis.obs logging foundation (Phase 1)."""

from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path

import pytest

from redibis.obs import (
    DecisionRecord,
    bind_context,
    decision,
    persist_run_log,
    sanitize_inputs,
    setup_logging,
)
from redibis.obs.decision import (
    decision_log_enabled,
    sanitize_text,
    set_decision_log_enabled,
)
from redibis.services.run_logging import capture_run_log


@pytest.fixture(autouse=True)
def _reset_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_child_levels: dict[str, int] = {}
    for name in ("great_expectations", "redibis.pii", "redibis.decision", "redibis.test"):
        lg = logging.getLogger(name)
        saved_child_levels[name] = lg.level
        lg.handlers = []
    set_decision_log_enabled(True)
    yield
    root.handlers.clear()
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    for name, lvl in saved_child_levels.items():
        logging.getLogger(name).setLevel(lvl)
    set_decision_log_enabled(True)


def test_setup_logging_json_debug_context_fields():
    setup_logging(level="DEBUG", fmt="json", force=True)
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    console_handlers = [
        h for h in root.handlers
        if getattr(h, "redibis_handler_kind", None) == "redibis.obs.console"
    ]
    assert len(console_handlers) == 1

    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(console_handlers[0].formatter)
    handler.addFilter(console_handlers[0].filters[0])
    log = logging.getLogger("redibis.test")
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    with bind_context(run_id="r1", table="db.t", column="email", fn="scan"):
        log.info("hello")

    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["run_id"] == "r1"
    assert payload["table"] == "db.t"
    assert payload["column"] == "email"
    assert payload["fn"] == "scan"
    assert "pid" in payload
    assert "thread" in payload


def test_plain_format_shows_correlation_ids(capsys):
    setup_logging(level="INFO", fmt="plain", force=True)
    with bind_context(run_id="r99", table="db.orders", column="", fn="scan"):
        logging.getLogger("redibis.test").info("hello")
    captured = capsys.readouterr()
    assert "[r99/db.orders]" in captured.err


def test_module_levels_override():
    setup_logging(
        level="DEBUG",
        fmt="plain",
        force=True,
        module_levels={"great_expectations": "WARNING", "redibis.pii": "DEBUG"},
    )
    assert logging.getLogger("great_expectations").level == logging.WARNING
    assert logging.getLogger("redibis.pii").level == logging.DEBUG


def test_setup_logging_idempotent():
    setup_logging(level="INFO", fmt="plain", force=True)
    setup_logging(level="INFO", fmt="plain", force=False)
    assert len([h for h in logging.getLogger().handlers
                if getattr(h, "redibis_handler_kind", None) == "redibis.obs.console"]) == 1


def test_setup_logging_preserves_capture_handler():
    setup_logging(level="INFO", fmt="plain", force=True)
    with capture_run_log() as buf:
        setup_logging(level="INFO", fmt="plain", force=True)
        logging.getLogger("redibis.test").info("still captured")
    assert "still captured" in buf.getvalue()


def test_console_filters_debug_during_capture(capsys):
    setup_logging(level="INFO", fmt="plain", force=True)
    with capture_run_log() as cap_buf:
        logging.getLogger("great_expectations.test").debug("ge debug noise")
        logging.getLogger("redibis.test").info("visible info")
    captured = capsys.readouterr()
    assert "ge debug noise" not in captured.err
    assert "visible info" in captured.err
    cap_buf.seek(0)
    assert "ge debug noise" in cap_buf.read()


def test_persist_run_log_writes_file(tmp_path):
    setup_logging(level="DEBUG", fmt="plain", force=True)
    dest_dir = tmp_path / "run"
    with persist_run_log(dest_dir, "demo.t", "run-1") as _buf:
        logging.getLogger("redibis.test").info("captured line")
    log_file = dest_dir / "demo.t.run-1.log"
    assert log_file.is_file()
    assert "captured line" in log_file.read_text(encoding="utf-8")


def test_decision_sanitize_no_raw_values():
    rec = DecisionRecord(
        stage="pii",
        fn="pii.equations.decide_pii",
        table="db.t",
        column="phone",
        verdict="pii",
        confidence=0.9,
        rule="regex >= 0.8",
        inputs={
            "score": 0.95,
            "threshold": 0.8,
            "pattern": "PHONE_NUMBER",
            "email_leak": "user@example.com",
            "match_rate": 0.42,
        },
    )
    buf = StringIO()
    setup_logging(level="INFO", fmt="plain", force=True)
    handler = logging.StreamHandler(buf)
    logging.getLogger("redibis.decision").handlers = [handler]
    logging.getLogger("redibis.decision").setLevel(logging.INFO)
    decision(rec)
    text = buf.getvalue()
    assert "user@example.com" not in text
    clean = sanitize_inputs(rec.inputs)
    assert "email_leak" not in clean
    assert clean["match_rate"] == 0.42


def test_sanitize_preserves_regex_and_pattern_metadata():
    long_regex = (
        r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        r"|^\+?1?\d{9,15}$"
    )
    clean = sanitize_inputs({
        "pattern_name": "EMAIL_ADDRESS",
        "regex": long_regex,
        "match_count": 7,
        "collision_group": "contact",
        "validator": "presidio",
        "group": "pii",
        "value": "raw-cell-should-drop",
        "sample": "also-drop",
    })
    assert clean["pattern_name"] == "EMAIL_ADDRESS"
    assert "@" in clean["regex"]
    assert clean["regex"].startswith("^")
    assert clean["match_count"] == 7
    assert "value" not in clean
    assert "sample" not in clean


def test_sanitize_text_redacts_value_like_rule():
    assert sanitize_text("matched user@evil.com in column") == "[redacted]"
    assert sanitize_text("regex >= 0.8") == "regex >= 0.8"


def test_decision_log_toggle():
    set_decision_log_enabled(False)
    assert not decision_log_enabled()
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    logging.getLogger("redibis.decision").handlers = [handler]
    logging.getLogger("redibis.decision").setLevel(logging.INFO)
    decision(DecisionRecord(
        stage="pii", fn="f", table="t", column="c",
        verdict="pii", confidence=1.0, rule="r",
    ))
    assert buf.getvalue() == ""

    set_decision_log_enabled(True)
    decision(DecisionRecord(
        stage="pii", fn="f", table="t", column="c",
        verdict="pii", confidence=1.0, rule="r",
    ))
    assert "DECISION" in buf.getvalue()


def test_decision_rule_field_sanitized_in_output():
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    logging.getLogger("redibis.decision").handlers = [handler]
    logging.getLogger("redibis.decision").setLevel(logging.INFO)
    set_decision_log_enabled(True)
    decision(DecisionRecord(
        stage="pii",
        fn="f",
        table="t",
        column="email",
        verdict="pii: user@leak.com",
        confidence=1.0,
        rule="hit user@leak.com",
    ))
    text = buf.getvalue()
    assert "user@leak.com" not in text
    assert "[redacted]" in text
