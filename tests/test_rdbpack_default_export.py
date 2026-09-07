"""Phase 2 — default pack export (T1) and golden drift detection (T4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from redibis import __version__
from redibis.config import RedibisConfig
from redibis.pack import load_pack
from redibis.pack.config_allowlist import extract_portable_config
from redibis.pack.defaults import (
    DEFAULT_PACK_ID,
    build_default_pack_files,
    default_compat_range,
    export_default_pack,
)

GOLDEN_PACK = (
    Path(__file__).resolve().parent / "data" / "packs" / "redibis-default.rdbpack"
)


def test_extract_portable_config_strips_secrets():
    cfg = RedibisConfig.default()
    cfg.pii.llm.api_key = "secret-key"
    cfg.storage.contracts_bucket = "should-not-travel"
    portable = extract_portable_config(cfg)
    assert "storage" not in portable
    assert "source" not in portable
    assert "llm" not in portable.get("pii", {})
    assert "scan_types" in portable
    assert "pii" in portable
    assert portable["pii"]["equation_mode"] == cfg.pii.equation_mode


def test_t1_export_default_produces_loadable_pack(tmp_path: Path, monkeypatch):
    from redibis.enrich.prompt_store import EnrichPromptStore

    prompts = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(prompts))
    EnrichPromptStore(root=prompts).reset_to_builtin()

    out = tmp_path / "redibis-default.rdbpack"
    sha = export_default_pack(out)
    loaded = load_pack(out)
    assert loaded.pack_sha256 == sha
    assert loaded.manifest.metadata.id == DEFAULT_PACK_ID
    assert loaded.manifest.metadata.version == __version__
    assert loaded.manifest.requires.redibis == default_compat_range(__version__)
    assert loaded.manifest.contents.config is True
    assert loaded.manifest.contents.locale is True
    assert loaded.manifest.contents.ner is True
    assert "telecom" in loaded.manifest.contents.classification
    assert "general" in loaded.manifest.contents.classification
    assert "config/redibis.yaml" in loaded.files
    assert "locale/regex.yaml" in loaded.files
    assert "locale/tokens.yaml" in loaded.files
    assert "locale/phone.yaml" in loaded.files
    assert "ner/models.yaml" in loaded.files
    assert "classification/packs/telecom.yaml" in loaded.files
    assert "assets/regex_patterns.json" in loaded.files
    assert "assets/collisions.yaml" in loaded.files
    assert "assets/coverage_gaps.yaml" in loaded.files
    assert "assets/prompts/enrichment_system.md" in loaded.files
    assert "assets/prompts/enrich/01_role.md" in loaded.files
    assert "assets/prompts/pii_refiner_system.md" in loaded.files
    cfg = loaded.yaml("config/redibis.yaml")
    assert isinstance(cfg, dict)
    assert "storage" not in cfg
    assert "llm" not in (cfg.get("pii") or {})
    regex = loaded.yaml("locale/regex.yaml")
    assert regex["replace_all"] is True
    assert len(regex["add"]) >= 50
    ner = loaded.yaml("ner/models.yaml")
    assert ner["labels"]
    assert ner["phrases"]


def test_t1_golden_fixture_exists_and_loads():
    assert GOLDEN_PACK.is_file(), (
        f"missing golden default pack: {GOLDEN_PACK} — run export_default_pack to refresh"
    )
    loaded = load_pack(GOLDEN_PACK)
    assert loaded.manifest.metadata.id == DEFAULT_PACK_ID
    assert loaded.manifest.contents.config is True
    assert loaded.manifest.contents.locale is True
    assert "locale/regex.yaml" in loaded.files
    assert "classification/packs/telecom.yaml" in loaded.files


def test_t4_default_pack_drift(tmp_path: Path, monkeypatch):
    """CI gate: regenerating the default pack must match the committed golden.

    Prompt export is Settings-backed; isolate to a freshly seeded prompt dir so
    local operator edits do not fail the drift gate.
    """
    from redibis.enrich.prompt_store import EnrichPromptStore
    from redibis.pack.archive import write_zip_bytes

    prompts = tmp_path / "golden-prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(prompts))
    EnrichPromptStore(root=prompts).reset_to_builtin()

    assert GOLDEN_PACK.is_file()
    files, sha = build_default_pack_files()
    golden = load_pack(GOLDEN_PACK)
    assert sha == golden.pack_sha256, (
        "redibis-default.rdbpack drifted from RedibisConfig.default() — "
        "update tests/data/packs/redibis-default.rdbpack deliberately if intended"
    )
    # Byte-identical ZIP (deterministic writer).
    assert GOLDEN_PACK.read_bytes() == write_zip_bytes(files)


def test_cli_rdbpack_export_default(tmp_path: Path):
    from redibis.cli.main import main

    out = tmp_path / "out.rdbpack"
    rc = main(["rdbpack", "export-default", "--out", str(out), "--json"])
    assert rc == 0
    assert out.is_file()
    assert load_pack(out).manifest.metadata.id == DEFAULT_PACK_ID


def test_cli_pack_export_default_alias(tmp_path: Path):
    from redibis.cli.main import main

    out = tmp_path / "alias.rdbpack"
    rc = main(["pack", "export-default", "--out", str(out)])
    assert rc == 0
    assert out.is_file()


def test_cli_rdbpack_validate(tmp_path: Path):
    from redibis.cli.main import main

    out = tmp_path / "v.rdbpack"
    export_default_pack(out)
    rc = main(["rdbpack", "validate", str(out), "--json"])
    assert rc == 0
