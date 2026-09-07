"""Tests for the Data tab de-identification pipeline (redibis.masking)."""

import json

import pandas as pd
import pytest

from redibis.masking import (
    MaskingPlan, ColumnMaskRule, MaskingEngine, RunKeys,
    auto_suggest_plan, suggest_rule, risk_report,
)
from redibis.masking import transforms as T


# ── transforms ──────────────────────────────────────────────────────────────

def test_keyed_random_is_deterministic():
    a = T.KeyedRandom(b"k", "label")
    b = T.KeyedRandom(b"k", "label")
    assert a.digits(10) == b.digits(10)
    assert T.KeyedRandom(b"k", "x").digits(10) != T.KeyedRandom(b"k", "y").digits(10)


def test_mask_value_keeps_last4():
    assert T.mask_value("4111111111111234", keep_last=4) == "************1234"
    assert T.mask_value("ab") == "ab"  # nothing hidden


def test_hash_value_stable_and_truncated():
    h1 = T.hash_value("hello", truncate=12)
    h2 = T.hash_value("hello", truncate=12)
    assert h1 == h2 and len(h1) == 12
    keyed = T.hash_value("hello", hmac_key=b"salt", truncate=12)
    assert keyed != h1  # salting changes the digest


def test_fpe_is_reversible_and_format_preserving():
    key = b"0" * 32
    orig = "1234-5678-9012-3456"
    kw = dict(alphabet="digits", tweak="card", column="card")
    enc = T.fpe_transform(orig, key, mode="ff3", **kw)
    dec = T.fpe_transform(enc, key, mode="ff3", decrypt=True, **kw)
    assert dec == orig
    assert enc != orig
    assert [i for i, c in enumerate(enc) if c == "-"] == [4, 9, 14]


def test_encrypt_roundtrip():
    key = b"k" * 32
    token = T.encrypt_value("secret@example.com", key)
    assert T.decrypt_value(token, key) == "secret@example.com"


def test_phone_preserves_country_code_and_shape():
    rng = T.KeyedRandom(b"k", "p")
    out = T.fake_phone(rng, "+20 100 123 4567", preserve_format=True,
                       preserve_country_code=True)
    assert out.startswith("+20 ")
    assert len(out) == len("+20 100 123 4567")


def test_arabic_detection_and_locale_resolution():
    assert T.is_arabic("محمد") and not T.is_arabic("Ahmed")
    assert T.resolve_locale("محمد", "mixed") == "ar"
    assert T.resolve_locale("Ahmed", "mixed") == "en"


def test_faker_locale_aliases_normalize():
    assert T.canonicalize_faker_locale("mixed") == "default"
    assert T.canonicalize_faker_locale("faker_arabic") == "ar"
    assert T.canonicalize_faker_locale("faker_english") == "en"
    assert T.effective_faker_locale("default", "ar") == "ar"


def test_fake_name_uses_locale_specific_faker(monkeypatch):
    seen_locales = []

    class StubFaker:
        def __init__(self, locale):
            self.locale = locale
            seen_locales.append(locale)

        def seed_instance(self, seed):
            self.seed = seed

        def name(self):
            return "محمد علي" if self.locale == "ar_AA" else "John Smith"

    monkeypatch.setattr(T, "_HAS_FAKER", True)
    monkeypatch.setattr(T, "_Faker", StubFaker)

    assert T.fake_name(T.KeyedRandom(b"k", "ar"), "ar") == "محمد علي"
    assert T.fake_name(T.KeyedRandom(b"k", "en"), "en") == "John Smith"
    assert seen_locales == ["ar_AA", "en_US"]


# ── plan + auto-suggest ───────────────────────────────────────────────────────

def test_suggest_rule_maps_entities():
    assert suggest_rule("c", "EMAIL", detected=True).strategy == "fake"
    assert suggest_rule("c", "EMAIL", detected=True).params["kind"] == "email"
    assert suggest_rule("c", "NATIONAL_ID", detected=True).strategy == "fpe"
    assert suggest_rule("c", "NATIONAL_ID", detected=True).params.get("mode") == "ff3"
    assert suggest_rule("c", None, detected=False).strategy == "passthrough"


