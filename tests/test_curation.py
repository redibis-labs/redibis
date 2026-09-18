"""Span curation overlay — never scans, never mutates engine output."""

from __future__ import annotations

import json

import pytest

from redibis.pii.curation import (
    Curation,
    CurationEntry,
    CurationError,
    SpanKey,
    apply_curation,
    assert_no_surfaces,
)
from redibis.pii.scan.result import Detection


def _det(start, end, et, text, *, source="engine"):
    return Detection(
        entity_type=et,
        score=0.9,
        engine="regex",
        start=start,
        end=end,
        text=text[start:end],
        source=source,
    )


def test_apply_curation_reject_removes_only_that_span():
    text = "Alice met Bob"
    dets = [_det(0, 5, "PERSON", text), _det(10, 13, "PERSON", text)]
    rec = Curation(
        run_uuid="r1",
        entries=(CurationEntry(key=SpanKey(0, 5, "PERSON"), decision="reject"),),
    )
    out = apply_curation(dets, rec, text=text)
    assert [(d.start, d.end, d.entity_type) for d in out] == [(10, 13, "PERSON")]
    assert dets[0].entity_type == "PERSON"  # original untouched


def test_apply_curation_modify_preserves_slice_integrity():
    text = "Call Alice please"
    dets = [_det(5, 17, "PERSON", text)]  # "Alice please"
    rec = Curation(
        entries=(CurationEntry(
            key=SpanKey(5, 17, "PERSON"),
            decision="modify",
            new_start=5,
            new_end=10,
        ),),
    )
    out = apply_curation(dets, rec, text=text)
    assert len(out) == 1
    assert out[0].start == 5 and out[0].end == 10
    assert text[out[0].start:out[0].end] == out[0].text == "Alice"


def test_apply_curation_refuses_empty_or_out_of_range_modify():
    text = "Alice"
    dets = [_det(0, 5, "PERSON", text)]
    rec = Curation(
        entries=(CurationEntry(
            key=SpanKey(0, 5, "PERSON"),
            decision="modify",
            new_start=2,
            new_end=2,
        ),),
    )
    with pytest.raises(CurationError, match="empty or out of range"):
        apply_curation(dets, rec, text=text)
    rec2 = Curation(
        entries=(CurationEntry(
            key=SpanKey(0, 5, "PERSON"),
            decision="modify",
            new_start=0,
            new_end=99,
        ),),
    )
    with pytest.raises(CurationError, match="empty or out of range"):
        apply_curation(dets, rec2, text=text)


def test_curation_record_contains_no_surface_text():
    rec = Curation(
        run_uuid="abc",
        text_digest="deadbeef",
        entries=(CurationEntry(
            key=SpanKey(0, 5, "PERSON"),
            decision="reject",
            reason="false positive 0123456789",
            by="specialist",
        ),),
    )
    payload = rec.to_dict()
    assert_no_surfaces(payload)
    blob = json.dumps(payload)
    assert "Alice" not in blob
    assert "text" not in payload["entries"][0]
    assert "text" not in payload["entries"][0]["key"]


def test_curation_roundtrip_is_byte_identical():
    rec = Curation(
        run_uuid="r1",
        text_digest="d1",
        auto_trim=True,
        entries=(CurationEntry(
            key=SpanKey(1, 4, "LOCATION", "engine"),
            decision="modify",
            new_start=1,
            new_end=3,
            trimmed=True,
            at="2026-09-16T00:00:00Z",
            by="op",
        ),),
    )
    blob = rec.dumps()
    again = Curation.from_dict(json.loads(blob)).dumps()
    assert again == blob
