"""Committed eval datasets must stay in sync with authored corpora."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from redibis.pii.eval.builder import build_path

ROOT = Path(__file__).resolve().parents[1]
CORPORA = ROOT / "tests" / "data" / "text_pii" / "corpora"
DATASETS = ROOT / "tests" / "data" / "text_pii" / "datasets"


def test_eval_corpora_rebuild_and_match_committed(tmp_path):
    written = build_path(CORPORA, tmp_path)
    assert written
    DATASETS.mkdir(parents=True, exist_ok=True)
    for path in written:
        committed = DATASETS / path.name
        fresh = json.loads(path.read_text(encoding="utf-8"))
        if not committed.exists():
            pytest.fail(
                f"committed dataset missing: {committed}. "
                "Run: redibis pii eval-build --corpus tests/data/text_pii/corpora "
                "--out tests/data/text_pii/datasets"
            )
        old = json.loads(committed.read_text(encoding="utf-8"))
        assert old["cases"][0]["id"] == fresh["cases"][0]["id"]
        for left, right in zip(old["cases"], fresh["cases"]):
            assert left["expected_spans"] == right["expected_spans"]
            assert left.get("forbidden_spans") == right.get("forbidden_spans")