def test_plan_yaml_roundtrip():
    plan = auto_suggest_plan("db.t", ["name", "email"],
                             [{"column": "name", "detected": True, "entity_type": "PERSON"},
                              {"column": "email", "detected": True, "entity_type": "EMAIL"}],
                             default_locale="mixed")
    plan2 = MaskingPlan.from_yaml(plan.to_yaml())
    assert plan2.schema_table == "db.t"
    assert plan2.rule_for("name").strategy == "fake"
    assert plan2.name_column() == "name"
    assert plan2.source == "auto-from-pii"


def test_plan_from_dict_normalizes_locale_aliases():
    plan = MaskingPlan.from_dict({
        "schema_table": "db.t",
        "default_locale": "mixed",
        "columns": [{
            "column": "name",
            "strategy": "fake",
            "params": {"kind": "name", "locale": "faker_arabic"},
        }],
    })
    assert plan.default_locale == "default"
    assert plan.rule_for("name").params["locale"] == "ar"


# ── engine ────────────────────────────────────────────────────────────────────

def _df():
    return pd.DataFrame({
        "name": ["Ahmed Hassan", "محمد علي", "Sara Smith"],
        "email": ["ahmed.hassan@company.com", "m.ali@company.com", "s@co.org"],
        "phone": ["+20 100 123 4567", "+20 122 456 7890", "+1 555 010 2030"],
        "ssn": ["12345678901234", "98765432109876", "11122233344455"],
        "city": ["Cairo", "Cairo", "Giza"],
    })


def _plan():
    return auto_suggest_plan("data.customers", list(_df().columns), [
        {"column": "name", "detected": True, "entity_type": "PERSON"},
        {"column": "email", "detected": True, "entity_type": "EMAIL"},
        {"column": "phone", "detected": True, "entity_type": "PHONE"},
        {"column": "ssn", "detected": True, "entity_type": "NATIONAL_ID"},
        {"column": "city", "detected": False, "entity_type": None},
    ], default_locale="mixed")


def test_engine_transforms_and_passthrough():
    df = _df()
    plan = _plan()
    eng = MaskingEngine(plan, RunKeys.mint(seed="s1"))
    out = eng.transform_dataframe(df)
    assert list(out["city"]) == list(df["city"])           # passthrough untouched
    assert all(out["name"].iloc[i] != df["name"].iloc[i] for i in range(3))
    assert out["phone"].iloc[0].startswith("+20 ")          # phone format preserved
    assert len(out["ssn"].iloc[0]) == len(df["ssn"].iloc[0])  # fpe length preserved


def test_compare_record_shape():
    df = _df()
    plan = _plan()
    eng = MaskingEngine(plan, RunKeys.mint(seed="s1"))
    cmp = eng.compare_record(df, row_index=1)
    assert cmp["row_index"] == 1
    assert cmp["total_rows"] == 3
    assert len(cmp["fields"]) == len(df.columns)
    name_field = next(f for f in cmp["fields"] if f["column"] == "name")
    assert name_field["raw"] == df["name"].iloc[1]
    assert name_field["masked"] != name_field["raw"]
    assert name_field["strategy"] == "fake"
    city_field = next(f for f in cmp["fields"] if f["column"] == "city")
    assert city_field["raw"] == city_field["masked"]
    json.dumps(cmp)  # numpy scalars must not leak into API responses


def test_compare_record_json_safe_numeric():
    df = pd.DataFrame({"phone": [201001234567], "age": [30]})
    plan = MaskingPlan(schema_table="t.t", columns=[
        ColumnMaskRule(column="phone", strategy="passthrough"),
        ColumnMaskRule(column="age", strategy="passthrough"),
    ])
    cmp = MaskingEngine(plan, RunKeys.mint(seed="s1")).compare_record(df, row_index=0)
    json.dumps(cmp)
    assert isinstance(cmp["fields"][0]["raw"], int)
    assert isinstance(cmp["fields"][1]["raw"], int)


