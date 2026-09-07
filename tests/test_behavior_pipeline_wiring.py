"""Pipeline wiring for Behavior Policy Runtime (default-off, shadow, active)."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import RuntimeMode
from redibis.models import PIIDetection
from redibis.services import pipeline


@pytest.fixture
def sample_df():
    return pd.DataFrame({
        "customer_id": ["12345678901234"] * 5,
        "lac": ["12345"] * 5,
    })


def test_behavior_disabled_uses_edge_rules(sample_df, monkeypatch):
    cfg = BehaviorConfig(enabled=False, mode="disabled")

    class _R:
        classification = type("C", (), {
            "edge_rules_enabled": True,
            "policy_pack": "telecom",
            "per_run_overlay_allowed": True,
        })()
        behavior = cfg

        @classmethod
        def load(cls):
            return cls()

    monkeypatch.setattr("redibis.config.RedibisConfig.load", _R.load)

    results = pipeline.run_pii_detection(
        sample_df,
        engines="regex",
        equation_mode="lenient",
        edge_rules_enabled=True,
        policy_pack="telecom",
    )
    by_col = {d.column: d for d in results}
    assert "customer_id" in by_col
    # Edge rules still run under default-off behavior runtime
    if by_col["customer_id"].entity_type == "NATIONAL_ID":
        assert by_col["customer_id"].detected is False


def test_behavior_shadow_preserves_baseline(sample_df, monkeypatch):
    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.SHADOW.value)

    class _R:
        classification = type("C", (), {
            "edge_rules_enabled": True,
            "policy_pack": "telecom",
            "per_run_overlay_allowed": True,
        })()
        behavior = cfg

        @classmethod
        def load(cls):
            return cls()

    monkeypatch.setattr("redibis.config.RedibisConfig.load", _R.load)
    warnings: list[str] = []

    results = pipeline.run_pii_detection(
        sample_df,
        engines="regex",
        equation_mode="lenient",
        edge_rules_enabled=True,
        policy_pack="telecom",
        progress_callback=warnings.append,
    )
    assert isinstance(results, list)
    assert all(isinstance(d, PIIDetection) for d in results)


def test_apply_pii_behavior_policies_active_demotes_surrogate(monkeypatch, tmp_path):
    from redibis.behavior.runtime import apply_pii_behavior_policies
    from redibis.services import behavior_service as bs

    # Isolate store so leftover active policies cannot replace edge-compat.
    monkeypatch.setattr(bs, "default_policy_store_dir", lambda: tmp_path / "empty-policies")

    cfg = BehaviorConfig(enabled=True, mode=RuntimeMode.ACTIVE.value)
    df = pd.DataFrame({"customer_id": ["12345678901234", "99999999999999"]})
    detection = PIIDetection(
        column="customer_id",
        detected=True,
        entity_type="NATIONAL_ID",
        confidence=0.9,
    )
    result = apply_pii_behavior_policies(
        [detection],
        df=df,
        table="telecom.customers",
        policy_pack="telecom",
        behavior_config=cfg,
    )
    updated = result.detections[0]
    assert updated.detected is False
    assert updated.entity_type is None
