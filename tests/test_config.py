"""Tests for redibis.config — RedibisConfig YAML spine and legacy adapters."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redibis.pipeline_config import to_pipeline_config
from redibis.cli.overrides import apply_cli_overrides
from redibis.config import ConfigError, GlinerConfig, NERConfig, RedibisConfig, deep_merge
from redibis.pii.thresholds import DEFAULT_EQUATION
from redibis.quality.rule_set import QualityRuleSet
from redibis.services.scan_service import ScanConfig, from_scan_config, to_scan_config
from redibis.services.session_service import GlobalConfig, from_global_config, to_global_config


def test_default_equation_is_independent():
    cfg = RedibisConfig.default()
    assert cfg.pii.equation_mode == DEFAULT_EQUATION == "independent"


def test_yaml_round_trip(tmp_path):
    path = tmp_path / "redibis.yaml"
    src = RedibisConfig.default()
    src.table = "telecom.customers"
    src.pii.equation_mode = "balanced"
    src.profiling.engine = "great_expectations"
    src.quality.rule_set = QualityRuleSet(name="tpl", rules=[
        {"rule": "expect_column_values_to_not_be_null", "column": "phone", "kwargs": {}},
    ])
    src.to_yaml(path)

    loaded = RedibisConfig.from_yaml(path)
    assert loaded.table == "telecom.customers"
    assert loaded.pii.equation_mode == "balanced"
    assert loaded.profiling.engine == "great_expectations"
    assert loaded.quality.rule_set.name == "tpl"
    assert len(loaded.quality.rule_set.rules) == 1


def test_partial_yaml_deep_merges_defaults(tmp_path):
    path = tmp_path / "partial.yaml"
    path.write_text(yaml.safe_dump({
        "table": "db.table",
        "pii": {"equation_mode": "strict"},
        "contract": {"automerge": "pii"},
    }), encoding="utf-8")

    cfg = RedibisConfig.from_yaml(path)
    assert cfg.table == "db.table"
    assert cfg.pii.equation_mode == "strict"
    assert cfg.contract.automerge == "pii"
    assert cfg.pii.engines == "both"
    assert cfg.profiling.engine == "great_expectations"


def test_deep_merge_nested():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = deep_merge(base, {"a": {"y": 99}, "c": 4})
    assert out == {"a": {"x": 1, "y": 99}, "b": 3, "c": 4}


def test_dump_default_yaml(tmp_path):
    path = tmp_path / "defaults.yaml"
    RedibisConfig.dump_default_yaml(path)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["scan_types"] == ["profile", "quality", "pii"]
    assert loaded["pii"]["equation_mode"] == "independent"
    assert "ner" in loaded["pii"]
    assert loaded["pii"]["ner"]["type"] == "gliner"
    assert loaded["pii"]["ner"]["model_path"] == ""
    assert loaded["pii"]["models_dir"] == "/models"


def test_scan_config_adapter_roundtrip():
    sc = ScanConfig(
        table="telecom.customers",
        equation_mode="lenient",
        run_pii=True,
        run_quality=False,
        automerge="pii",
        gliner_config=GlinerConfig(model_id="custom/model"),
        pii_engines="regex",
    )
    rb = from_scan_config(sc)
    assert rb.table == "telecom.customers"
    assert rb.pii.equation_mode == "lenient"
    assert rb.pii.engines == "regex"
    assert "pii" in rb.scan_types
    assert "quality" not in rb.scan_types

    back = to_scan_config(rb)
    assert back.table == sc.table
    assert back.equation_mode == sc.equation_mode
    assert back.run_pii == sc.run_pii
    assert back.run_quality == sc.run_quality
    assert back.automerge == sc.automerge
    assert back.gliner_config.model_id == "custom/model"


def test_from_global_config_auto_merge_contract_does_not_force_automerge():
    gc = GlobalConfig(
        scan_mode="both",
        auto_merge_contract=True,
        automerge="none",
    )
    with pytest.warns(DeprecationWarning, match="auto_merge_contract"):
        rb = from_global_config(gc)
    assert rb.contract.automerge == "none"


def test_global_config_automerges_does_not_honor_legacy_boolean():
    gc = GlobalConfig(auto_merge_contract=True, automerge="none")
    with pytest.warns(DeprecationWarning, match="auto_merge_contract"):
        assert gc.automerges("quality") is False
    gc2 = GlobalConfig(auto_merge_contract=True, automerge="both")
    assert gc2.automerges("quality") is True


def test_global_config_adapter():
    gc = GlobalConfig(
        scan_mode="pii",
        equation_mode="independent",
        automerge="none",
        pii_engines="gliner",
        masking_default_locale="ar",
    )
    rb = from_global_config(gc)
    assert rb.scan_types == ["pii"]
    assert rb.pii.engines == "gliner"
    assert rb.masking.default_locale == "ar"

    back = to_global_config(rb, table="ignored")
    assert back.scan_mode == "pii"
    assert back.pii_engines == "gliner"
    assert back.masking_default_locale == "ar"


def test_to_scan_config_auto_write_does_not_force_automerge():
    cfg = RedibisConfig.default()
    cfg.table = "t.t"
    cfg.contract.automerge = "pii"
    with pytest.warns(DeprecationWarning):
        sc = to_scan_config(cfg, auto_write=True)
    assert sc.automerge == "pii"


def test_scan_config_uses_canonical_gliner_config():
    cfg = RedibisConfig.default()
    cfg.table = "t.t"
    sc = to_scan_config(cfg)
    assert isinstance(sc.gliner_config, GlinerConfig)
    assert isinstance(sc.ner_config, NERConfig)


def test_apply_cli_overrides():
    cfg = RedibisConfig.default()
    args = type("Args", (), {
        "table": "cli.table",
        "mode": "pii,quality",
        "equation": "strict",
        "automerge": "pii",
        "scan_output_dir": "/tmp/out",
        "use_s3": False,
    })()
    apply_cli_overrides(cfg, args)
    assert cfg.table == "cli.table"
    assert cfg.scan_types == ["profile", "quality", "pii"]
    assert cfg.pii.equation_mode == "strict"
    assert cfg.contract.automerge == "pii"
    assert str(cfg.report.output_dir) == "/tmp/out"


@pytest.mark.parametrize("field,value,match", [
    ("profiling", {"engine": "not_real"}, "profiler engine"),
    ("pii", {"equation_mode": "bogus"}, "equation mode"),
    ("pii", {"engines": "all"}, "pii engines"),
    ("contract", {"automerge": "everything"}, "automerge"),
    ("scan_types", ["profile", "bogus"], "scan_types"),
])
def test_validate_rejects_unknown_values(field, value, match):
    data = {field: value}
    with pytest.raises(ConfigError, match=match):
        RedibisConfig.from_dict(data)


def test_validate_accepts_default_config():
    RedibisConfig.default().validate()


def test_openmetadata_entity_mode_accepts_legacy_mode_key():
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = RedibisConfig.from_dict({
            "catalog": {"openmetadata": {"mode": "enrich_existing"}},
        })
    assert cfg.catalog.openmetadata.entity_mode == "enrich_existing"
    assert any("entity_mode" in str(w.message) for w in caught)
    # New key wins without warning when only entity_mode is set
    with warnings.catch_warnings(record=True) as caught2:
        warnings.simplefilter("always")
        cfg2 = RedibisConfig.from_dict({
            "catalog": {"openmetadata": {"entity_mode": "create_if_missing"}},
        })
    assert cfg2.catalog.openmetadata.entity_mode == "create_if_missing"
    assert not any("entity_mode" in str(w.message) for w in caught2)


def test_to_pipeline_config():
    cfg = RedibisConfig.default()
    cfg.table = "db.t"
    cfg.storage.runs_bucket = "runs"
    pc = to_pipeline_config(cfg)
    assert pc.table == "db.t"
    assert pc.runs_bucket == "runs"
    assert pc.equation == "independent"
