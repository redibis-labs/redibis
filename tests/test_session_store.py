"""Operator sessions: session_name / date / usecases."""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from redibis.pii.curation import Curation, CurationEntry, SpanKey
from redibis.pii.session_store import SessionError, SessionStore
from redibis.pii.usecase_store import usecase_from_payload


def test_create_save_reload_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    store = SessionStore(root=tmp_path / "pii_sessions")
    meta = store.create("Call review", owner="op")
    uc1, _ = usecase_from_payload({
        "name": "case-a",
        "text": "Alice called",
        "language": "en",
        "expected_spans": [{"start": 0, "end": 5, "entity_type": "PERSON", "value": "Alice"}],
    })
    uc2, _ = usecase_from_payload({
        "name": "case-b",
        "text": "Bob called",
        "language": "en",
        "expected_spans": [{"start": 0, "end": 3, "entity_type": "PERSON", "value": "Bob"}],
    })
    cur = Curation(
        run_uuid="r1",
        entries=(CurationEntry(key=SpanKey(0, 5, "PERSON"), decision="reject"),),
    )
    saved1 = store.save_usecase(meta.slug, uc1, curation=cur)
    saved2 = store.save_usecase(meta.slug, uc2)
    day = store.dates(meta.slug)[0]
    root = tmp_path / "pii_sessions" / meta.slug / day / "usecases"
    assert (root / saved1.id / "v1.json").is_file()
    assert (root / saved2.id / "v1.json").is_file()
    loaded = store.usecases(meta.slug, date=day).get(saved1.id)
    assert loaded is not None
    assert loaded.context.get("curation", {}).get("entries")
    blob = store.export(meta.slug, date=day)
    names = zipfile.ZipFile(BytesIO(blob)).namelist()
    assert any(saved1.id in n for n in names)
    assert any(saved2.id in n for n in names)


def test_duplicate_slug_is_refused(tmp_path):
    store = SessionStore(root=tmp_path / "pii_sessions")
    store.create("Dup")
    with pytest.raises(SessionError, match="already exists"):
        store.create("Dup")
