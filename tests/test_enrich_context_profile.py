"""Context profile export, custom overlays, and enrich CLI dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.cli.main import _rewrite_enrich_argv, main
from redibis.enrich.context_profile import (
    CustomContextStore,
    export_context_profile,
    overlay_relpath,
)
from redibis.enrich.packs.loader import load_enrichment_pack
from redibis.enrich.prompt_store import EnrichPromptStore
from redibis.enrich.workflow import LLM_STAGE_KINDS


def test_rewrite_enrich_argv_keeps_table_positional():
    assert _rewrite_enrich_argv(["enrich", "telecom.customers", "--provider", "demo"]) == [
        "enrich", "telecom.customers", "--provider", "demo",
    ]
    assert _rewrite_enrich_argv(["enrich", "run", "telecom.customers"]) == [
        "enrich", "--enrich-action", "run", "telecom.customers",
    ]
    assert _rewrite_enrich_argv(["enrich", "export-context", "./out"]) == [
        "enrich", "--enrich-action", "export-context", "./out",
    ]


def test_rewrite_enrich_argv_only_matches_the_subcommand_slot():
    # "enrich" as a value or as another command's argument must not be rewritten.
    assert _rewrite_enrich_argv(["catalog", "push", "enrich", "run"]) == [
        "catalog", "push", "enrich", "run",
    ]
    assert _rewrite_enrich_argv(["--debug", "enrich", "list-context"]) == [
        "--debug", "enrich", "--enrich-action", "list-context",
    ]
    assert _rewrite_enrich_argv(["--log-format", "json", "enrich", "run", "t1"]) == [
        "--log-format", "json", "enrich", "--enrich-action", "run", "t1",
    ]
    assert _rewrite_enrich_argv(["enrich", "--provider", "run", "t1"]) == [
        "enrich", "--provider", "run", "t1",
    ]


def test_overlay_relpath_rejects_traversal_and_unknown_stage():
    with pytest.raises(ValueError):
        overlay_relpath("shared", "../secret.md")
    with pytest.raises(ValueError):
        overlay_relpath("multistep", "ok.md", stage="not_a_stage")
    with pytest.raises(ValueError):
        overlay_relpath("shared", "ok.md", stage="column_definitions")
    assert overlay_relpath("multistep", "note.md", stage="contract_review") == (
        "multistep/steps/contract_review/note.md"
    )


def test_export_context_structure_and_roundtrip(tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(prompts))
    EnrichPromptStore(root=prompts).reset_to_builtin()

    dest = tmp_path / "exported"
    manifest = export_context_profile(dest)
    assert (dest / "context-manifest.yaml").is_file()
    assert (dest / "manifest.yaml").is_file()
    assert (dest / "normal").is_dir()
    assert (dest / "multistep" / "shared").is_dir()
    for kind in LLM_STAGE_KINDS:
        assert (dest / "multistep" / "steps" / kind).is_dir()
    assert manifest["include_custom"] is False
    checksums = {f["path"]: f["sha256"] for f in manifest["files"]}
    again = export_context_profile(tmp_path / "exported2")
    checksums2 = {f["path"]: f["sha256"] for f in again["files"]}
    assert checksums == checksums2

    pack = load_enrichment_pack(dest)
    assert pack.manifest.kind == "EnrichmentPack"
    assert pack.manifest.prompt.normal
    assert pack.manifest.prompt.shared
    assert "contract_review" in (pack.manifest.prompt.stages or {})


def test_export_include_custom_default_off(tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(prompts))
    EnrichPromptStore(root=prompts).reset_to_builtin()
    custom = tmp_path / "custom"
    (custom / "shared").mkdir(parents=True)
    (custom / "shared" / "secret.md").write_text("SENSITIVE OVERLAY\n", encoding="utf-8")

    dest = tmp_path / "out"
    export_context_profile(dest, custom_dir=custom, include_custom=False)
    assert not (dest / "shared" / "secret.md").exists()
    text = "\n".join(p.read_text(encoding="utf-8") for p in dest.rglob("*.md"))
    assert "SENSITIVE OVERLAY" not in text

    dest2 = tmp_path / "out2"
    export_context_profile(dest2, custom_dir=custom, include_custom=True)
    assert "SENSITIVE OVERLAY" in (dest2 / "shared" / "secret.md").read_text(encoding="utf-8")

    # Every written file must be declared, or --pack DIR rejects the export.
    pack = load_enrichment_pack(dest2)
    assert "shared/secret.md" in pack.manifest.prompt.normal
    assert "shared/secret.md" in pack.manifest.prompt.shared


def test_custom_store_add_list_remove(tmp_path):
    store = CustomContextStore(tmp_path / "custom")
    src = tmp_path / "note.md"
    src.write_text("hello overlay\n", encoding="utf-8")
    written = store.add([src], mode="multistep", stage="column_definitions")
    assert written[0]["path"].endswith("note.md")
    listed = store.list_files()
    assert listed[0]["path"] == "multistep/steps/column_definitions/note.md"
    rels = store.read_for(mode="multistep", stage_kind="column_definitions")
    assert rels and "hello overlay" in rels[0][1]
    assert store.read_for(mode="multistep", stage_kind="table_definition") == []
    assert store.remove(listed[0]["path"]) is True
    assert store.list_files() == []


def test_cli_export_and_add_context(tmp_path, monkeypatch, capsys):
    prompts = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(prompts))
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path / "configs"))
    EnrichPromptStore(root=prompts).reset_to_builtin()

    dest = tmp_path / "profile"
    rc = main(["enrich", "export-context", str(dest), "--output-dir", str(tmp_path / "reports")])
    assert rc == 0
    assert (dest / "context-manifest.yaml").is_file()

    note = tmp_path / "company.md"
    note.write_text("Acme overlay\n", encoding="utf-8")
    rc = main([
        "enrich", "add-context", str(note),
        "--scope", "global", "--mode", "shared",
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 0
    rc = main([
        "enrich", "list-context", "--scope", "global", "--json",
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["files"]
    rel = payload["files"][0]["path"]
    rc = main([
        "enrich", "remove-context", rel, "--scope", "global",
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 0


def test_cli_add_context_accepts_multiple_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path / "configs"))
    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    rc = main([
        "enrich", "add-context", str(first), str(second),
        "--mode", "multistep", "--stage", "classification_pii",
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 0
    capsys.readouterr()
    paths = {
        item["path"]
        for item in CustomContextStore(
            tmp_path / "configs" / "enrich-context" / "custom"
        ).list_files()
    }
    assert paths == {
        "multistep/steps/classification_pii/a.md",
        "multistep/steps/classification_pii/b.md",
    }


def test_table_context_rejects_key_traversal(tmp_path):
    from redibis.enrich.service import enrichment_service_for_store
    from redibis.store.contract_store import ContractStore
    from redibis.store.storage_backend import LocalBackend

    store = ContractStore(backend=LocalBackend(root=tmp_path / "store"), bucket="contracts")
    svc = enrichment_service_for_store(store)
    with pytest.raises(ValueError):
        svc.delete_context_doc("telecom.customers", "../../active/telecom.customers.yaml")
    with pytest.raises(ValueError):
        svc.add_context_doc("telecom.customers", "..", b"x")


def test_cli_enrich_table_still_requires_contract(tmp_path, capsys):
    rc = main([
        "enrich", "telecom.customers", "--provider", "demo",
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active contract" in err or "active contract" in err
