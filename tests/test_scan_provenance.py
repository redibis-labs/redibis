"""P1–P4: ScanProvenance, run registry, publish ≠ activate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.config import RedibisConfig
from redibis.pack import apply_packs
from redibis.pack.models import PackMetadata
from redibis.pack.publish import PublishError, _published_rules_checksum, publish_text_gateway_pack
from redibis.pack.stack_models import AppliedPackStack
from redibis.pii.provenance import mint_scan_provenance
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.run_store import (
    HMAC_PREFIX,
    ProvenanceRunStore,
    input_digest,
    record_run,
    reset_run_store_cache,
)
from redibis.services.text_pii_service import TextPIIService
from redibis.store.pack_store import PackStore
from redibis.store.storage_backend import LocalBackend

from tests.test_text_gateway_pack_section import _write_tg_pack


def _isolate_run_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIBIS_PII_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("REDIBIS_PII_RUN_HMAC_KEY", "unit-test-hmac")
    reset_run_store_cache()


def _eval_run_for_rules(
    rules: dict,
    *,
    gate_passed: bool | None = True,
    run_uuid: str = "eval-1",
    kind: str = "eval",
    checksum: str | None = None,
) -> str:
    overlay_checksum = checksum
    if overlay_checksum is None:
        overlay_checksum = _published_rules_checksum({"operator": rules})
    outcome: dict = {"rules_checksum": overlay_checksum}
    if gate_passed is not None:
        outcome["gate_passed"] = gate_passed
        outcome["gate"] = {
            "passed": gate_passed,
            "summary": "pass" if gate_passed else "fail",
        }
    rec = record_run(kind=kind, outcome=outcome, run_uuid=run_uuid)
    return rec.run_uuid


def test_identical_config_yields_identical_provenance_uuid(tmp_path: Path):
    pack = _write_tg_pack(tmp_path / "tg.rdbpack")
    stack = apply_packs(RedibisConfig.default(), [pack], include_builtin_default=False)
    rs = RuleSetCompiler.from_stack(stack)
    a = mint_scan_provenance(ruleset=rs, stack=stack, ner_backend=None)
    b = mint_scan_provenance(ruleset=rs, stack=stack, ner_backend=None)
    assert a.provenance_uuid
    assert a.provenance_uuid == b.provenance_uuid
    assert a.provenance_degraded is False
    assert a.stack_uuid
    assert a.stack_sha256
    assert a.rules_checksum.startswith("sha256:")
    assert "builtin" in a.rules_source


def test_rule_change_changes_provenance_uuid(tmp_path: Path):
    a_pack = _write_tg_pack(tmp_path / "a.rdbpack", marker="ONE")
    b_pack = _write_tg_pack(tmp_path / "b.rdbpack", marker="TWO", version="1.1.0")
    sa = apply_packs(RedibisConfig.default(), [a_pack], include_builtin_default=False)
    sb = apply_packs(RedibisConfig.default(), [b_pack], include_builtin_default=False)
    pa = mint_scan_provenance(ruleset=RuleSetCompiler.from_stack(sa), stack=sa)
    pb = mint_scan_provenance(ruleset=RuleSetCompiler.from_stack(sb), stack=sb)
    assert pa.provenance_uuid != pb.provenance_uuid


def test_missing_layer_digest_degrades(tmp_path: Path):
    pack = _write_tg_pack(tmp_path / "tg.rdbpack")
    stack = apply_packs(RedibisConfig.default(), [pack], include_builtin_default=False)
    broken = SimpleNamespace(
        uuid="",
        sha256="",
        source="path",
        mode="overlay",
        to_dict=lambda: {"id": "broken", "version": "0"},
    )
    stack.layers.append(broken)  # type: ignore[arg-type]
    rec = mint_scan_provenance(
        ruleset=RuleSetCompiler.from_stack(
            apply_packs(RedibisConfig.default(), [pack], include_builtin_default=False)
        ),
        stack=stack,
    )
    assert rec.provenance_degraded is True
    assert rec.provenance_uuid
    assert "uuid" in rec.provenance_degraded_reason.lower() or "sha256" in rec.provenance_degraded_reason.lower()


def test_http_ner_degrades(tmp_path: Path, monkeypatch):
    _isolate_run_store(tmp_path, monkeypatch)
    backend = SimpleNamespace(name="http:https://ner.example/v1")
    rec = mint_scan_provenance(
        ruleset=RuleSetCompiler.default(),
        ner_backend=backend,
    )
    assert rec.provenance_degraded is True
    assert rec.ner_backend == "http"
    assert rec.provenance_uuid
    store = ProvenanceRunStore(tmp_path / "runs")
    store.put_provenance(rec)
    loaded = store.get_provenance(rec.provenance_uuid)
    assert loaded is not None
    assert loaded.provenance_degraded is True
    assert loaded.provenance_uuid == rec.provenance_uuid


def test_service_stamps_detection_result(tmp_path: Path, monkeypatch):
    _isolate_run_store(tmp_path, monkeypatch)
    svc = TextPIIService(
        redibis_config=RedibisConfig.default(),
        pack_stack=AppliedPackStack(config=RedibisConfig.default()),
        skip_stored_text_rules=True,
    )
    result = svc.scan("hello alice@example.com", engines="regex", min_score=0.2)
    assert result.provenance_uuid
    assert result.run_uuid
    dumped = result.to_dict()
    assert dumped["provenance_uuid"] == result.provenance_uuid
    again = svc.scan("hello alice@example.com", engines="regex", min_score=0.2)
    assert again.provenance_uuid == result.provenance_uuid
    assert again.run_uuid != result.run_uuid


def test_run_registry_stores_digest_not_text(tmp_path: Path, monkeypatch):
    _isolate_run_store(tmp_path, monkeypatch)
    svc = TextPIIService(
        redibis_config=RedibisConfig.default(),
        pack_stack=AppliedPackStack(config=RedibisConfig.default()),
        skip_stored_text_rules=True,
    )
    secret = "ssn 123-45-6789 should never be stored"
    result = svc.scan(secret, engines="regex", min_score=0.2, include_provenance=True)
    rec = record_run(
        kind="api_scan",
        provenance=result.provenance,
        text=secret,
        outcome={"entity_counts": dict(result.entity_counts)},
        run_uuid=result.run_uuid,
    )
    assert rec.input_digest == input_digest(secret)
    raw = (tmp_path / "runs" / "runs" / f"{rec.run_uuid}.json").read_text(encoding="utf-8")
    assert secret not in raw
    assert "123-45-6789" not in raw
    store = ProvenanceRunStore(tmp_path / "runs")
    loaded = store.get_run(rec.run_uuid)
    assert loaded is not None
    assert loaded.provenance_uuid == result.provenance_uuid
    by_prov = store.list_runs(provenance_uuid=result.provenance_uuid)
    assert any(r.run_uuid == rec.run_uuid for r in by_prov)
    assert result.provenance is not None
    assert "regex_inventory" in result.provenance


def test_publish_refuses_without_evaluation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STORE_DIR", str(tmp_path / "published"))
    _isolate_run_store(tmp_path, monkeypatch)
    with pytest.raises(PublishError, match="evaluation"):
        publish_text_gateway_pack(
            rules={"exclude_terms": ["agent"]},
            version="1.0.0",
            description="try without eval",
            author="tests",
        )


def test_publish_mints_uuid_and_is_not_activation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STORE_DIR", str(tmp_path / "published"))
    monkeypatch.setenv("REDIBIS_PACK_STACK_DIR", str(tmp_path / "stack"))
    _isolate_run_store(tmp_path, monkeypatch)
    store = PackStore(LocalBackend(tmp_path / "published"), bucket="packs")
    parent_rules = {"exclude_terms": ["agent"]}
    child_rules = {"exclude_terms": ["agent", "caller"]}
    parent_eval = _eval_run_for_rules(parent_rules, run_uuid="eval-1")
    child_eval = _eval_run_for_rules(child_rules, run_uuid="eval-2")
    parent = publish_text_gateway_pack(
        rules=parent_rules,
        version="1.0.0",
        description="first version",
        author="tests",
        eval_run_uuid=parent_eval,
        eval_gate_passed=True,
        eval_gate_summary="passed",
        store=store,
    )
    child = publish_text_gateway_pack(
        rules=child_rules,
        version="1.1.0",
        description="add caller exclusion",
        author="tests",
        parent_uuid=parent.uuid,
        family_id=parent.family_id,
        eval_run_uuid=child_eval,
        eval_gate_passed=True,
        store=store,
    )
    assert parent.uuid != child.uuid
    assert child.parent_uuid == parent.uuid
    assert child.family_id == parent.family_id
    from redibis.pack import PackStackStore

    stack = PackStackStore(root=tmp_path / "stack")
    assert stack.list_layers() == []
    blob = store.get(child.uuid)
    archive = tmp_path / "child.rdbpack"
    archive.write_bytes(blob)
    stack.import_pack(archive)
    assert len(stack.list_layers()) == 1


def test_failing_gate_requires_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STORE_DIR", str(tmp_path / "published"))
    _isolate_run_store(tmp_path, monkeypatch)
    rules = {"exclude_terms": ["x"]}
    _eval_run_for_rules(rules, gate_passed=False, run_uuid="eval-bad")
    with pytest.raises(PublishError, match="gate failed"):
        publish_text_gateway_pack(
            rules=rules,
            version="1.0.0",
            description="failed gate",
            author="tests",
            eval_run_uuid="eval-bad",
            eval_gate_passed=False,
        )
    ref = publish_text_gateway_pack(
        rules=rules,
        version="1.0.0",
        description="failed gate with override",
        author="tests",
        eval_run_uuid="eval-bad",
        eval_gate_passed=False,
        allow_unevaluated=True,
        unevaluated_reason="admin accepted known FN on LOCATION",
        store=PackStore(LocalBackend(tmp_path / "published"), bucket="packs"),
    )
    assert ref.uuid


def test_description_does_not_change_provenance_uuid():
    rs = RuleSetCompiler.default()
    a = mint_scan_provenance(ruleset=rs, description="alpha label")
    b = mint_scan_provenance(ruleset=rs, description="beta label")
    assert a.provenance_uuid
    assert a.provenance_uuid == b.provenance_uuid
    assert a.description != b.description


def test_publish_refuses_unknown_eval_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STORE_DIR", str(tmp_path / "published"))
    _isolate_run_store(tmp_path, monkeypatch)
    with pytest.raises(PublishError, match="not found"):
        publish_text_gateway_pack(
            rules={"exclude_terms": ["agent"]},
            version="1.0.0",
            description="fake eval uuid",
            author="tests",
            eval_run_uuid="eval-1",
            eval_gate_passed=True,
        )


def test_publish_refuses_when_eval_gate_unknown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STORE_DIR", str(tmp_path / "published"))
    _isolate_run_store(tmp_path, monkeypatch)
    rules = {"exclude_terms": ["agent"]}
    _eval_run_for_rules(rules, gate_passed=None, run_uuid="eval-none")
    with pytest.raises(PublishError, match="gate verdict"):
        publish_text_gateway_pack(
            rules=rules,
            version="1.0.0",
            description="optimistic none",
            author="tests",
            eval_run_uuid="eval-none",
            eval_gate_passed=None,
        )


def test_publish_refuses_checksum_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("REDIBIS_PACK_STORE_DIR", str(tmp_path / "published"))
    _isolate_run_store(tmp_path, monkeypatch)
    _eval_run_for_rules({"exclude_terms": ["a"]}, run_uuid="eval-x")
    with pytest.raises(PublishError, match="rules_checksum"):
        publish_text_gateway_pack(
            rules={"exclude_terms": ["b"]},
            version="1.0.0",
            description="wrong checksum",
            author="tests",
            eval_run_uuid="eval-x",
            eval_gate_passed=True,
        )


def test_input_digest_is_hmac_and_skips_short(tmp_path: Path, monkeypatch):
    _isolate_run_store(tmp_path, monkeypatch)
    long_text = "x" * 40
    digest = input_digest(long_text)
    assert digest.startswith(HMAC_PREFIX)
    raw_sha = hashlib.sha256(long_text.encode("utf-8")).hexdigest()
    assert raw_sha not in digest
    assert digest != "sha256:" + raw_sha
    assert input_digest("01012345678") == ""
    rec = record_run(kind="api_scan", text="01012345678")
    assert rec.input_digest == ""
    assert rec.char_count == 11
    rec2 = record_run(kind="api_scan", text=long_text)
    raw = (tmp_path / "runs" / "runs" / f"{rec2.run_uuid}.json").read_text(encoding="utf-8")
    assert "unit-test-hmac" not in raw
    assert long_text not in raw
    assert rec2.input_digest.startswith(HMAC_PREFIX)


def test_span_policies_follow_scan_config():
    cfg = SimpleNamespace(engines="regex", preprocess_obfuscation=False, use_llm=False)
    rec = mint_scan_provenance(ruleset=RuleSetCompiler.default(), scan_config=cfg)
    assert "ner" not in rec.span_policies
    assert "llm" not in rec.span_policies
    assert "regex" in rec.span_policies
    assert rec.expanders == ()
    assert rec.validators == ()
    empty = mint_scan_provenance(ruleset=RuleSetCompiler.default(), scan_config=None)
    assert "ner" not in empty.span_policies
    assert "regex" not in empty.span_policies
    assert "validate" in empty.span_policies
    assert "resolve" in empty.span_policies


def test_pack_metadata_ignores_unknown_fields():
    md = PackMetadata.model_validate(
        {"id": "x", "version": "1.0.0", "future_eval_blob": {"a": 1}}
    )
    assert md.id == "x"
    assert not hasattr(md, "future_eval_blob")


def test_loader_requires_redibis_before_unknown_metadata(tmp_path: Path):
    from redibis.pack.archive import write_zip_bytes
    from redibis.pack.canonical import (
        CHECKSUMS_NAME,
        MANIFEST_NAME,
        build_checksums_document,
        canonical_pack_digest,
        dump_canonical_json,
        dump_canonical_yaml,
        format_checksum_field,
    )
    from redibis.pack.errors import PackCompatibilityError
    from redibis.pack.loader import load_pack

    files: dict[str, bytes] = {}
    raw = {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {
            "id": "future-pack",
            "version": "9.0.0",
            "future_eval_blob": {"x": 1},
        },
        "requires": {"redibis": ">=99.0"},
        "contents": {},
        "mode": "overlay",
    }
    files[MANIFEST_NAME] = dump_canonical_yaml(raw)
    files["README.md"] = b"# future\n"
    pack_sha = canonical_pack_digest(files)
    raw["checksum"] = format_checksum_field(pack_sha)
    files[MANIFEST_NAME] = dump_canonical_yaml(raw)
    files[CHECKSUMS_NAME] = dump_canonical_json(
        build_checksums_document(files, pack_sha256=pack_sha)
    ) + b"\n"
    path = tmp_path / "future.rdbpack"
    path.write_bytes(write_zip_bytes(files))
    with pytest.raises(PackCompatibilityError, match="requires redibis"):
        load_pack(path)
