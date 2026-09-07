"""Plan B — ColumnScanner + detect_pii shim + shared RuleSet parity."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from redibis.config import RedibisConfig
from redibis.pack import apply_packs, write_fr_fr_pack
from redibis.pack.locale_builtin import valid_fr_nir
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.rules.resolver import VerdictResolver
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.column_scanner import ColumnScanner
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextScanner


def test_detect_pii_delegates_to_column_scanner():
    """Shim returns evidence-only rows (detected=False)."""
    df = pd.DataFrame({"email": ["a@b.co", "c@d.co"] * 4})
    dets = detect_pii(df, columns=["email"], engines="regex")
    assert len(dets) == 1
    assert dets[0].detected is False
    assert dets[0].column == "email"


def test_column_scanner_scan_kind_and_verdicts():
    pytest.importorskip("presidio_analyzer")
    df = pd.DataFrame({"email": ["person@example.com"] * 8})
    scanner = ColumnScanner()
    result = scanner.scan(df, columns=["email"], engines="regex", apply_verdicts=True)
    assert result.kind == "column"
    assert result.ruleset_id == scanner.ruleset.id
    assert any(d.text == "email" for d in result.detections)


def test_verdict_resolver_wraps_decide_pii():
    df = pd.DataFrame({"email": ["a@b.co"] * 8})
    scanner = ColumnScanner()
    evidence = scanner.detect(df, columns=["email"], engines="regex")
    assert all(d.detected is False for d in evidence)
    verdicts = VerdictResolver().resolve(
        evidence,
        equation_mode="balanced",
        thresholds=scanner.ruleset.thresholds,
    )
    # Same path as decide_pii
    manual = [
        decide_pii(d, "balanced", scanner.ruleset.thresholds) for d in evidence
    ]
    assert [v.detected for v in verdicts] == [m.detected for m in manual]
    assert [v.confidence for v in verdicts] == [m.confidence for m in manual]


def test_pack_rule_visible_to_column_and_text(tmp_path: Path):
    """Key Plan B parity: pack-compiled rule is on both scanners' RuleSet."""
    pytest.importorskip("presidio_analyzer")
    pack_path = tmp_path / "fr-FR.rdbpack"
    write_fr_fr_pack(pack_path)
    stack = apply_packs(
        RedibisConfig.default(),
        [pack_path],
        include_builtin_default=False,
    )
    rs = RuleSetCompiler.from_stack(stack)
    assert "fr_nir" in rs.patterns or any(
        "nir" in n for n in rs.patterns
    )

    nir = valid_fr_nir()
    df = pd.DataFrame({"nir": [nir] * 8, "note": ["x"] * 8})
    col = ColumnScanner(ruleset=rs)
    dets = col.detect(df, columns=["nir"], engines="regex", pii_config=stack.config.pii)
    verdicts = VerdictResolver().resolve(
        dets,
        equation_mode=stack.config.pii.equation_mode,
        thresholds=stack.config.pii.thresholds,
    )
    by_col = {d.column: d for d in verdicts}
    assert by_col["nir"].detected is True
    assert by_col["nir"].entity_type == "NATIONAL_ID"

    # Same RuleSet drives text scanner (email free_text still present)
    text = TextScanner(ruleset=rs)
    tr = text.scan(
        "mail person@domain.test please",
        TextScanConfig(engines="regex", language="fr", min_score=0.1),
    )
    assert tr.ruleset_id == rs.id
    assert col.ruleset.id == rs.id
