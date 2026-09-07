"""Phase 4 — locale sections: tokens, fr-FR pack, parity + T6."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from redibis.config import PIIConfig, RedibisConfig
from redibis.pack import apply_packs, write_ar_eg_pack, write_fr_fr_pack
from redibis.pack.locale_builtin import valid_fr_nir
from redibis.pii.context_tokens import (
    builtin_context_tokens,
    column_matches_hints,
    merge_context_tokens,
)
from redibis.pii.detector import detect_pii
from redibis.pii.equations import decide_pii
from redibis.pii.ner_backend import entity_to_gliner_phrase
from redibis.pii.regex_catalog import catalog_validators, validate_nir_mod97
from redibis.pii.regex_overrides import RegexOverrides
from redibis.pii.token_normalize import apply_normalizers


def test_nir_mod97_validator_registered():
    assert "nir_mod97" in catalog_validators
    assert "validate_nir_mod97" in catalog_validators
    nir = valid_fr_nir()
    assert validate_nir_mod97(nir)
    assert not validate_nir_mod97(nir[:-1] + ("0" if nir[-1] != "0" else "1"))


def test_accent_fold_telephone_equivalence():
    a = apply_normalizers("téléphone", ("casefold", "accent_fold"))
    b = apply_normalizers("TELEPHONE", ("casefold", "accent_fold"))
    assert a == b
    assert column_matches_hints("Téléphone_client", ["telephone"])


def test_builtin_tokens_cover_arabic_phone():
    tokens = builtin_context_tokens()
    assert "هاتف" in tokens["PHONE_NUMBER"]
    assert "رقم_قومي" in tokens["EG_NATIONAL_ID"]


def test_locale_parity_corpus_unchanged():
    """Phase 0 gate still green after context-token extraction."""
    pytest.importorskip("presidio_analyzer")
    from tests.test_rdbpack_parity_corpus import build_parity_snapshot, CORPUS_PATH
    import json

    golden = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert build_parity_snapshot()["columns"] == golden["columns"]


def test_ner_phrase_overlay():
    assert entity_to_gliner_phrase("NATIONAL_ID") == "national id"
    assert (
        entity_to_gliner_phrase(
            "NATIONAL_ID",
            phrases={"NATIONAL_ID": "numéro de sécurité sociale"},
        )
        == "numéro de sécurité sociale"
    )


def test_t6_fr_fr_pack_end_to_end(tmp_path: Path):
    """Import fr-FR → NIR as NATIONAL_ID, FR mobile detected, EG plate not."""
    pytest.importorskip("presidio_analyzer")
    pack_path = tmp_path / "fr-FR.rdbpack"
    write_fr_fr_pack(pack_path)
    stack = apply_packs(
        RedibisConfig.default(),
        [pack_path],
        include_builtin_default=False,
    )
    assert stack.config.pii.default_region == "FR"
    assert "06" in stack.config.pii.msisdn_prefixes
    assert "téléphone" in stack.context_tokens.get("PHONE_NUMBER", ())
    assert stack.ner_phrases.get("NATIONAL_ID")

    overrides = None
    if stack.config.pii.regex_overrides:
        overrides = RegexOverrides.from_dict(stack.config.pii.regex_overrides)

    nir = valid_fr_nir()
    eg_plate = "أ ب ج 1234"  # typical EG latin/arabic plate style sample
    # Use a known EG latin plate pattern sample from catalog tests if needed
    from redibis.pii.regex_catalog import CATALOG

    plate_entry = CATALOG.get("vehicle_plate_egypt_latin")
    if plate_entry is not None:
        # simple synthetic that matched before remove
        eg_plate = "ABC1234"

    df = pd.DataFrame(
        {
            "nir": [nir] * 8,
            "portable": ["0612345678", "+33698765432"] * 4,
            "لوحة": [eg_plate] * 8,
        }
    )
    pii_cfg = stack.config.pii
    dets = detect_pii(
        df,
        columns=list(df.columns),
        engines="regex",
        pii_config=pii_cfg,
        regex_overrides=overrides,
    )
    verdicts = {
        d.column: decide_pii(d, pii_cfg.equation_mode, pii_cfg.thresholds) for d in dets
    }
    assert verdicts["nir"].detected is True
    assert verdicts["nir"].entity_type == "NATIONAL_ID"
    assert verdicts["portable"].detected is True
    assert verdicts["portable"].entity_type == "PHONE_NUMBER"
    # EG plate patterns removed by fr-FR pack — must not fire as EG_VEHICLE_PLATE
    plate_v = verdicts["لوحة"]
    assert plate_v.entity_type != "EG_VEHICLE_PLATE" or plate_v.detected is False


def test_ar_eg_pack_loads(tmp_path: Path):
    path = tmp_path / "ar-EG.rdbpack"
    write_ar_eg_pack(path)
    stack = apply_packs(RedibisConfig.default(), [path], include_builtin_default=False)
    assert stack.phone_locale.get("geofence") == "egypt"
    assert stack.config.pii.default_region == "EG"
    merged = merge_context_tokens(builtin_context_tokens(), stack.context_tokens)
    assert "هاتف" in merged["PHONE_NUMBER"]


def test_write_fr_fr_fixture(tmp_path: Path):
    """Ensure committed fixture path can be refreshed from builder."""
    out = Path(__file__).resolve().parent / "data" / "packs" / "fr-FR.rdbpack"
    # Write beside tmp first for smoke, then refresh golden.
    smoke = tmp_path / "fr.rdbpack"
    sha = write_fr_fr_pack(smoke)
    assert smoke.is_file() and sha
    write_fr_fr_pack(out)
    assert out.is_file()