def test_compare_record_clamps_index():
    df = _df()
    plan = _plan()
    eng = MaskingEngine(plan, RunKeys.mint(seed="s1"))
    low = eng.compare_record(df, row_index=-5)
    assert low["row_index"] == 0
    high = eng.compare_record(df, row_index=999)
    assert high["row_index"] == 2
    empty = eng.compare_record(pd.DataFrame(), row_index=0)
    assert empty["total_rows"] == 0
    assert empty["fields"] == []


def test_adapt_plan_runtime_downgrades_ff3_without_lib(monkeypatch):
    from redibis.masking.plan import adapt_plan_runtime
    monkeypatch.setattr(T, "_HAS_FF3", False)
    plan = MaskingPlan(schema_table="t.t", columns=[
        ColumnMaskRule(column="tax_registration_number", strategy="fpe",
                       params={"mode": "ff3", "alphabet": "digits", "key_ref": "k1"}),
        ColumnMaskRule(column="city", strategy="passthrough"),
    ])
    assert adapt_plan_runtime(plan) is True
    assert plan.columns[0].params["mode"] == "keystream"
    assert adapt_plan_runtime(plan) is False


def test_fpe_preview_without_ff3(monkeypatch):
    monkeypatch.setattr(T, "_HAS_FF3", False)
    df = pd.DataFrame({"tax_registration_number": ["29001011234567"]})
    plan = MaskingPlan(schema_table="t.t", columns=[
        ColumnMaskRule(column="tax_registration_number", strategy="fpe",
                       params={"mode": "keystream", "alphabet": "digits", "key_ref": "k1"}),
    ])
    cmp = MaskingEngine(plan, RunKeys.mint(seed="s1")).compare_record(df, row_index=0)
    assert cmp["fields"][0]["masked"] != cmp["fields"][0]["raw"]


def test_sync_plan_from_pii_upgrades_passthrough():
    from redibis.masking.plan import sync_plan_from_pii
    plan = MaskingPlan(schema_table="t.t", columns=[
        ColumnMaskRule(column="email", strategy="passthrough"),
        ColumnMaskRule(column="city", strategy="passthrough"),
    ])
    dets = [{"column": "email", "detected": True, "entity_type": "EMAIL_ADDRESS"}]
    assert sync_plan_from_pii(plan, ["email", "city"], dets) is True
    email = plan.rule_for("email")
    assert email.strategy == "fake"
    assert plan.rule_for("city").strategy == "passthrough"


def test_engine_determinism_within_seed():
    df = _df()
    plan = _plan()
    a = MaskingEngine(plan, RunKeys.mint(seed="s1")).transform_dataframe(df)
    b = MaskingEngine(plan, RunKeys.mint(seed="s1")).transform_dataframe(df)
    assert list(a["name"]) == list(b["name"])
    c = MaskingEngine(plan, RunKeys.mint(seed="other")).transform_dataframe(df)
    assert list(a["name"]) != list(c["name"])  # new seed → different output


def test_email_localpart_derived_from_faked_name():
    df = _df()
    plan = _plan()
    out = MaskingEngine(plan, RunKeys.mint(seed="s1")).transform_dataframe(df)
    faked_name = out["name"].iloc[0].lower().replace(" ", ".")
    # English row: local part derived from faked name; domain preserved
    assert out["email"].iloc[0].endswith("@company.com")
    assert out["email"].iloc[0].split("@")[0] in faked_name


def test_locale_aware_names():
    df = _df()
    plan = _plan()
    out = MaskingEngine(plan, RunKeys.mint(seed="s1")).transform_dataframe(df)
    assert T.is_arabic(out["name"].iloc[1])        # Arabic input → Arabic fake
    assert not T.is_arabic(out["name"].iloc[0])     # English input → English fake


