"""Phase 1 Redibis Pack format — round-trip (T2) and import safety (T7)."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from redibis.pack import (
    PackCompatibilityError,
    PackLoadError,
    PackManifest,
    PackValidationError,
    build_pack_files,
    load_pack,
    write_pack,
)
from redibis.pack.archive import MAX_FILE_BYTES, write_zip_bytes
from redibis.pack.canonical import CHECKSUMS_NAME, MANIFEST_NAME, sha256_bytes


def _manifest(
    *,
    pack_id: str = "demo",
    version: str = "1.0.0",
    redibis: str = ">=0.5,<1",
    contents: dict | None = None,
    registries: dict | None = None,
) -> dict:
    return {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {
            "id": pack_id,
            "version": version,
            "description": "test pack",
            "author": "tests",
        },
        "requires": {
            "redibis": redibis,
            "registries": registries or {},
        },
        "contents": contents or {},
        "mode": "overlay",
        "checksum": None,
        "signature": None,
    }


def test_t2_round_trip_identity(tmp_path: Path):
    sections = {
        "config/redibis.yaml": {
            "pii": {"equation_mode": "independent"},
            "scan_types": ["profile", "pii"],
        },
        "locale/tokens.yaml": {"PHONE_NUMBER": ["phone", "هاتف"]},
        "locale/regex.yaml": {"add": [], "remove": [], "replace_all": False},
    }
    manifest = _manifest(
        contents={"config": True, "locale": True},
        registries={
            "validators": ["validate_luhn"],
            "behavior_actions": ["core.verdict.set_entity"],
            "ner_backends": ["gliner"],
            "profilers": ["great_expectations"],
        },
    )
    out1 = tmp_path / "a.rdbpack"
    sha1 = write_pack(out1, manifest, sections)
    loaded = load_pack(out1)
    assert loaded.pack_sha256 == sha1
    assert loaded.manifest.identity == "demo@1.0.0"

    # Rebuild from the same logical payload — SHA and ZIP bytes must match.
    out2 = tmp_path / "b.rdbpack"
    sha2 = write_pack(out2, manifest, sections)
    assert sha1 == sha2
    assert out1.read_bytes() == out2.read_bytes()

    files_a, _ = build_pack_files(manifest, sections)
    files_b, _ = build_pack_files(PackManifest.model_validate(manifest), sections)
    assert files_a == files_b
    assert files_a[CHECKSUMS_NAME] == files_b[CHECKSUMS_NAME]


def test_t2_key_order_does_not_change_sha():
    sections_a = {
        "config/redibis.yaml": {"scan_types": ["pii"], "pii": {"equation_mode": "balanced"}},
    }
    sections_b = {
        "config/redibis.yaml": {"pii": {"equation_mode": "balanced"}, "scan_types": ["pii"]},
    }
    manifest = _manifest(contents={"config": True})
    _, sha_a = build_pack_files(manifest, sections_a)
    _, sha_b = build_pack_files(manifest, sections_b)
    assert sha_a == sha_b


def test_load_tar_gz(tmp_path: Path):
    manifest = _manifest()
    files, sha = build_pack_files(manifest, {})
    tar_path = tmp_path / "demo.rdbpack"
    with tarfile.open(tar_path, "w:gz") as tf:
        for rel, data in files.items():
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    loaded = load_pack(tar_path)
    assert loaded.source_kind == "tar.gz"
    assert loaded.pack_sha256 == sha


def test_t7_path_traversal_rejected(tmp_path: Path):
    zip_path = tmp_path / "evil.rdbpack"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.yaml", "x: 1\n")
        zf.writestr(MANIFEST_NAME, yaml.safe_dump(_manifest()))
    with pytest.raises(PackLoadError):
        load_pack(zip_path)


def test_t7_py_entry_rejected(tmp_path: Path):
    zip_path = tmp_path / "evil.rdbpack"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("hook.py", "print('no')\n")
        zf.writestr(MANIFEST_NAME, yaml.safe_dump(_manifest()))
    with pytest.raises(PackLoadError, match="unsupported pack file extension"):
        load_pack(zip_path)


def test_t7_checksum_mismatch(tmp_path: Path):
    out = tmp_path / "demo.rdbpack"
    write_pack(out, _manifest(), {})
    # Tamper README inside the zip while leaving CHECKSUMS stale.
    buf = io.BytesIO(out.read_bytes())
    with zipfile.ZipFile(buf, "r") as zin:
        members = {i.filename: zin.read(i) for i in zin.infolist() if not i.is_dir()}
    members["README.md"] = b"tampered\n"
    evil = tmp_path / "tampered.rdbpack"
    evil.write_bytes(write_zip_bytes(members))
    with pytest.raises(PackValidationError, match="checksum"):
        load_pack(evil)


def test_t7_version_range_violation():
    manifest = _manifest(redibis=">=99.0")
    with pytest.raises(PackCompatibilityError, match="requires redibis"):
        build_pack_files(manifest, {})


def test_t7_unresolved_validator():
    manifest = _manifest(
        registries={"validators": ["definitely_not_a_real_validator"]},
    )
    with pytest.raises(PackCompatibilityError, match="unresolved validator"):
        build_pack_files(manifest, {})


def test_t7_unresolved_behavior_action():
    manifest = _manifest(
        registries={"behavior_actions": ["plugin.not.registered"]},
    )
    with pytest.raises(PackCompatibilityError, match="unresolved behavior_action"):
        build_pack_files(manifest, {})


def test_t7_credential_key_in_config_rejected():
    manifest = _manifest(contents={"config": True})
    with pytest.raises(PackValidationError, match="excluded config key"):
        build_pack_files(
            manifest,
            {"config/redibis.yaml": {"storage": {"bucket": "secret"}, "pii": {}}},
        )


def test_t7_storage_key_rejected_on_load(tmp_path: Path):
    # Build a pack without config, then inject a bad config file + recompute badly.
    files, _ = build_pack_files(_manifest(), {})
    bad_config = yaml.safe_dump({"source": {"jdbc_url": "jdbc:evil"}}).encode()
    files["config/redibis.yaml"] = bad_config
    # Bypass writer allow-list by hand-writing a zip without valid CHECKSUMS — loader
    # should still reject on extension-valid content once checksums are fixed.
    from redibis.pack.canonical import (
        build_checksums_document,
        canonical_pack_digest,
        dump_canonical_json,
        dump_canonical_yaml,
        format_checksum_field,
        parse_yaml_bytes,
    )

    raw = parse_yaml_bytes(files[MANIFEST_NAME])
    raw["contents"] = {"config": True}
    raw["checksum"] = None
    files[MANIFEST_NAME] = dump_canonical_yaml(raw)
    pack_sha = canonical_pack_digest(files)
    raw["checksum"] = format_checksum_field(pack_sha)
    files[MANIFEST_NAME] = dump_canonical_yaml(raw)
    files[CHECKSUMS_NAME] = dump_canonical_json(
        build_checksums_document(files, pack_sha256=pack_sha)
    ) + b"\n"
    path = tmp_path / "bad-config.rdbpack"
    path.write_bytes(write_zip_bytes(files))
    with pytest.raises(PackValidationError, match="excluded config key"):
        load_pack(path)


def test_t7_oversized_document(monkeypatch, tmp_path: Path):
    import redibis.pack.archive as archive_mod
    import redibis.pack.writer as writer_mod

    monkeypatch.setattr(archive_mod, "MAX_FILE_BYTES", 64)
    monkeypatch.setattr(writer_mod, "MAX_FILE_BYTES", 64, raising=False)
    # writer imports write_zip_file which checks MAX_FILE_BYTES from archive
    big = {"locale/tokens.yaml": {"X": ["a" * 200]}}
    with pytest.raises(PackLoadError, match="size limit"):
        write_pack(tmp_path / "big.rdbpack", _manifest(contents={"locale": True}), big)


def test_t7_duplicate_policy_id():
    manifest = _manifest(
        contents={"behavior": ["alpha@1.0.0", "alpha@2.0.0"]},
    )
    sections = {
        "behavior/alpha@1.0.0.yaml": {"apiVersion": "redibis.behavior/v1", "id": "alpha"},
        "behavior/alpha@2.0.0.yaml": {"apiVersion": "redibis.behavior/v1", "id": "alpha"},
    }
    with pytest.raises(PackValidationError, match="duplicate policy id"):
        build_pack_files(manifest, sections)


def test_t7_zip_bomb_uncompressed_limit(monkeypatch, tmp_path: Path):
    import redibis.pack.archive as archive_mod

    monkeypatch.setattr(archive_mod, "MAX_UNCOMPRESSED_BYTES", 128)
    monkeypatch.setattr(archive_mod, "MAX_FILE_BYTES", 10_000)
    # Highly compressible payload that expands past the monkeypatched limit.
    payload = b"A" * 4096
    zip_path = tmp_path / "bomb.rdbpack"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, yaml.safe_dump(_manifest()))
        zf.writestr("README.md", payload)
        zf.writestr(
            CHECKSUMS_NAME,
            json.dumps(
                {
                    "version": 1,
                    "algorithm": "sha256",
                    "files": {
                        MANIFEST_NAME: sha256_bytes(yaml.safe_dump(_manifest()).encode()),
                        "README.md": sha256_bytes(payload),
                    },
                    "pack_sha256": "00",
                }
            ),
        )
    with pytest.raises(PackLoadError, match="uncompressed"):
        load_pack(zip_path)
