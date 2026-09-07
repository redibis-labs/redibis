"""Evidence store, decide, context, and export (PLAN §8 tests 13–20, 30–31)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from redibis.cli.main import main
from redibis.scan.context_build import ContextBuildError, build_context, expand_add_paths
from redibis.scan.evidence_ops import (
    detected_columns,
    export_with_packs,
    load_evidence_bundle,
    replay_preset,
    store_evidence,
    strip_raw_values,
)
from redibis.store.pack_store import PackStore
from redibis.store.run_output_writer import RunOutputWriter, evidence_storage_key
from redibis.store.storage_backend import LocalBackend

from redibis.pack import write_pack

SOURCE_VALUES = [
    "01012345678",
    "01123456789",
    "Ahmed Hassan ElSayed",
    "CUST-0001",
    "28001012110017",
    "ahmed.hassan.demo@example.com",
    "ZZZ_RAW_SENTINEL_VALUE_9f3c",
]

STACK_UUID = "b7c1f0a2-9e44-4d61-8f30-2a5d6c7e1b93"


def _col(
    *,
    entity: str | None,
    detected: bool,
    presidio: dict | None = None,
    gliner: dict | None = None,
    phone: dict | None = None,
    validators: dict | None = None,
    samples: list | None = None,
    top_values: list | None = None,
    equation_id: str = "eq.balanced",
    confidence: float = 0.0,
) -> dict:
    pii_evidence = {
        "presidio": presidio or {"ran": False},
        "gliner": gliner or {"ran": False},
        "phone": phone or {"ran": False},
        "regex_catalog": {"ran": False},
        "llm_refiner": {"ran": False},
        "learned": {"ran": False},
    }
    block = {
        "pii_evidence": pii_evidence,
        "rules_fired": [],
        "negative_signals_fired": [],
        "pii_verdict": {
            "derived": True,
            "equation_id": equation_id,
            "decided_at": "2026-08-08T10:22:28.900Z",
            "detected": detected,
            "entity_type": entity,
            "confidence": confidence,
            "deciding_engines": [],
        },
        "samples": {
            "mode": "raw",
            "n": 10,
            "seed": 1337,
            "values": list(samples or []),
        },
        "profile": {
            "counts": {"sampled_rows": 25, "non_null": 25},
            "frequency": {
                "top_values": list(top_values or []),
                "top_n": 20,
                "entropy_bits": 1.0,
                "normalized_entropy": 0.5,
            },
        },
    }
    if validators:
        block["validator_results"] = validators
    return block


def _synthetic_bundle(*, run_id: str = "run_store_1") -> dict:
    return {
        "schema_version": "2.0",
        "kind": "redibis.evidence_bundle",
        "sensitivity": {
            "contains_raw_pii": True,
            "sample_mode": "raw",
            "egress": "deny",
            "note": "raw",
        },
        "header": {
            "provenance": {
                "redibis_version": "0.6.2",
                "redibis_git_sha": "testsha",
                "pack_stack": {
                    "stack_uuid": STACK_UUID,
                    "stack_sha256": "aa" * 32,
                    "mode": "overlay",
                    "packs": [],
                },
            },
            "timestamps": {
                "bundle_created_at": "2026-08-08T10:22:31.441Z",
                "phases": {
                    "sampling": {"duration_ms": 10},
                    "profiling": {"duration_ms": 20},
                    "quality": {"duration_ms": 30},
                    "pii": {"duration_ms": 40},
                },
                "timezone": "UTC",
            },
            "engines": [],
            "rules": {
                "equations": [
                    {
                        "id": "eq.balanced",
                        "mode": "balanced",
                        "default": True,
                        "expression": "2-of-N",
                        "thresholds": {
                            "presidio_min": 0.80,
                            "gliner_min": 0.70,
                            "phone_min": 0.80,
                        },
                    }
                ],
                "rulesets": [],
                "custom_rules": [],
                "validators": [],
                "negative_signals": [],
            },
            "sampling": {"strategy": "random", "seed": 1337, "sampled_rows": 25},
        },
        "table": {
            "name": "telecom.customers",
            "run_id": run_id,
            "status": "completed",
            "scan_types": ["pii"],
            "column_count": 4,
        },
        "columns": {
            "msisdn": _col(
                entity="PHONE_NUMBER",
                detected=True,
                confidence=0.94,
                presidio={
                    "ran": True,
                    "score": 0.95,
                    "entity": "PHONE_NUMBER",
                    "match_rate": 0.98,
                    "pattern_hits": [
                        {"pattern": "msisdn_egypt_national", "entity_type": "PHONE_NUMBER", "score": 0.90},
                    ],
                },
                phone={
                    "ran": True,
                    "score": 0.94,
                    "entity": "PHONE_NUMBER",
                    "valid_rate": 0.98,
                    "mobile_rate": 0.97,
                    "msisdn_valid_rate": 0.98,
                    "regions": {"EG": 25},
                },
                samples=["01012345678", "01123456789"],
                top_values=[{"value": "01012345678", "count": 20, "rate": 0.8}],
            ),
            "notes": _col(
                entity="PERSON",
                detected=False,
                confidence=0.52,
                gliner={"ran": True, "score": 0.52, "label": "PERSON", "match_rate": 0.4},
                samples=["Ahmed Hassan ElSayed", "hello world"],
                top_values=[{"value": "Ahmed Hassan ElSayed", "count": 3, "rate": 0.12}],
            ),
            "legacy_ref": _col(
                entity="EG_NATIONAL_ID",
                detected=False,
                confidence=0.61,
                presidio={
                    "ran": True,
                    "score": 0.61,
                    "entity": "EG_NATIONAL_ID",
                    "match_rate": 0.61,
                    "pattern_hits": [
                        {"pattern": "egypt_national_id", "entity_type": "EG_NATIONAL_ID", "score": 0.61},
                    ],
                },
                validators={"v.eg_nid": {"rate": 0.90, "checked": 25}},
                samples=["28001012110017", "ZZZ_RAW_SENTINEL_VALUE_9f3c"],
                top_values=[{"value": "28001012110017", "count": 1, "rate": 0.04}],
            ),
            "account_status": _col(
                entity=None,
                detected=False,
                samples=["active", "CUST-0001"],
                top_values=[{"value": "active", "count": 20, "rate": 0.8}],
            ),
        },
        "table_summary": {
            "pii": {
                "columns_scanned": 4,
                "columns_detected": 1,
                "by_entity": {"PHONE_NUMBER": 1},
            },
            "quality": {"expectations": 0, "passed": 0, "failed": 0},
        },
    }


def _write_bundle(tmp_path: Path, bundle: dict) -> Path:
    path = tmp_path / "runs" / bundle["table"]["run_id"] / "evidence_bundle.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return path


# ── 13 / 14 / 15 store ───────────────────────────────────────────────────────


def test_13_store_strips_samples_and_top_values(tmp_path: Path):
    bundle = _synthetic_bundle()
    stripped = strip_raw_values(bundle)
    for block in stripped["columns"].values():
        samples = block.get("samples") or {}
        assert "values" not in samples
        assert samples.get("mode") == "none"
        assert "n" in samples and "seed" in samples
        tops = ((block.get("profile") or {}).get("frequency") or {}).get("top_values") or []
        for item in tops:
            assert "value" not in item
            assert "count" in item
            assert "rate" in item


def test_14_stored_json_contains_no_source_string(tmp_path: Path):
    bundle = _synthetic_bundle()
    backend = LocalBackend(tmp_path / "storage")
    writer = RunOutputWriter(
        backend=backend, bucket="pii-reports", workflow="evidence",
        table="telecom.customers", run_id="run_store_1",
    )
    stripped, key = store_evidence(bundle, writer)
    stored = backend.get_bytes("pii-reports", key).decode("utf-8")
    for value in SOURCE_VALUES:
        assert value not in stored, f"source literal leaked: {value}"
    dumped = json.dumps(stripped)
    for value in SOURCE_VALUES:
        assert value not in dumped


def test_15_store_flips_sensitivity(tmp_path: Path):
    stripped = strip_raw_values(_synthetic_bundle())
    sens = stripped["sensitivity"]
    assert sens["contains_raw_pii"] is False
    assert sens["egress"] == "allow"
    assert sens["sample_mode"] == "none"
    assert sens.get("stripped_at")
    assert sens.get("stripped_by") == "redibis scan evidence store"


def test_16_keep_masked_samples_no_source_literals():
    bundle = _synthetic_bundle()
    masked = strip_raw_values(bundle, keep_masked_samples=True)
    assert masked["sensitivity"]["sample_mode"] == "masked"
    assert masked["sensitivity"]["contains_raw_pii"] is False
    dumped = json.dumps(masked)
    for value in SOURCE_VALUES:
        assert value not in dumped, f"masked bundle still contains {value}"
    msisdn_vals = masked["columns"]["msisdn"]["samples"]["values"]
    assert len(msisdn_vals) == 2
    notes_vals = masked["columns"]["notes"]["samples"]["values"]
    assert len(notes_vals) == 2
    for val in msisdn_vals + notes_vals:
        assert val not in SOURCE_VALUES


# ── 17 / 18 / 19 / 31 decide ─────────────────────────────────────────────────


def test_17_investigation_is_superset_of_balanced():
    bundle = _synthetic_bundle()
    reporting = replay_preset(bundle, preset="reporting")
    investigation = replay_preset(bundle, preset="investigation")
    bal = detected_columns(reporting)
    inv = detected_columns(investigation)
    assert bal <= inv
    assert "msisdn" in bal
    assert "notes" in inv - bal
    assert "legacy_ref" in inv - bal


def test_18_decide_performs_zero_source_reads(tmp_path: Path, monkeypatch):
    bundle = _synthetic_bundle()
    _write_bundle(tmp_path, bundle)

    def _boom(*_a, **_k):
        raise AssertionError("source table read during scan decide")

    monkeypatch.setattr(pd, "read_csv", _boom)
    monkeypatch.setattr(pd, "read_parquet", _boom)
    monkeypatch.setattr(pd, "read_table", _boom)

    from redibis.scan.base import Scan

    monkeypatch.setattr(Scan, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Scan.run called during decide")
    ))

    out = tmp_path / "replayed.json"
    rc = main([
        "scan", "decide",
        "--table", "telecom.customers",
        "--latest",
        "--preset", "investigation",
        "--out", str(out),
        "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    assert out.is_file()
    replayed = json.loads(out.read_text(encoding="utf-8"))
    assert "notes" in detected_columns(replayed)


def test_19_decide_appends_equation_and_leaves_evidence_byte_identical():
    bundle = _synthetic_bundle()
    before = json.dumps(
        {c: b["pii_evidence"] for c, b in bundle["columns"].items()},
        sort_keys=True,
    )
    replayed = replay_preset(bundle, preset="investigation")
    after = json.dumps(
        {c: b["pii_evidence"] for c, b in replayed["columns"].items()},
        sort_keys=True,
    )
    assert before == after
    eq_ids = [e["id"] for e in replayed["header"]["rules"]["equations"]]
    assert "eq.balanced" in eq_ids
    assert "eq.investigation" in eq_ids
    assert replayed["columns"]["notes"]["pii_verdict"]["equation_id"] == "eq.investigation"
    assert replayed["header"]["timestamps"]["phases"] == bundle["header"]["timestamps"]["phases"]
    assert replayed["header"]["timestamps"]["decided_at"]


def test_31_decide_copies_stack_uuid():
    bundle = _synthetic_bundle()
    replayed = replay_preset(bundle, preset="audit")
    assert replayed["header"]["provenance"]["pack_stack"]["stack_uuid"] == STACK_UUID
    assert (
        replayed["header"]["provenance"]["pack_stack"]["stack_uuid"]
        == bundle["header"]["provenance"]["pack_stack"]["stack_uuid"]
    )


# ── 20 + extra context ───────────────────────────────────────────────────────


def test_20_context_build_refuses_raw_pii(tmp_path: Path):
    bundle = _synthetic_bundle()
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ContextBuildError, match="contains raw PII"):
        build_context([path], allow_raw_pii=False)

    rc = main([
        "context", "build",
        "--add", str(path),
        "--out", str(tmp_path / "ctx.json"),
    ])
    assert rc == 2

    rc_ok = main([
        "context", "build",
        "--add", str(path),
        "--allow-raw-pii",
        "--reason", "operator review of investigation findings",
        "--out", str(tmp_path / "ctx.json"),
    ])
    assert rc_ok == 0
    envelope = json.loads((tmp_path / "ctx.json").read_text(encoding="utf-8"))
    assert envelope["kind"] == "redibis.llm_context"
    assert envelope["allow_raw_pii"] is True
    assert "operator review" in (envelope["raw_pii_reason"] or "")
    assert envelope["sources"][0]["table"] == "telecom.customers"
    assert envelope["items"][0]["content"]["kind"] == "redibis.evidence_bundle"


def test_context_build_max_bytes_drops_whole_file(tmp_path: Path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("aaaa" * 50, encoding="utf-8")
    b.write_text("bbbb" * 50, encoding="utf-8")
    envelope = build_context([a, b], max_bytes=a.stat().st_size + 10)
    assert len(envelope["items"]) == 1
    assert len(envelope["dropped"]) == 1
    assert envelope["dropped"][0]["reason"] == "max_bytes"
    assert envelope["dropped"][0]["path"] == str(b.resolve())


def test_context_build_dedupes_globs(tmp_path: Path):
    f = tmp_path / "note.md"
    f.write_text("# hello", encoding="utf-8")
    paths = expand_add_paths([str(tmp_path / "*.md"), str(f)])
    assert len(paths) == 1


# ── T1 emit / store CLI ──────────────────────────────────────────────────────


def test_scan_evidence_emit_and_list_runs(tmp_path: Path, capsys):
    bundle = _synthetic_bundle(run_id="run_a")
    _write_bundle(tmp_path, bundle)
    rc = main([
        "scan", "evidence",
        "--table", "telecom.customers",
        "--list-runs",
        "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    assert "run_a" in capsys.readouterr().out

    out = tmp_path / "emitted.json"
    rc = main([
        "scan", "evidence",
        "--table", "telecom.customers",
        "--latest",
        "--out", str(out),
        "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["table"]["name"] == "telecom.customers"


def test_scan_evidence_store_cli(tmp_path: Path, capsys):
    bundle = _synthetic_bundle(run_id="run_store_cli")
    _write_bundle(tmp_path, bundle)
    rc = main([
        "scan", "evidence", "store",
        "--table", "telecom.customers",
        "--latest",
        "--output-dir", str(tmp_path),
    ])
    assert rc == 0
    key = capsys.readouterr().out.strip().splitlines()[0]
    assert key == evidence_storage_key("telecom.customers", "run_store_cli")
    backend = LocalBackend(tmp_path / "_dev_storage")
    stored = backend.get_bytes("pii-reports", key).decode("utf-8")
    for value in SOURCE_VALUES:
        assert value not in stored


# ── 30 export --with-packs air-gap ───────────────────────────────────────────


def _minimal_pack_manifest(**kwargs) -> dict:
    metadata = {
        "id": kwargs.get("pack_id", "demo"),
        "version": kwargs.get("version", "1.0.0"),
        "description": kwargs.get("description", "test pack"),
        "author": kwargs.get("author", "tests"),
    }
    if kwargs.get("family_id"):
        metadata["family_id"] = kwargs["family_id"]
    if kwargs.get("uuid_value"):
        metadata["uuid"] = kwargs["uuid_value"]
    return {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": metadata,
        "requires": {"redibis": ">=0.5,<1"},
        "contents": {"locale": True},
        "mode": "overlay",
        "checksum": None,
        "signature": None,
    }


def test_30_export_with_packs_replays_offline(tmp_path: Path):
    archive = tmp_path / "demo.rdbpack"
    write_pack(
        archive,
        _minimal_pack_manifest(pack_id="telecom-policy", author="acme"),
        {"locale/tokens.yaml": {"PHONE_NUMBER": ["phone"]},
         "locale/regex.yaml": {"add": {}, "remove": [], "replace_all": False}},
    )
    work = tmp_path / "work"
    work.mkdir()
    backend = LocalBackend(work / "_dev_storage")
    store = PackStore(backend, bucket="pii-reports")
    ref = store.publish(archive, author="acme-governance")

    bundle = _synthetic_bundle(run_id="run_export_1")
    bundle["header"]["provenance"]["pack_stack"]["packs"] = [
        {
            "uuid": ref.uuid,
            "family_id": ref.family_id,
            "kind": ref.kind,
            "id": ref.id,
            "version": ref.version,
            "parent_uuid": None,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "published_at": ref.published_at,
            "author": ref.author,
            "signature": None,
            "contents": ref.contents,
            "available_locally": True,
        }
    ]
    _write_bundle(work, bundle)

    export_zip = tmp_path / "portable.zip"
    rc = main([
        "scan", "evidence", "export",
        "--table", "telecom.customers",
        "--latest",
        "--with-packs",
        "--out", str(export_zip),
        "--output-dir", str(work),
    ])
    assert rc == 0
    assert export_zip.is_file()
    with zipfile.ZipFile(export_zip) as zf:
        names = set(zf.namelist())
        assert "evidence_bundle.json" in names
        assert f"packs/{ref.uuid}.zip" in names
        assert "MANIFEST.sha256" in names

    # Delete local pack store (air-gap / clean host).
    packs_dir = Path(backend.root) / "pii-reports" / "_meta" / "packs"
    if packs_dir.exists():
        import shutil
        shutil.rmtree(packs_dir)
    assert store.head(ref.uuid) is None

    clean = tmp_path / "clean_host"
    clean.mkdir()
    with zipfile.ZipFile(export_zip) as zf:
        zf.extractall(clean)

    rc = main([
        "scan", "decide",
        "--table", "telecom.customers",
        "--latest",
        "--preset", "investigation",
        "--out", str(clean / "replayed.json"),
        "--output-dir", str(clean),
    ])
    assert rc == 0
    replayed = json.loads((clean / "replayed.json").read_text(encoding="utf-8"))
    assert detected_columns(replayed) >= {"msisdn", "notes", "legacy_ref"}
    assert replayed["header"]["provenance"]["pack_stack"]["stack_uuid"] == STACK_UUID


def test_scan_evidence_packs_lists_stack(tmp_path: Path, capsys):
    archive = tmp_path / "demo.rdbpack"
    write_pack(
        archive,
        _minimal_pack_manifest(pack_id="telecom-policy", author="acme"),
        {"locale/tokens.yaml": {"PHONE_NUMBER": ["phone"]},
         "locale/regex.yaml": {"add": {}, "remove": [], "replace_all": False}},
    )
    work = tmp_path / "work"
    work.mkdir()
    backend = LocalBackend(work / "_dev_storage")
    store = PackStore(backend, bucket="pii-reports")
    ref = store.publish(archive, author="acme-governance")

    bundle = _synthetic_bundle(run_id="run_packs_list")
    bundle["header"]["provenance"]["pack_stack"]["packs"] = [
        {
            "uuid": ref.uuid,
            "family_id": ref.family_id,
            "kind": ref.kind,
            "id": ref.id,
            "version": ref.version,
            "parent_uuid": None,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "published_at": ref.published_at,
            "author": ref.author,
            "signature": None,
            "contents": ref.contents,
            "available_locally": True,
        }
    ]
    _write_bundle(work, bundle)

    rc = main([
        "scan", "evidence", "packs",
        "--table", "telecom.customers",
        "--latest",
        "--output-dir", str(work),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stack" in out
    assert ref.uuid in out
    assert ref.id in out