def test_arabic_default_forces_arabic_faker():
    df = pd.DataFrame({"name": ["Ahmed Hassan"]})
    plan = MaskingPlan(
        schema_table="t",
        default_locale="ar",
        columns=[ColumnMaskRule(
            column="name",
            strategy="fake",
            params={"kind": "name", "locale": "default"},
        )],
    )
    out = MaskingEngine(plan, RunKeys.mint(seed="s1")).transform_dataframe(df)
    assert T.is_arabic(out["name"].iloc[0])


def test_runkeys_public_view_has_no_secrets():
    keys = RunKeys.mint(seed="topsecret")
    pub = keys.to_public()
    assert "master_key_hex" not in pub and "seed" not in pub
    assert pub["key_refs"] == ["k1"]


def test_apply_position_slice_mask_middle():
    out = T.apply_position_slice(
        "01012345678",
        lambda seg: T.mask_value(seg, keep_first=0, keep_last=0, mask_char="*"),
        start_index=2,
    )
    assert out[:2] == "01"
    assert out[2:].count("*") == len("012345678")


def test_fpe_position_preserves_prefix():
    key = b"0" * 32
    orig = "01012345678"
    kw = dict(alphabet="digits", tweak="m", column="mobile", mode="ff3")
    enc = T.apply_position_slice(
        orig,
        lambda seg: T.fpe_transform(seg, key, **kw),
        start_index=2,
    )
    assert enc[:2] == "01"
    assert enc != orig
    dec = T.apply_position_slice(
        enc,
        lambda seg: T.fpe_transform(seg, key, decrypt=True, **kw),
        start_index=2,
    )
    assert dec == orig


def test_fake_from_regex_deterministic():
    rng_a = T.KeyedRandom(b"k", "col|010")
    rng_b = T.KeyedRandom(b"k", "col|010")
    pat = r"01[0125]\d{8}"
    assert T.fake_from_regex(rng_a, pat, deterministic=True) == \
           T.fake_from_regex(rng_b, pat, deterministic=True)
    assert len(T.fake_from_regex(rng_a, pat, deterministic=True)) == 11


def test_regex_library_resolve():
    from redibis.masking.regex_library import resolve_pattern, list_patterns, clear_cache
    clear_cache()
    assert resolve_pattern("eg_mobile") == r"01[0125]\d{8}"
    assert resolve_pattern("missing") is None
    assert any(p["name"] == "eg_mobile" for p in list_patterns())


def test_engine_fpe_from_index_2():
    df = pd.DataFrame({"mobile": ["01012345678"]})
    plan = MaskingPlan(
        schema_table="t",
        columns=[ColumnMaskRule(
            column="mobile", strategy="fpe", deterministic=True,
            params={"alphabet": "digits", "key_ref": "k1", "start_index": 2},
        )],
    )
    out = MaskingEngine(plan, RunKeys.mint(seed="s")).transform_dataframe(df)
    assert out["mobile"].iloc[0][:2] == "01"
    assert out["mobile"].iloc[0] != df["mobile"].iloc[0]


def test_engine_fake_regex_library():
    df = pd.DataFrame({"mobile": ["01012345678"]})
    plan = MaskingPlan(
        schema_table="t",
        columns=[ColumnMaskRule(
            column="mobile", strategy="fake", deterministic=True,
            params={"kind": "regex", "regex_library": "eg_mobile"},
        )],
    )
    out = MaskingEngine(plan, RunKeys.mint(seed="s")).transform_dataframe(df)
    fake = out["mobile"].iloc[0]
    assert fake.startswith("01") and len(fake) == 11 and fake[2] in "0125"


def test_risk_flags_low_cardinality_hash():
    df = pd.DataFrame({"gender": ["M", "F"] * 50})
    plan = MaskingPlan(schema_table="t", columns=[
        ColumnMaskRule(column="gender", strategy="hash", deterministic=True,
                       params={"algo": "sha256"})])
    findings = risk_report(df, plan)
    assert any(f["severity"] == "high" for f in findings)


