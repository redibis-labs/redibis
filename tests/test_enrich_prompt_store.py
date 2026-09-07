"""Enrichment prompt store + Settings ↔ pack bidirectional sync."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from redibis.enrich.prompt_store import (
    ENRICH_PROMPT_PARTS,
    EnrichPromptStore,
    builtin_enrichment_system_prompt,
    resolve_enrichment_system_prompt,
)
from redibis.enrich.service import EnrichmentService
from redibis.pack import export_default_pack, load_pack
from redibis.pack.sections import apply_prompt_assets


def test_builtin_compose_matches_service_seed():
    text = builtin_enrichment_system_prompt()
    assert "senior data steward" in text
    assert "DELTA ONLY" in text or "Output format" in text
    assert '"table"' in text or "table.description" in text
    assert "Table definition (required)" in text
    assert len(ENRICH_PROMPT_PARTS) >= 4


def test_prompt_store_seed_edit_compose(tmp_path: Path, monkeypatch):
    root = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    store = EnrichPromptStore(root=root)
    store.ensure_seeded()
    assert "01_role.md" in store.list_files()
    composed = store.compose(mode="normal")
    assert "senior data steward" in composed
    store.write("normal/01_role.md", "## CUSTOM ROLE\n\nAlways say CUSTOM.\n")
    again = store.compose(mode="normal")
    assert "CUSTOM ROLE" in again
    assert resolve_enrichment_system_prompt(root=root, mode="normal") == again
    assert "CUSTOM ROLE" in EnrichmentService.default_system_prompt()


def test_normal_compose_requires_table_description(tmp_path: Path, monkeypatch):
    root = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    store = EnrichPromptStore(root=root)
    store.reset_to_builtin()
    composed = store.compose(mode="normal")
    assert '"table"' in composed
    assert "table.description" in composed
    assert "Table definition (required)" in composed
    assert "a table-level definition" in composed


def test_export_includes_live_prompt_edits(tmp_path: Path, monkeypatch):
    root = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    store = EnrichPromptStore(root=root)
    store.ensure_seeded()
    store.write("normal/01_role.md", "PACK-EDITED ROLE PROMPT\n")
    store.write("01_role.md", "PACK-EDITED ROLE PROMPT\n")
    out = tmp_path / "out.rdbpack"
    export_default_pack(out)
    loaded = load_pack(out)
    assert "assets/prompts/enrich/01_role.md" in loaded.files
    assert "PACK-EDITED ROLE PROMPT" in loaded.text("assets/prompts/enrich/01_role.md")
    assert "PACK-EDITED ROLE PROMPT" in loaded.text("assets/prompts/enrichment_system.md")


def test_import_pack_updates_prompt_store(tmp_path: Path, monkeypatch):
    root = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    EnrichPromptStore(root=root).ensure_seeded()

    pack_prompts = tmp_path / "prompts2"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(pack_prompts))
    EnrichPromptStore(root=pack_prompts).ensure_seeded()
    EnrichPromptStore(root=pack_prompts).write(
        "01_role.md", "FROM-PACK ROLE\n\nImported role text.\n"
    )
    EnrichPromptStore(root=pack_prompts).write(
        "normal/01_role.md", "FROM-PACK ROLE\n\nImported role text.\n"
    )
    out = tmp_path / "from.rdbpack"
    export_default_pack(out)

    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    loaded = load_pack(out)
    report = apply_prompt_assets(loaded, replace=True)
    assert report["count"] >= 1
    assert "FROM-PACK ROLE" in EnrichPromptStore(root=root).read("01_role.md")
    assert "FROM-PACK ROLE" in EnrichmentService.default_system_prompt()


def test_prompt_api_roundtrip(tmp_path: Path, monkeypatch):
    from redibis.webapp import prompt_routes
    from redibis.webapp.backend import app

    root = tmp_path / "api-prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))

    def _factory():
        return EnrichPromptStore(root=root)

    prompt_routes.register_prompt_routes(app, store_factory=_factory)
    client = TestClient(app)

    r = client.get("/api/prompts/enrich")
    assert r.status_code == 200
    body = r.json()
    assert body["files"]
    name = body["files"][0]["name"]

    r = client.put(
        f"/api/prompts/enrich/{name}",
        json={"content": f"# Edited {name}\n\nHello from API.\n"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "saved"

    r = client.get(f"/api/prompts/enrich/{name}")
    assert "Hello from API" in r.json()["content"]

    r = client.post("/api/prompts/enrich/reset")
    assert r.status_code == 200
    assert r.json()["status"] == "reset"


def test_settings_markers_prompts_tab():
    root = Path(__file__).resolve().parents[1]
    js = (root / "redibis" / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'stab("prompts","Prompts")' in js
    assert "function loadPromptsPanel" in js
    assert "/api/prompts/enrich" in js


def test_nested_prompt_paths_and_stage_isolation(tmp_path: Path, monkeypatch):
    root = tmp_path / "prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    store = EnrichPromptStore(root=root)
    store.reset_to_builtin()
    assert "normal/01_role.md" in store.list_files()
    assert "multistep/steps/contract_review/08_contract_review.md" in store.list_files()
    pii = store.compose(mode="multistep", stage_kind="classification_pii")
    assert "PII re-review" in pii
    assert "Table definition (required)" not in pii
    assert "Always include `table.description`" not in pii
    table = store.compose(mode="multistep", stage_kind="table_definition")
    assert "Table definition (required)" in table
    assert "table.description" in table
    review = store.compose(mode="multistep", stage_kind="contract_review")
    assert "Whole-contract review" in review
    store.write("multistep/steps/classification_pii/99_custom.md", "STAGE-ONLY CUSTOM\n")
    assert "STAGE-ONLY CUSTOM" in store.compose(
        mode="multistep", stage_kind="classification_pii",
    )
    assert "STAGE-ONLY CUSTOM" not in store.compose(
        mode="multistep", stage_kind="table_definition",
    )


def test_non_destructive_seed_preserves_flat_only_store(tmp_path: Path):
    root = tmp_path / "prompts"
    enrich = root / "enrich"
    enrich.mkdir(parents=True)
    (enrich / "01_role.md").write_text("OPERATOR FLAT ROLE\n", encoding="utf-8")
    store = EnrichPromptStore(root=root)
    store.ensure_seeded()
    assert "OPERATOR FLAT ROLE" in store.read("01_role.md")
    assert not any(n.startswith("normal/") for n in store.list_files())
    assert "OPERATOR FLAT ROLE" in store.compose(mode="normal")


def test_nested_prompt_api_roundtrip(tmp_path: Path, monkeypatch):
    from redibis.webapp import prompt_routes
    from redibis.webapp.backend import app

    root = tmp_path / "api-prompts"
    monkeypatch.setenv("REDIBIS_PROMPTS_DIR", str(root))
    EnrichPromptStore(root=root).reset_to_builtin()

    def _factory():
        return EnrichPromptStore(root=root)

    prompt_routes.register_prompt_routes(app, store_factory=_factory)
    client = TestClient(app)
    name = "multistep/steps/contract_review/08_contract_review.md"
    r = client.put(
        f"/api/prompts/enrich/{name}",
        json={"content": "# Nested edit\n\nHello nested.\n"},
    )
    assert r.status_code == 200, r.text
    r = client.get(f"/api/prompts/enrich/{name}")
    assert r.status_code == 200
    assert "Hello nested" in r.json()["content"]

