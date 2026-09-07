"""Pack UUID identity, stack_uuid, and PackStore (evidence bundle task set 02)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from redibis.pack import (
    apply_packs,
    backfill_uuid,
    compute_stack_uuid,
    load_pack,
    pack_stack_for_evidence,
    write_ar_eg_pack,
    write_fr_fr_pack,
    write_pack,
)
from redibis.pack.identity import compute_stack_sha256
from redibis.store.pack_store import (
    PackStore,
    PackStoreError,
    PackVersionCollisionError,
)
from redibis.store.storage_backend import LocalBackend

from redibis.config import RedibisConfig


def _minimal_manifest(
    *,
    pack_id: str = "demo",
    version: str = "1.0.0",
    author: str = "tests",
    family_id: str | None = None,
    uuid_value: str | None = None,
    parent_uuid: str | None = None,
    description: str = "test pack",
    signature: dict | None = None,
) -> dict:
    metadata = {
        "id": pack_id,
        "version": version,
        "description": description,
        "author": author,
    }
    if family_id:
        metadata["family_id"] = family_id
    if uuid_value:
        metadata["uuid"] = uuid_value
    if parent_uuid:
        metadata["parent_uuid"] = parent_uuid
    return {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": metadata,
        "requires": {"redibis": ">=0.5,<1"},
        "contents": {"locale": True},
        "mode": "overlay",
        "checksum": None,
        "signature": signature,
    }


def _write_demo_pack(path: Path, *, description: str = "test pack", **kwargs) -> Path:
    sections = {
        "locale/tokens.yaml": {"PHONE_NUMBER": ["phone", "mobile"]},
        "locale/regex.yaml": {"add": {}, "remove": [], "replace_all": False},
    }
    write_pack(path, _minimal_manifest(description=description, **kwargs), sections)
    return path


# ── 21. builtin identity ──────────────────────────────────────────────────────


GOLDEN_DEFAULT = Path(__file__).resolve().parent / "data" / "packs" / "redibis-default.rdbpack"


def test_21_builtin_packs_expose_stable_identity(tmp_path: Path):
    golden = load_pack(GOLDEN_DEFAULT)
    md = golden.manifest.metadata
    assert md.uuid and md.family_id and md.version
    assert md.uuid == backfill_uuid(md.family_id, md.version, golden.pack_sha256)

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_ar_eg_pack(first / "ar-EG.rdbpack")
    write_ar_eg_pack(second / "ar-EG.rdbpack")
    ar_a = load_pack(first / "ar-EG.rdbpack")
    ar_b = load_pack(second / "ar-EG.rdbpack")
    assert ar_a.manifest.metadata.uuid
    assert ar_a.manifest.metadata.family_id
    assert ar_a.manifest.metadata.version
    assert ar_a.manifest.metadata.uuid == ar_b.manifest.metadata.uuid

    write_fr_fr_pack(first / "fr-FR.rdbpack")
    write_fr_fr_pack(second / "fr-FR.rdbpack")
    fr_a = load_pack(first / "fr-FR.rdbpack")
    fr_b = load_pack(second / "fr-FR.rdbpack")
    assert fr_a.manifest.metadata.uuid
    assert fr_a.manifest.metadata.family_id
    assert fr_a.manifest.metadata.version
    assert fr_a.manifest.metadata.uuid == fr_b.manifest.metadata.uuid


# ── 22–24 + tamper ───────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> PackStore:
    return PackStore(LocalBackend(tmp_path / "storage"), bucket="packs")


def test_22_publish_identical_bytes_is_idempotent(tmp_path: Path):
    archive = _write_demo_pack(tmp_path / "demo.rdbpack")
    store = _store(tmp_path)
    first = store.publish(archive, author="alice")
    second = store.publish(archive, author="bob")
    assert first.uuid == second.uuid
    assert first.sha256 == second.sha256
    assert first.family_id
    assert first.contents["regex_patterns"] == 0
    listed = store.list(family_id=first.family_id)
    assert len(listed) == 1
    assert listed[0].uuid == first.uuid


def test_23_publish_version_collision_names_both_uuids(tmp_path: Path):
    a = _write_demo_pack(tmp_path / "a.rdbpack", description="one")
    b = _write_demo_pack(tmp_path / "b.rdbpack", description="two")
    store = _store(tmp_path)
    first = store.publish(a, author="alice")
    with pytest.raises(PackVersionCollisionError, match="version collision") as exc:
        store.publish(b, author="bob")
    msg = str(exc.value)
    assert first.uuid in msg
    assert first.sha256 in msg
    loaded_b = load_pack(b)
    assert loaded_b.manifest.metadata.uuid in msg
    assert loaded_b.pack_sha256 in msg


def test_24_blob_is_write_once(tmp_path: Path):
    archive = _write_demo_pack(tmp_path / "demo.rdbpack")
    store = _store(tmp_path)
    ref = store.publish(archive, author="alice")
    with pytest.raises(PackStoreError, match="immutable"):
        store._write_blob_once(ref.uuid, b"not-a-zip")


def test_27_get_tampered_blob_raises_and_writes_nothing(tmp_path: Path):
    archive = _write_demo_pack(tmp_path / "demo.rdbpack")
    store = _store(tmp_path)
    ref = store.publish(archive, author="alice")
    out_before = tmp_path / "out.zip"
    key = store._blob_key(ref.uuid)
    store.backend.put_bytes(store.bucket, key, b"tampered-bytes-not-the-pack")
    index_before = store.backend.get_bytes(store.bucket, store._index_key())
    with pytest.raises(PackStoreError, match="sha256 mismatch"):
        store.get(ref.uuid)
    assert not out_before.exists()
    assert store.backend.get_bytes(store.bucket, store._index_key()) == index_before
    assert store.head(ref.uuid) is not None


# ── 25–26. stack_uuid ────────────────────────────────────────────────────────


_STACK_PROBE = """
from types import SimpleNamespace
from redibis.pack.identity import compute_stack_uuid
packs = [
    SimpleNamespace(uuid="11111111-1111-4111-8111-111111111111", sha256="aa" * 32),
    SimpleNamespace(uuid="22222222-2222-4222-8222-222222222222", sha256="bb" * 32),
]
print(compute_stack_uuid(packs, mode="overlay"))
"""


def test_25_stack_uuid_deterministic_across_processes():
    cmd = [sys.executable, "-c", _STACK_PROBE]
    first = subprocess.check_output(cmd, text=True).strip()
    second = subprocess.check_output(cmd, text=True).strip()
    assert first == second
    packs = [
        SimpleNamespace(uuid="11111111-1111-4111-8111-111111111111", sha256="aa" * 32),
        SimpleNamespace(uuid="22222222-2222-4222-8222-222222222222", sha256="bb" * 32),
    ]
    assert compute_stack_uuid(packs, mode="overlay") == first


def test_26_reversing_stack_changes_stack_uuid():
    a = SimpleNamespace(uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", sha256="11" * 32)
    b = SimpleNamespace(uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", sha256="22" * 32)
    forward = compute_stack_uuid([a, b], mode="overlay")
    reverse = compute_stack_uuid([b, a], mode="overlay")
    assert forward != reverse
    same_mode_diff = compute_stack_uuid([a, b], mode="replace")
    assert same_mode_diff != forward


def test_stack_uuid_rejects_missing_identity():
    real = SimpleNamespace(
        uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", sha256="aa" * 32,
    )
    none_uuid = SimpleNamespace(uuid=None, sha256="aa" * 32)
    empty_uuid = SimpleNamespace(uuid="", sha256="aa" * 32)
    whitespace_uuid = SimpleNamespace(uuid="   ", sha256="aa" * 32)
    missing_sha = SimpleNamespace(uuid=real.uuid, sha256=None)
    empty_sha = SimpleNamespace(uuid=real.uuid, sha256="")

    with pytest.raises(ValueError, match="no uuid"):
        compute_stack_uuid([none_uuid, none_uuid], mode="overlay")
    with pytest.raises(ValueError, match="no uuid"):
        compute_stack_uuid([real, none_uuid], mode="overlay")
    with pytest.raises(ValueError, match="no uuid"):
        compute_stack_uuid([empty_uuid], mode="overlay")
    with pytest.raises(ValueError, match="no uuid"):
        compute_stack_uuid([whitespace_uuid], mode="overlay")
    with pytest.raises(ValueError, match="no sha256"):
        compute_stack_uuid([missing_sha], mode="overlay")
    with pytest.raises(ValueError, match="no sha256"):
        compute_stack_uuid([empty_sha], mode="overlay")
    with pytest.raises(ValueError, match="no uuid"):
        compute_stack_sha256([none_uuid], mode="overlay")


def test_stack_sha256_changes_when_content_changes():
    """Same UUID, different blob digest → different stack hash (and UUID)."""
    uid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    aaa = SimpleNamespace(uuid=uid, sha256="aa" * 32)
    zzz = SimpleNamespace(uuid=uid, sha256="ff" * 32)
    hash_aaa = compute_stack_sha256([aaa], mode="overlay")
    hash_zzz = compute_stack_sha256([zzz], mode="overlay")
    assert hash_aaa != hash_zzz
    assert compute_stack_uuid([aaa], mode="overlay") != compute_stack_uuid(
        [zzz], mode="overlay",
    )


# ── T4 / T5 / T6 ─────────────────────────────────────────────────────────────


_PACK_STACK_KEYS = ("stack_uuid", "stack_sha256", "mode", "packs")
_PACK_ENTRY_KEYS = (
    "uuid",
    "family_id",
    "kind",
    "id",
    "version",
    "parent_uuid",
    "sha256",
    "size_bytes",
    "published_at",
    "author",
    "signature",
    "contents",
    "available_locally",
)


def test_pack_stack_for_evidence_matches_plan_schema(tmp_path: Path):
    overlay = _write_demo_pack(tmp_path / "overlay.rdbpack", pack_id="policy-demo", author="acme")
    cfg = RedibisConfig.default()
    stack = apply_packs(cfg, [overlay], include_builtin_default=True)
    store = _store(tmp_path)
    published = store.publish(overlay, author="acme-governance")

    header = pack_stack_for_evidence(stack, store)
    assert set(_PACK_STACK_KEYS) <= set(header)
    assert header["stack_uuid"]
    assert header["stack_sha256"]
    assert header["mode"] == "overlay"
    assert len(header["packs"]) >= 2
    for entry in header["packs"]:
        assert set(_PACK_ENTRY_KEYS) <= set(entry)
        assert entry["uuid"]
        assert entry["family_id"]
        assert entry["version"]
        assert isinstance(entry["available_locally"], bool)
        assert isinstance(entry["contents"], dict)

    policy_entry = next(p for p in header["packs"] if p["id"] == "policy-demo")
    assert policy_entry["uuid"] == published.uuid
    assert policy_entry["available_locally"] is True

    without_store = pack_stack_for_evidence(stack, None)
    assert without_store["stack_uuid"] == header["stack_uuid"]
    builtin_entry = next(p for p in without_store["packs"] if p["id"] == "redibis-default")
    assert builtin_entry["available_locally"] is True


def test_pack_stack_uuid_matches_compute_helper(tmp_path: Path):
    overlay = _write_demo_pack(tmp_path / "o.rdbpack")
    stack = apply_packs(RedibisConfig.default(), [overlay], include_builtin_default=True)
    header = pack_stack_for_evidence(stack, None)
    refs = [
        SimpleNamespace(uuid=p["uuid"], sha256=p["sha256"])
        for p in header["packs"]
    ]
    assert compute_stack_uuid(refs, mode=header["mode"]) == header["stack_uuid"]
    assert compute_stack_sha256(refs, mode=header["mode"]) == header["stack_sha256"]


def test_unsigned_and_untrusted_signature_status(tmp_path: Path):
    unsigned = _write_demo_pack(tmp_path / "unsigned.rdbpack")
    loaded = load_pack(unsigned)
    assert loaded.signature_status is None

    signed_path = tmp_path / "signed.rdbpack"
    write_pack(
        signed_path,
        _minimal_manifest(
            pack_id="signed-demo",
            signature={"algo": "ed25519", "key_id": "acme-gov-01", "value": "AAAA"},
        ),
        {"locale/tokens.yaml": {"PHONE_NUMBER": ["phone"]}},
    )
    loaded_signed = load_pack(signed_path)
    assert loaded_signed.signature_status is not None
    assert loaded_signed.signature_status["verified"] is False
    assert loaded_signed.signature_status["reason"] == "key_id not in trust store"
    assert loaded_signed.signature_status["key_id"] == "acme-gov-01"

    with pytest.raises(Exception, match="trust store|signature"):
        load_pack(signed_path, require_signature=True)


def test_pack_require_signature_config_round_trip(tmp_path: Path):
    path = tmp_path / "redibis.yaml"
    RedibisConfig.from_dict({"pack": {"require_signature": True}}).to_yaml(path)
    loaded = RedibisConfig.from_yaml(path)
    assert loaded.pack.require_signature is True
    assert loaded.pack.trust_store == ""
    assert RedibisConfig.default().pack.require_signature is False


def test_28_pack_history_follows_parent_uuid(tmp_path: Path):
    from redibis.cli.main import main

    reports = tmp_path / "reports"
    store = PackStore(LocalBackend(reports / "_dev_storage"), bucket="pii-reports")
    v1 = _write_demo_pack(tmp_path / "v1.rdbpack", version="1.0.0", family_id="acme.policy.telecom")
    r1 = store.publish(v1, author="alice")
    v2 = _write_demo_pack(
        tmp_path / "v2.rdbpack",
        version="2.0.0",
        family_id="acme.policy.telecom",
        parent_uuid=r1.uuid,
    )
    r2 = store.publish(v2, author="alice")
    v3 = _write_demo_pack(
        tmp_path / "v3.rdbpack",
        version="3.0.0",
        family_id="acme.policy.telecom",
        parent_uuid=r2.uuid,
    )
    r3 = store.publish(v3, author="alice")
    chain = store.history("acme.policy.telecom")
    assert [r.uuid for r in chain] == [r1.uuid, r2.uuid, r3.uuid]

    rc = main([
        "pack", "history",
        "--family", "acme.policy.telecom",
        "--json",
        "--output-dir", str(reports),
    ])
    assert rc == 0


def _write_policy_pack(
    path: Path,
    *,
    regex_add: dict,
    ner_labels: list[str],
    edge_rules: list[dict],
    validators: list[str] | None = None,
    thresholds: dict | None = None,
    version: str = "1.0.0",
    pack_id: str = "telecom",
    **kwargs,
) -> Path:
    manifest = _minimal_manifest(
        pack_id=pack_id,
        version=version,
        family_id=kwargs.pop("family_id", None) or "acme.policy.telecom",
        **kwargs,
    )
    manifest["contents"] = {
        "locale": True,
        "ner": True,
        "classification": ["policy"],
        "config": bool(thresholds),
    }
    if validators:
        manifest["requires"] = {
            "redibis": ">=0.5,<1",
            "registries": {"validators": validators},
        }
    sections = {
        "locale/tokens.yaml": {"PHONE_NUMBER": ["phone"]},
        "locale/regex.yaml": {"add": regex_add, "remove": [], "replace_all": False},
        "ner/models.yaml": {"labels": ner_labels},
        "classification/packs/policy.yaml": {"edge_rules": edge_rules},
    }
    if thresholds:
        sections["config/redibis.yaml"] = {"pii": {"thresholds": thresholds}}
    write_pack(path, manifest, sections, check_registries=False)
    return path


def test_29_pack_diff_reports_rule_level_changes(tmp_path: Path):
    from redibis.pack.diff import diff_pack_paths  # noqa: E402

    a = _write_policy_pack(
        tmp_path / "a.rdbpack",
        version="1.0.0",
        regex_add={"msisdn_egypt": {"pattern": r"01[0125][0-9]{8}", "entity": "PHONE_NUMBER"}},
        ner_labels=["PERSON", "PHONE_NUMBER"],
        edge_rules=[{"id": "cr.msisdn_by_name", "condition": "name contains msisdn"}],
        validators=["validate_luhn"],
    )
    b = _write_policy_pack(
        tmp_path / "b.rdbpack",
        version="2.0.0",
        regex_add={
            "msisdn_egypt": {"pattern": r"01[0125][0-9]{8}", "entity": "PHONE_NUMBER", "score": 0.9},
            "eg_nid": {"pattern": r"[23][0-9]{13}", "entity": "EG_NATIONAL_ID"},
        },
        ner_labels=["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS"],
        edge_rules=[
            {"id": "cr.msisdn_by_name", "condition": "name contains msisdn or mobile"},
            {"id": "cr.exclude_cgi", "condition": "name matches cgi|lac"},
        ],
        validators=["validate_luhn", "validate_egypt_national_id"],
        thresholds={"presidio_min": 0.45, "gliner_min": 0.30},
    )
    diff = diff_pack_paths(a, b)
    assert "eg_nid" in diff["regex_patterns"]["added"]
    assert "msisdn_egypt" in diff["regex_patterns"]["changed"]
    assert "EMAIL_ADDRESS" in diff["ner_entities"]["added"]
    assert "cr.exclude_cgi" in diff["custom_rules"]["added"]
    assert "cr.msisdn_by_name" in diff["custom_rules"]["changed"]
    assert "validate_egypt_national_id" in diff["validators"]["added"]
    assert "presidio_min" in diff["thresholds"]["added"]

    store = PackStore(LocalBackend(tmp_path / "reports" / "_dev_storage"), bucket="pii-reports")
    ra = store.publish(a, author="alice")
    rb = store.publish(b, author="alice")
    from redibis.cli.main import main

    rc = main([
        "pack", "diff", ra.uuid, rb.uuid,
        "--json",
        "--output-dir", str(tmp_path / "reports"),
    ])
    assert rc == 0


def test_27_pack_get_cli_writes_nothing_on_mismatch(tmp_path: Path):
    from redibis.cli.main import main

    archive = _write_demo_pack(tmp_path / "demo.rdbpack")
    reports = tmp_path / "reports"
    store = PackStore(LocalBackend(reports / "_dev_storage"), bucket="pii-reports")
    ref = store.publish(archive, author="alice")
    key = store._blob_key(ref.uuid)
    store.backend.put_bytes(store.bucket, key, b"tampered-bytes-not-the-pack")
    out_dir = tmp_path / "download"
    out_dir.mkdir()
    rc = main([
        "pack", "get", ref.uuid,
        "--out", str(out_dir),
        "--output-dir", str(reports),
    ])
    assert rc == 2
    assert list(out_dir.iterdir()) == []


def test_resolve_stack_and_list_order(tmp_path: Path):
    store = _store(tmp_path)
    v1 = _write_demo_pack(tmp_path / "v1.rdbpack", version="1.0.0")
    v2 = _write_demo_pack(tmp_path / "v2.rdbpack", version="2.0.0")
    r1 = store.publish(v1, author="alice")
    r2 = store.publish(v2, author="alice")
    resolved = store.resolve_stack([r2.uuid, r1.uuid])
    assert [r.uuid for r in resolved] == [r2.uuid, r1.uuid]
    with pytest.raises(PackStoreError, match="not in store"):
        store.resolve_stack(["00000000-0000-4000-8000-000000000000"])
