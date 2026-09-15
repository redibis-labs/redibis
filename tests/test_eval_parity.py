"""Independent-mode eval fingerprint must match the committed golden."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from redibis.cli.main import _eval_service_and_options
from redibis.config import RedibisConfig
from redibis.pii.eval.batch import evaluate_path
from redibis.pii.eval.parity import diff_fingerprints, fingerprint_report, load_fingerprint

ROOT = Path(__file__).resolve().parent
GOLDEN = ROOT / "data" / "text_pii" / "independent_baseline.json"
DATASETS = ROOT / "data" / "text_pii" / "datasets"
RULES = ROOT.parent / "configs" / "text_rules.default.yaml"


def _independent_eval():
    args = SimpleNamespace(
        rules=str(RULES),
        rules_defaults=False,
        draft_rules=None,
        pack=None,
        pack_stack=None,
        language="en",
        engines="regex",
        min_score=0.35,
        use_llm=False,
        llm_provider="",
        llm_model="",
        no_preprocess=False,
        overlap_iou=0.5,
        normalization="v1",
        tier="strict,value,overlap,type",
        run_uuid="",
        label="",
        equation="independent",
    )
    svc, options = _eval_service_and_options(args, RedibisConfig())
    return evaluate_path(svc, DATASETS, options=options, recursive=True)


def test_independent_eval_matches_committed_golden():
    assert GOLDEN.is_file(), "commit tests/data/text_pii/independent_baseline.json"
    got = fingerprint_report(_independent_eval())
    expected = load_fingerprint(GOLDEN)
    diffs = diff_fingerprints(expected, got)
    assert diffs == [], "\n".join(diffs)
