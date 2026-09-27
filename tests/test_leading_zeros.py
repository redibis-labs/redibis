"""Leading zeros survive every path that re-reads a CSV the scan authored rules on.

The scan reads CSVs as text; anything that re-reads the same file (quality
monitor samples, `redibis mask`, the generated quality program) must too, or
"01012345678" becomes 1012345678 and length/format rules fail on unchanged data.
"""

from __future__ import annotations

from redibis.masking import transforms as T


def _csv(tmp_path):
    p = tmp_path / "phones.csv"
    p.write_text("msisdn,amount\n01012345678,10\n01198765432,\n", encoding="utf-8")
    return p


def test_monitor_sample_loader_keeps_text(tmp_path):
    from redibis.services.source_sample import _load_file_sample

    df = _load_file_sample(_csv(tmp_path), rows=10)
    assert df["msisdn"].tolist() == ["01012345678", "01198765432"]
    assert df["amount"].tolist() == ["10", ""], "empty stays empty, not NaN"


def test_mask_cli_loader_keeps_text(tmp_path):
    from redibis.cli.main import _load_mask_df

    assert _load_mask_df(str(_csv(tmp_path)))["msisdn"].iloc[0] == "01012345678"


def test_masking_service_loader_keeps_text(tmp_path):
    from redibis.services.masking_service import _load_df

    assert _load_df(str(_csv(tmp_path)))["msisdn"].iloc[1] == "01198765432"


def test_generated_pandas_program_reads_text(tmp_path):
    from redibis.services.quality_code import CuratedRules, render_quality_program

    code = render_quality_program(
        table="t.phones", engine="pandas",
        curated=CuratedRules(rules=[{"rule": "expect_column_value_lengths_to_be_between",
                                     "column": "msisdn",
                                     "kwargs": {"column": "msisdn", "min_value": 11,
                                                "max_value": 11}}]))
    ns = {"__name__": "cell"}
    exec(compile(code, "prog", "exec"), ns)
    df = ns["load_sample"](str(_csv(tmp_path)))
    assert ns["validate"](df)["statistics"]["successful_expectations"] == 1


def test_fake_phone_keeps_national_prefix_and_length():
    rng = T.KeyedRandom(b"k", "phone")
    fake = T.fake_phone(rng, "01012345678")
    assert len(fake) == 11 and fake.startswith("010") and fake != "01012345678"
    intl = T.fake_phone(T.KeyedRandom(b"k", "p2"), "+20 101 234 5678")
    assert intl.startswith("+20 ") and len(intl) == len("+20 101 234 5678")
