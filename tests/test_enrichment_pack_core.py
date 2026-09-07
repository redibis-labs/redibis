"""Tests for enrichment pack models, loader, validator, and context compiler."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import yaml

from redibis.enrich.packs.context_compiler import (
    compile_pack_context,
    require_approved_context,
    score_glossary_entry,
)
from redibis.enrich.packs.errors import ContextReductionRequired, PackLoadError, PackValidationError
from redibis.enrich.packs.eval_engine import evaluate_case, evaluate_pack_deltas
from redibis.enrich.packs.loader import load_enrichment_pack, pack_inspect_summary
from redibis.enrich.packs.validator import validate_loaded_pack

EXAMPLE_PACK = Path(__file__).resolve().parents[1] / "examples" / "enrichment_packs" / "telco"


@pytest.fixture
def telco_pack():
    return load_enrichment_pack(EXAMPLE_PACK)


def test_load_example_pack(telco_pack):
    assert telco_pack.manifest.metadata.name == "telco-enrichment-lite"
    assert telco_pack.sha256
    assert len(telco_pack.glossary) >= 10
    report = validate_loaded_pack(telco_pack)
    assert report.ok, report.errors
    assert report.signature_status == "unsigned"


def test_inspect_summary_hides_proprietary_content(telco_pack):
    summary = pack_inspect_summary(telco_pack)
    assert summary["counts"]["glossary_entries"] >= 10
    assert "glossary" not in summary
    assert "prompts" not in summary


def test_folder_zip_parity(telco_pack, tmp_path):
    zip_path = tmp_path / "telco.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in EXAMPLE_PACK.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(EXAMPLE_PACK).as_posix())
    zpack = load_enrichment_pack(zip_path)
    assert zpack.sha256 == telco_pack.sha256
    assert zpack.source_kind == "zip"


def test_zip_slip_rejected(tmp_path):
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.yaml", "x: 1\n")
        zf.writestr(
            "manifest.yaml",
            yaml.safe_dump(
                {
                    "apiVersion": "redibis.enrichment/v1",
                    "kind": "EnrichmentPack",
                    "metadata": {
                        "publisherId": "com.example",
                        "name": "evil",
                        "version": "1.0.0",
                    },
                    "compatibility": {
                        "redibis": ">=0.5,<0.7",
                        "outputContract": "redibis.enrichment-delta/v1",
                    },
                }
            ),
        )
    with pytest.raises(PackLoadError):
        load_enrichment_pack(zip_path)


def test_glossary_scoring():
    from redibis.enrich.packs.models import GlossaryEntry

    entry = GlossaryEntry(
        canonicalName="msisdn",
        aliases=["mobile_number"],
        definition="phone",
    )
    assert score_glossary_entry("msisdn", entry)[1] == "canonical"
    assert score_glossary_entry("mobile_number", entry)[1] == "alias"
    assert score_glossary_entry("subscriber_msisdn_legacy", entry)[0] > 0


def test_context_compile_no_reduction_when_within_limits(telco_pack):
    contract = telco_pack.golden_inputs["customer"]
    compiled = compile_pack_context(telco_pack, contract, provider_name="demo")
    # 12 glossary topK and 1 example — should fit without truncation ops for this lite pack.
    if compiled.reduction_plan is None:
        require_approved_context(compiled)
        assert "BEGIN_ENRICHMENT_PACK_CONTEXT" in compiled.user_addendum
        assert "msisdn" in compiled.selected_glossary or "msisdn" in compiled.user_addendum
    else:
        # If top-K or budget triggered, approval must gate LLM usage.
        with pytest.raises(ContextReductionRequired):
            require_approved_context(compiled)
        approved = compile_pack_context(
            telco_pack,
            contract,
            provider_name="demo",
            approve_reduction_plan_id=compiled.reduction_plan.reduction_plan_id,
        )
        require_approved_context(approved)
        assert approved.reduction_applied


def test_reduction_plan_id_stable(telco_pack):
    contract = telco_pack.golden_inputs["customer"]
    # Force a small topK to require reduction.
    telco_pack.manifest.selection.glossaryTopK = 2
    a = compile_pack_context(telco_pack, contract, provider_name="demo")
    b = compile_pack_context(telco_pack, contract, provider_name="demo")
    assert a.reduction_plan is not None
    assert a.reduction_plan.reduction_plan_id == b.reduction_plan.reduction_plan_id
    assert a.reduction_plan.reduction_plan_id.startswith("crp_v1_")


def test_wrong_approval_fails(telco_pack):
    contract = telco_pack.golden_inputs["customer"]
    telco_pack.manifest.selection.glossaryTopK = 2
    with pytest.raises(ContextReductionRequired):
        compile_pack_context(
            telco_pack,
            contract,
            provider_name="demo",
            approve_reduction_plan_id="crp_v1_" + ("0" * 64),
        )


def test_eval_engine_on_golden_delta(telco_pack):
    case = telco_pack.eval_cases[0]
    delta = telco_pack.golden_deltas["customer"]
    result = evaluate_case(case, delta)
    assert result.ok, result.to_dict()
    agg = evaluate_pack_deltas(telco_pack.eval_cases, {"customer-msisdn": delta})
    assert agg["ok"]


def test_undeclared_file_rejected(tmp_path):
    root = tmp_path / "pack"
    root.mkdir()
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "redibis.enrichment/v1",
                "kind": "EnrichmentPack",
                "metadata": {
                    "publisherId": "com.example",
                    "name": "x",
                    "version": "1.0.0",
                    "license": "MIT",
                },
                "compatibility": {
                    "redibis": ">=0.5,<0.7",
                    "outputContract": "redibis.enrichment-delta/v1",
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "secret.py").write_text("print('no')\n", encoding="utf-8")
    with pytest.raises((PackValidationError, PackLoadError)):
        load_enrichment_pack(root)


def _profile_pack(root: Path, stages: dict[str, list[str]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "normal").mkdir(exist_ok=True)
    (root / "normal" / "base.md").write_text("PACK BASE PROMPT\n", encoding="utf-8")
    (root / "multistep").mkdir(exist_ok=True)
    (root / "multistep" / "shared.md").write_text("PACK SHARED PROMPT\n", encoding="utf-8")
    (root / "multistep" / "stage.md").write_text("PACK STAGE PROMPT\n", encoding="utf-8")
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "redibis.enrichment/v1",
                "kind": "EnrichmentPack",
                "metadata": {
                    "publisherId": "com.example",
                    "name": "profile",
                    "version": "1.0.0",
                    "license": "MIT",
                },
                "compatibility": {
                    "redibis": ">=0.5,<0.8",
                    "outputContract": "redibis.enrichment-delta/v1",
                },
                "prompt": {
                    "normal": ["normal/base.md"],
                    "shared": ["multistep/shared.md"],
                    "stages": stages,
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_unknown_prompt_stage_key_rejected(tmp_path):
    root = _profile_pack(tmp_path / "bad", {"not_a_stage": ["multistep/stage.md"]})
    with pytest.raises((PackValidationError, PackLoadError)):
        load_enrichment_pack(root)


def test_pack_profile_prompt_is_not_duplicated_in_addendum(tmp_path):
    from redibis.enrich.packs.context_compiler import pack_profile_base, pack_stage_prompt

    pack = load_enrichment_pack(
        _profile_pack(tmp_path / "ok", {"classification_pii": ["multistep/stage.md"]})
    )
    assert pack_profile_base(pack, mode="normal").strip() == "PACK BASE PROMPT"
    assert pack_profile_base(pack, mode="multistep").strip() == "PACK SHARED PROMPT"
    assert pack_stage_prompt(pack, stage_kind="classification_pii").strip() == (
        "PACK STAGE PROMPT"
    )
    assert pack_stage_prompt(pack, stage_kind="table_definition") == ""

    compiled = compile_pack_context(
        pack, {"schema": []}, provider_name="demo", mode="multistep",
        stage_kind="classification_pii",
    )
    # The profile drives the base system prompt, so it must not also be appended.
    assert "PACK SHARED PROMPT" not in compiled.system_addendum
    assert "PACK STAGE PROMPT" not in compiled.system_addendum
    assert "PACK SHARED PROMPT" in compiled.profile_prompt
    assert compiled.section_characters.get("profile_prompts")
