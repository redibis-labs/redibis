"""Phase 3 — apply_packs resolver, overlay semantics, layer audit, scan parity."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from redibis.config import PackSourceConfig, RedibisConfig
from redibis.models import RunMetadata
from redibis.obs.decision import sanitize_inputs
from redibis.pack import (
    PackStackStore,
    apply_packs,
    apply_packs_from_config,
    export_default_pack,
    load_pack,
    write_pack,
)
from redibis.pack.config_allowlist import extract_portable_config
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii

GOLDEN_DEFAULT = (
    Path(__file__).resolve().parent / "data" / "packs" / "redibis-default.rdbpack"
)


def _regex_snapshot(pii_cfg) -> dict:
    from tests import test_rdbpack_parity_corpus as corpus

    df = pd.DataFrame(corpus._CORPUS_COLUMNS)
    dets = detect_pii(
        df,
        columns=list(df.columns),
        engines="regex",
        pii_config=pii_cfg,
    )
    columns: dict[str, dict] = {}
    for det in dets:
        verdict = decide_pii(det, pii_cfg.equation_mode, pii_cfg.thresholds)
        columns[det.column] = {
            "detected": bool(verdict.detected),
            "entity_type": verdict.entity_type,
            "confidence": round(float(verdict.confidence or 0), 4),
            "presidio_pattern": det.presidio_pattern,
            "presidio_match_rate": (
                round(float(det.presidio_match_rate), 4)
                if det.presidio_match_rate is not None
                else None
            ),
            "sample_match_rate": (
                round(float(det.sample_match_rate), 4)
                if det.sample_match_rate is not None
                else None
            ),
        }
    return columns


def test_t3_default_pack_scan_parity():
    """Applying redibis-default must not change detector verdicts vs stock config."""
    pytest.importorskip("presidio_analyzer")
    assert GOLDEN_DEFAULT.is_file()
    stock = RedibisConfig.default()
    applied = apply_packs(stock, [GOLDEN_DEFAULT], include_builtin_default=False)
    assert extract_portable_config(stock) == extract_portable_config(applied.config)
    assert _regex_snapshot(stock.pii) == _regex_snapshot(applied.config.pii)


def test_t5_overlay_only_changes_declared_keys(tmp_path: Path):
    overlay_manifest = {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {"id": "overlay-demo", "version": "1.0.0"},
        "requires": {"redibis": ">=0.5,<1"},
        "contents": {"config": True},
        "mode": "overlay",
    }
    sections = {
        "config/redibis.yaml": {
            "pii": {"equation_mode": "balanced", "sample_size": 42},
        },
    }
    pack_path = tmp_path / "overlay.rdbpack"
    write_pack(pack_path, overlay_manifest, sections)

    base = RedibisConfig.default()
    before = extract_portable_config(base)
    stack = apply_packs(base, [pack_path], include_builtin_default=False)
    after = extract_portable_config(stack.config)

    assert after["pii"]["equation_mode"] == "balanced"
    assert after["pii"]["sample_size"] == 42
    # Untouched sibling keys remain identical.
    assert after["scan_types"] == before["scan_types"]
    assert after["profiling"] == before["profiling"]
    assert after["quality"] == before["quality"]
    assert after["contract"] == before["contract"]
    assert after["pii"]["engines"] == before["pii"]["engines"]
    assert after["pii"]["thresholds"] == before["pii"]["thresholds"]


def test_t9_layer_audit_on_stack_and_run_metadata(tmp_path: Path):
    pack_path = tmp_path / "layer.rdbpack"
    write_pack(
        pack_path,
        {
            "apiVersion": "redibis.io/pack/v1",
            "kind": "RedibisPack",
            "metadata": {"id": "layer-a", "version": "0.1.0"},
            "requires": {"redibis": ">=0.5,<1"},
            "contents": {"config": True},
            "mode": "overlay",
        },
        {"config/redibis.yaml": {"report": {"formats": ["pii"]}}},
    )
    stack = apply_packs(RedibisConfig.default(), [pack_path], include_builtin_default=True)
    audit = stack.layer_audit()
    assert len(audit) == 2
    assert audit[0]["id"] == "redibis-default"
    assert audit[0]["source"] == "builtin"
    assert audit[1]["id"] == "layer-a"
    assert audit[1]["checksum"]
    assert audit[1]["version"] == "0.1.0"

    meta = RunMetadata(pack_layers=audit)
    assert meta.pack_layers[1]["identity"] == "layer-a@0.1.0"

    cleaned = sanitize_inputs({"pack_layers": audit, "score": 0.9})
    assert "pack_layers" in cleaned
    assert cleaned["score"] == 0.9


def test_apply_packs_from_config(tmp_path: Path):
    pack_path = tmp_path / "from-cfg.rdbpack"
    write_pack(
        pack_path,
        {
            "apiVersion": "redibis.io/pack/v1",
            "kind": "RedibisPack",
            "metadata": {"id": "cfg-ref", "version": "1.0.0"},
            "requires": {"redibis": ">=0.5,<1"},
            "contents": {"config": True},
            "mode": "overlay",
        },
        {"config/redibis.yaml": {"pii": {"sample_size": 7}}},
    )
    cfg = RedibisConfig.default()
    cfg.packs = [PackSourceConfig(path=str(pack_path), mode="overlay")]
    stack = apply_packs_from_config(cfg)
    assert stack.config.pii.sample_size == 7
    assert any(L.id == "cfg-ref" for L in stack.layers)


def test_stack_import_list_remove(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    pack_path = tmp_path / "imp.rdbpack"
    export_default_pack(pack_path)
    store = PackStackStore()
    dry = store.import_pack(pack_path, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["identity"].startswith("redibis-default@")
    assert store.list_layers() == []

    report = store.import_pack(pack_path, dry_run=False)
    assert report["imported"] is True
    layers = store.list_layers()
    assert len(layers) == 1
    identity = layers[0].identity
    removed = store.remove(identity)
    assert removed["ok"] is True
    assert store.list_layers() == []


def test_cli_rdbpack_import_list_remove(tmp_path: Path, monkeypatch):
    from redibis.cli.main import main

    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    pack_path = tmp_path / "c.rdbpack"
    export_default_pack(pack_path)
    assert main(["rdbpack", "import", str(pack_path), "--dry-run", "--json"]) == 0
    assert main(["rdbpack", "import", str(pack_path), "--json"]) == 0
    assert main(["rdbpack", "list", "--json"]) == 0
    loaded = load_pack(pack_path)
    assert main(["rdbpack", "remove", loaded.manifest.identity, "--json"]) == 0
