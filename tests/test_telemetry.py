"""Tests for OTel + RAI telemetry layer."""

from __future__ import annotations

import pytest

from redibis.config import RAIConfig
from redibis.telemetry import RAIMiddleware, run_context


def test_run_context_isolates_spans():
    with run_context("run-a") as tel_a:
        with tel_a.span("step.one"):
            pass
        export_a = tel_a.export()

    with run_context("run-b") as tel_b:
        with tel_b.span("step.two"):
            pass
        export_b = tel_b.export()

    assert len(export_a) == 1
    assert export_a[0]["name"] == "step.one"
    assert export_a[0]["run_id"] == "run-a"
    assert len(export_b) == 1
    assert export_b[0]["run_id"] == "run-b"


def test_rai_hard_blocks_external_raw_pii_even_in_report_mode():
    rai = RAIMiddleware(RAIConfig(mode="report", hard_block_external_pii=True))

    with pytest.raises(PermissionError, match="raw PII blocked"):
        rai.wrap_model_call(
            lambda: "ok",
            model_id="gpt-4",
            residency="public",
            contains_raw_pii=True,
        )


def test_rai_reports_external_raw_pii_when_hard_block_disabled():
    rai = RAIMiddleware(RAIConfig(mode="report", hard_block_external_pii=False))

    result = rai.wrap_model_call(
        lambda: "ok",
        model_id="gpt-4",
        residency="public",
        contains_raw_pii=True,
    )
    assert result == "ok"
    report = rai.report()
    assert report["advisory_count"] == 1
    assert "raw PII" in report["advisories"][0]["reason"]


def test_rai_enforce_blocks_allowlist_violation():
    rai = RAIMiddleware(RAIConfig(enforce=True, allowed_models=["local-llm"]))

    with pytest.raises(PermissionError, match="not in allow-list"):
        rai.wrap_model_call(
            lambda: "ok",
            model_id="gpt-4",
            residency="local",
            contains_raw_pii=False,
        )


def test_rai_allows_local_raw_pii():
    rai = RAIMiddleware(RAIConfig(hard_block_external_pii=True))
    result = rai.wrap_model_call(
        lambda: "ok",
        model_id="local-llm",
        residency="local",
        contains_raw_pii=True,
    )
    assert result == "ok"
    assert rai.report()["advisory_count"] == 0


def test_rai_disabled_passthrough():
    rai = RAIMiddleware(RAIConfig(enabled=False, enforce=True))
    result = rai.wrap_model_call(
        lambda: "ok",
        model_id="gpt-4",
        residency="public",
        contains_raw_pii=True,
    )
    assert result == "ok"
    assert rai.report()["advisory_count"] == 0