# ── FF3-1 FPE production tests ──────────────────────────────────────────────

@pytest.mark.skipif(not T._HAS_FF3, reason="ff3 not installed")
def test_ff3_nist_vector():
    from ff3 import FF3Cipher
    key = "EF4359D8D580AA4F7F036D6F04FC6A94"
    tweak = "D8E7920AFA330A73"
    plain = "890121234567890000"
    expected = "750918814058654607"
    assert FF3Cipher(key, tweak, radix=10).encrypt(plain) == expected


@pytest.mark.skipif(not T._HAS_FF3, reason="ff3 not installed")
def test_fpe_ff3_alnum_and_custom_roundtrip():
    key = b"a" * 32
    cases = [("alnum", "abc1"), ("0123456789abcdef", "abcde")]
    for alphabet, orig in cases:
        enc = T.fpe_transform(orig, key, alphabet=alphabet, mode="ff3", column="c")
        dec = T.fpe_transform(enc, key, alphabet=alphabet, mode="ff3",
                              column="c", decrypt=True)
        assert dec == orig


@pytest.mark.skipif(not T._HAS_FF3, reason="ff3 not installed")
def test_fpe_ff3_domain_edges():
    key = b"b" * 32
    col = "id"
    with pytest.raises(ValueError, match="minimum 6"):
        T.fpe_transform("12345", key, mode="ff3", column=col)
    assert T.fpe_transform("123456", key, mode="ff3", column=col) != "123456"
    too_long = "1" * 57
    with pytest.raises(ValueError, match="max 56"):
        T.fpe_transform(too_long, key, mode="ff3", column=col)


@pytest.mark.skipif(not T._HAS_FF3, reason="ff3 not installed")
def test_fpe_short_value_keystream_mixed_mode():
    key = b"c" * 32
    meta: dict = {}
    out = T.fpe_transform("12345", key, mode="ff3", column="c",
                          short_value_policy="keystream", meta_out=meta)
    assert out != "12345"
    assert meta["modes_used"] == {"keystream"}


@pytest.mark.skipif(not T._HAS_FF3, reason="ff3 not installed")
def test_fpe_determinism_and_key_ref():
    mk = RunKeys.mint(seed="fpe-seed").master_key
    k1 = T._derive_key(mk, "k1")
    k2 = T._derive_key(mk, "k2")
    a = T.fpe_transform("123456789012", k1, mode="ff3", column="c")
    b = T.fpe_transform("123456789012", k1, mode="ff3", column="c")
    c = T.fpe_transform("123456789012", k2, mode="ff3", column="c")
    assert a == b and a != c


@pytest.mark.skipif(not T._HAS_FF3, reason="ff3 not installed")
def test_fpe_ff3_diffusion():
    key = b"d" * 32
    a = T.fpe_transform("123456789012", key, mode="ff3", column="c")
    b = T.fpe_transform("123456789013", key, mode="ff3", column="c")
    digit_positions = [i for i, ch in enumerate(a) if ch.isdigit()]
    changes = sum(1 for i in digit_positions if a[i] != b[i])
    assert changes / len(digit_positions) > 0.5


def test_fpe_keystream_lacks_diffusion():
    key = b"e" * 32
    a = T.fpe_transform("123456789012", key, mode="keystream", column="c")
    b = T.fpe_transform("123456789013", key, mode="keystream", column="c")
    digit_positions = [i for i, ch in enumerate(a) if ch.isdigit()]
    changes = sum(1 for i in digit_positions if a[i] != b[i])
    assert changes / len(digit_positions) <= 0.5


def test_fpe_keystream_back_compat_roundtrip():
    key = b"0" * 32
    orig = "1234-5678-9012-3456"
    enc = T.fpe_transform(orig, key, mode="keystream", tweak="card")
    dec = T.fpe_transform(enc, key, mode="keystream", tweak="card", decrypt=True)
    assert dec == orig


