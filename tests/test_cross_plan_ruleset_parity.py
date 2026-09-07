"""Cross-plan gate: one pack RuleSet drives column, text, and PII Guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_ROOT = ROOT / "services" / "pii_guard"
sys.path.insert(0, str(GUARD_ROOT))

from redibis.config import RedibisConfig
from redibis.pack import apply_packs, write_fr_fr_pack
from redibis.pack.locale_builtin import valid_fr_nir
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.column_scanner import ColumnScanner
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner
from redibis_pii_guard.service import PiiGuardService
from redibis_pii_guard.pack_runtime import PackRuntime


def test_build_effective_catalog_delegates_to_ruleset():
    from redibis.pii.regex_catalog import build_effective_catalog

    direct = RuleSetCompiler.compile_patterns(None)
    via_legacy = build_effective_catalog(None)
    assert set(direct) == set(via_legacy)


def test_fr_pack_rule_in_column_text_and_pii_guard(tmp_path: Path):
    """Integration gate from PII_GUARD plan §6."""
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("fastapi")

    pack_path = tmp_path / "fr-FR.rdbpack"
    write_fr_fr_pack(pack_path)
    stack = apply_packs(
        RedibisConfig.default(),
        [pack_path],
        include_builtin_default=False,
    )
    rs = RuleSetCompiler.from_stack(stack)
    assert rs.default_region == "FR"
    assert "fr_nir" in rs.patterns or any(
        e.entity_type == "NATIONAL_ID" for e in rs.patterns.values()
    )

    nir = valid_fr_nir()
    df = pd.DataFrame({"nir": [nir] * 8})
    pii_cfg = stack.config.pii

    # Column path (ColumnScanner → RegexRecognizer.recognize_values)
    col_dets = ColumnScanner(ruleset=rs).detect(
        df, columns=["nir"], engines="regex", pii_config=pii_cfg
    )
    col_verdict = decide_pii(col_dets[0], pii_cfg.equation_mode, pii_cfg.thresholds)
    assert col_verdict.detected is True
    assert col_verdict.entity_type == "NATIONAL_ID"

    # Legacy shim must match
    legacy = detect_pii(df, columns=["nir"], engines="regex", pii_config=pii_cfg, ruleset=rs)
    assert legacy[0].presidio_pattern == col_dets[0].presidio_pattern

    # Text path
    text_scanner = TextScanner(ruleset=rs)
    text_result = text_scanner.scan(
        f"NIR {nir} fin",
        TextScanConfig(engines="regex", language="fr", min_score=0.1),
    )
    assert text_result.ruleset_id == rs.id

    # PII Guard service
    guard = PiiGuardService(pack=PackRuntime(pack_path=str(pack_path)))
    guard_rs = guard.ruleset()
    assert guard_rs["default_region"] == "FR"

    col_scan = guard.scan(
        kind="column",
        records={"nir": [nir] * 8},
        columns=["nir"],
        engines="regex",
        apply_verdicts=True,
        equation_mode=pii_cfg.equation_mode,
    )
    nir_det = next(d for d in col_scan.detections if d.text == "nir")
    assert nir_det.detected is True
    assert nir_det.entity_type == "NATIONAL_ID"


def test_parity_corpus_unchanged_after_deep_unification():
    """Phase 0 corpus must stay bit-identical."""
    pytest.importorskip("presidio_analyzer")
    from tests.test_rdbpack_parity_corpus import build_parity_snapshot, CORPUS_PATH
    import json

    golden = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert build_parity_snapshot()["columns"] == golden["columns"]
