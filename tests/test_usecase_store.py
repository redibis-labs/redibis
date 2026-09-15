"""Filesystem use-case store and download-without-a-run contract."""

from __future__ import annotations

import json

import pytest

from redibis.pii.usecase_store import (
    KIND,
    UseCaseStore,
    UseCaseValidationError,
    usecase_from_payload,
)


def test_download_works_without_any_run(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    store = UseCaseStore()
    uc = store.create(name="empty", text="Agent: hello", language="en")
    payload = json.loads(json.dumps(uc.to_dict()))
    assert payload["kind"] == KIND
    assert payload["expected_spans"] == []
    assert payload["text"] == "Agent: hello"
    assert payload["version"] == 1


def test_save_mints_new_version_and_old_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    store = UseCaseStore()
    first = store.create(name="v1", text="Agent: hello", language="en")
    second = store.save(first.__class__.from_dict({**first.to_dict(), "name": "v2", "version": 0}))
    assert second.version == 2
    assert store.get(first.id, version=1).name == "v1"
    assert store.get(first.id, version=2).name == "v2"
    assert store.get(first.id).version == 2


def test_value_offset_mismatch_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    text = "hello hello world"
    with pytest.raises(UseCaseValidationError) as exc:
        usecase_from_payload({
            "name": "dup",
            "text": text,
            "language": "en",
            "expected_spans": [{
                "id": "s1",
                "start": 0,
                "end": 4,
                "entity_type": "PERSON",
                "value": "hello",
            }],
        })
    assert exc.value.span_id == "s1"
    assert "hello" in str(exc.value)


def test_unique_value_is_relocated(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    text = "prefix 01011223344 suffix"
    uc, relocated = usecase_from_payload({
        "name": "reloc",
        "text": text,
        "language": "en",
        "expected_spans": [{
            "id": "s1",
            "start": 0,
            "end": 5,
            "entity_type": "PHONE_NUMBER",
            "value": "01011223344",
        }],
    })
    assert relocated
    assert uc.expected_spans[0]["start"] == text.index("01011223344")
    store = UseCaseStore()
    saved = store.save(uc)
    assert saved.expected_spans[0]["value"] == "01011223344"


def test_roundtrip_download_then_import_is_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    store = UseCaseStore()
    original = store.create(
        name="roundtrip",
        text="Agent: hello",
        language="en",
        tags=["eg"],
        expected_spans=[],
        notes="n1",
    )
    payload = json.loads(json.dumps(original.to_dict()))
    imported, _ = usecase_from_payload(payload)
    again = store.save(imported)
    left = original.to_dict()
    right = again.to_dict()
    for key in ("kind", "schema_version", "id", "name", "text", "language", "tags",
                "expected_spans", "forbidden_spans", "rule_edits", "notes"):
        assert left[key] == right[key]
    assert again.version == original.version + 1


def test_usecase_api_download_without_run(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from redibis.webapp.backend import app

    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/gateway/usecases",
            json={"name": "empty", "text": "Agent: hello", "language": "en"},
        )
        assert created.status_code == 201, created.text
        uc_id = created.json()["id"]
        dl = client.get(f"/api/gateway/usecases/{uc_id}/download")
        assert dl.status_code == 200
        assert "attachment" in dl.headers.get("Content-Disposition", "")
        payload = dl.json()
        assert payload["kind"] == KIND
        assert payload["text"] == "Agent: hello"
        assert payload["expected_spans"] == []
        page = client.get(f"/gateway/usecases/{uc_id}")
        assert page.status_code == 200
        assert b"gateway_usecase.js" in page.content


def test_usecase_api_rejects_ambiguous_value(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from redibis.webapp.backend import app

    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    with TestClient(app) as client:
        created = client.post(
            "/api/gateway/usecases",
            json={"name": "dup", "text": "hello hello world", "language": "en"},
        )
        uc_id = created.json()["id"]
        bad = client.put(
            f"/api/gateway/usecases/{uc_id}",
            json={
                "expected_spans": [{
                    "id": "s1",
                    "start": 0,
                    "end": 4,
                    "entity_type": "PERSON",
                    "value": "hello",
                }]
            },
        )
        assert bad.status_code == 422
        detail = bad.json()["detail"]
        blob = detail if isinstance(detail, str) else json.dumps(detail)
        assert "s1" in blob