def test_x1_encrypt_token_decrypts():
    key = b"k" * 32
    old = T._HAS_AESGCM
    T._HAS_AESGCM = False
    try:
        token = T.encrypt_value("legacy", key, require_aes_gcm=False)
        assert token.startswith("x1:")
        assert T.decrypt_value(token, key) == "legacy"
    finally:
        T._HAS_AESGCM = old


def test_require_authenticated_crypto_raises(monkeypatch):
    monkeypatch.setattr(T, "_HAS_FF3", False)
    monkeypatch.setattr(T, "_HAS_AESGCM", False)
    with pytest.raises(ValueError, match="ff3"):
        T.fpe_transform("123456", b"k" * 32, mode="ff3", column="c", require_ff3=True)
    with pytest.raises(ValueError, match="AES-256-GCM"):
        T.encrypt_value("x", b"k" * 32, require_aes_gcm=True)


def test_manifest_v2_fpe_metadata():
    from redibis.masking.engine import build_manifest
    plan = MaskingPlan(schema_table="t", columns=[
        ColumnMaskRule(column="ssn", strategy="fpe",
                       params={"mode": "ff3", "alphabet": "digits", "key_ref": "k1"}),
    ])
    keys = RunKeys.mint(seed="m")
    meta = {"ssn": {"modes_used": {"ff3"}, "mode": "ff3"}}
    m = build_manifest(plan, keys, column_run_meta=meta, rows=1)
    assert m["redibis_masking_manifest"] == "v2"
    col = m["columns"][0]
    assert col["mode"] == "ff3"
    assert "FF3-1" in col["algorithm"]


def test_manifest_v1_style_keystream_default():
    from redibis.masking.engine import _fpe_manifest_mode
    rule = ColumnMaskRule(column="c", strategy="fpe", params={"alphabet": "digits"})
    mode, _algo = _fpe_manifest_mode(rule, None)
    assert mode == "keystream"


def test_build_audit_report():
    from redibis.masking.engine import build_audit_report
    df = _df()
    plan = _plan()
    keys = RunKeys.mint(seed="audit")
    audit = build_audit_report(
        plan, keys, df=df,
        export_format="csv",
        export_filename="customers_mask_audit.csv",
        source_label="customers.csv",
        session_id="sess-1",
    )
    assert audit["redibis_masking_audit"] == "v1"
    assert audit["run_id"] == keys.run_id
    assert audit["source"]["rows"] == 3
    assert audit["summary"]["columns_transformed"] >= 1
    assert audit["summary"]["pii_columns_detected"] >= 1
    assert "manifest" in audit
    assert audit["keys"]["run_id"] == keys.run_id
    assert "master_key_hex" not in json.dumps(audit)
    assert len(audit["compliance_notes"]) >= 1
    assert isinstance(audit["risk_findings"], list)


def test_risk_keystream_fpe_high():
    df = pd.DataFrame({"id": ["123456789012"]})
    plan = MaskingPlan(schema_table="t", columns=[
        ColumnMaskRule(column="id", strategy="fpe",
                       params={"mode": "keystream", "alphabet": "digits"}),
    ])
    findings = risk_report(df, plan)
    assert any(f["severity"] == "high" and "keystream" in f["issue"].lower() for f in findings)


def test_capabilities_includes_ff3():
    caps = T.capabilities()
    assert "ff3" in caps
    assert caps["ff3"] == T._HAS_FF3


def test_web_extra_includes_mask_deps():
    """PyPI/Docker dashboard installs must pull redibis[mask] (FF3-1, AES-GCM, …)."""
    from importlib.metadata import metadata
    reqs = metadata("redibis").get_all("Requires-Dist") or []
    web = [r for r in reqs if 'extra == "web"' in r]
    for pkg in ("ff3", "cryptography", "faker", "pyarrow", "rstr"):
        assert any(r.lower().startswith(pkg) for r in web), f"{pkg} missing from web extra"
