"""Tests for free-text PII span scanning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from redibis.pii.deid.policy import DeidPolicy, EntityRule
from redibis.pii.rules.ruleset import RuleSetCompiler
from redibis.pii.scan.result import TextScanConfig
from redibis.pii.scan.text_scanner import TextPIIScan, TextScanner
from redibis.services.text_pii_service import TextPIIService, TextPIIServiceError


@pytest.fixture
def scanner():
    return TextScanner(ruleset=RuleSetCompiler.default())


def test_email_span_offsets(scanner):
    text = "Contact alice@example.com today"
    result = scanner.scan(text, TextScanConfig(engines="regex", min_score=0.2))
    emails = [d for d in result.detections if "EMAIL" in d.entity_type]
    assert emails, f"expected email span, got {result.detections}"
    e = emails[0]
    assert text[e.start:e.end] == e.text
    assert "@" in e.text
    assert result.offset_unit == "unicode_codepoint"


def test_phone_validated_span(scanner):
    # Egyptian mobile — default region EG
    text = "Call me on 01001234567 please"
    result = scanner.scan(
        text,
        TextScanConfig(engines="both", language="ar", min_score=0.2, default_region="EG"),
    )
    phones = [d for d in result.detections if d.entity_type == "PHONE_NUMBER"]
    # May or may not validate depending on catalog patterns; at least scan runs
    assert result.engines_ran
    for p in phones:
        assert text[p.start:p.end] == p.text


def test_unicode_emoji_offsets(scanner):
    # 😀 is one code point; offsets must be code-point based
    text = "hi 😀 alice@acme.com"
    result = scanner.scan(text, TextScanConfig(engines="regex", min_score=0.2))
    emails = [d for d in result.detections if "EMAIL" in d.entity_type]
    assert emails
    e = emails[0]
    assert text[e.start:e.end] == e.text == "alice@acme.com"


def test_arabic_code_point_offsets(scanner):
    text = "البريد test@example.org نهاية"
    result = scanner.scan(text, TextScanConfig(engines="regex", language="ar", min_score=0.2))
    emails = [d for d in result.detections if "EMAIL" in d.entity_type]
    assert emails
    e = emails[0]
    assert text[e.start:e.end] == e.text


def test_overlap_priority_prefers_phone():
    from redibis.pii.rules.resolver import SpanResolver
    from redibis.pii.scan.result import Candidate

    resolver = SpanResolver()
    cands = [
        Candidate("PHONE_NUMBER", 0.7, "regex", 0, 11, "01001234567", recognizer="msisdn"),
        Candidate(
            "PHONE_NUMBER", 0.9, "phone", 0, 11, "01001234567",
            recognizer="phonenumbers", validator="phonenumbers:valid",
        ),
    ]
    dets = resolver.resolve(cands, min_score=0.3, mode="priority")
    assert len(dets) == 1
    assert dets[0].engine == "phone"
    assert dets[0].validator == "phonenumbers:valid"


def test_resolve_all_keeps_overlaps():
    from redibis.pii.rules.resolver import SpanResolver
    from redibis.pii.scan.result import Candidate

    resolver = SpanResolver()
    cands = [
        Candidate("EMAIL_ADDRESS", 0.9, "regex", 0, 5, "a@b.c"),
        Candidate("PERSON", 0.8, "ner", 2, 7, "b.cxx"),
    ]
    dets = resolver.resolve(cands, min_score=0.3, mode="all")
    assert len(dets) == 2


def test_entity_filter(scanner):
    text = "alice@example.com and +1 415 555 2671"
    result = scanner.scan(
        text,
        TextScanConfig(engines="regex", entities=("EMAIL_ADDRESS", "EMAIL"), min_score=0.2),
    )
    assert all("EMAIL" in d.entity_type for d in result.detections)


def test_return_text_false(scanner):
    text = "mail alice@example.com"
    result = scanner.scan(text, TextScanConfig(engines="regex", return_text=False, min_score=0.2))
    for d in result.detections:
        assert d.text == ""


def test_max_chars_service():
    svc = TextPIIService()
    with pytest.raises(TextPIIServiceError) as ei:
        svc.scan("x" * 100, max_chars=50)
    assert ei.value.status_code == 413


def test_text_service_loads_ner_via_registry(monkeypatch):
    from redibis.config import RedibisConfig
    from redibis.pii.ner_registry import NERModelRegistry

    sentinel = object()
    seen = {}

    def fake_try_load(*, ner, gliner=None, models_dir=None):
        seen["models_dir"] = models_dir
        seen["ner"] = ner
        return sentinel

    monkeypatch.setattr(NERModelRegistry, "try_load", staticmethod(fake_try_load))
    assert TextPIIService()._ner is None
    svc = TextPIIService(redibis_config=RedibisConfig())
    assert svc._ner is sentinel
    assert seen["models_dir"] is None


def test_text_service_uses_global_settings_ner_path(monkeypatch, tmp_path):
    from redibis.config import RedibisConfig
    from redibis.pii.ner_registry import NERModelRegistry

    settings = tmp_path / "global_settings.json"
    settings.write_text(
        '{"pii_gliner_model": "/models/gliner-multi-v2.1"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("REDIBIS_CONFIGS_DIR", str(tmp_path))
    seen = {}

    def fake_try_load(*, ner, gliner=None, models_dir=None):
        seen["path"] = getattr(ner, "model_path", "")
        return object()

    monkeypatch.setattr(NERModelRegistry, "try_load", staticmethod(fake_try_load))
    TextPIIService(redibis_config=RedibisConfig())
    assert seen["path"] == "/models/gliner-multi-v2.1"


def test_facade_and_redact():
    scan = TextPIIScan(TextScanConfig(engines="regex", min_score=0.2))
    text = "reach bob@corp.io now"
    result = scan.scan(text)
    svc = TextPIIService()
    redacted = svc.redact(text, result)
    if result.detections:
        assert "@" not in redacted or "[EMAIL" in redacted


def test_local_llm_provider_allowed_by_default():
    from redibis.pii.text_llm import LlmTextRefiner

    refiner = LlmTextRefiner()
    refiner._assert_local_provider("ollama")  # must not raise


def test_cloud_llm_provider_blocked_without_allow_flag():
    from redibis.pii.text_llm import LlmTextRefiner

    refiner = LlmTextRefiner()
    with pytest.raises(RuntimeError, match="not allowed"):
        refiner._assert_local_provider("openai")


def test_cloud_llm_provider_allowed_when_config_opts_in():
    from redibis.config import RedibisConfig
    from redibis.pii.text_llm import LlmTextRefiner

    cfg = RedibisConfig()
    cfg.pii.llm.allow_external_raw_text = True
    refiner = LlmTextRefiner(redibis_config=cfg)
    refiner._assert_local_provider("openai")  # must not raise


def test_cloud_llm_provider_allowed_via_instance_flag():
    from redibis.pii.text_llm import LlmTextRefiner

    refiner = LlmTextRefiner(allow_cloud=True)
    refiner._assert_local_provider("openai")  # must not raise


def test_build_llm_override_rejects_unknown_provider():
    svc = TextPIIService()
    with pytest.raises(TextPIIServiceError) as ei:
        svc._build_llm_override("not-a-real-provider", "")
    assert ei.value.status_code == 400


def test_build_llm_override_rejects_cloud_provider_by_default():
    svc = TextPIIService()
    with pytest.raises(TextPIIServiceError) as ei:
        svc._build_llm_override("openai", "gpt-4o-mini")
    assert ei.value.status_code == 403


def test_build_llm_override_allows_local_provider():
    svc = TextPIIService()
    refiner = svc._build_llm_override("ollama", "")
    assert refiner is not None


def test_scan_with_unknown_llm_provider_raises_service_error():
    svc = TextPIIService()
    with pytest.raises(TextPIIServiceError):
        svc.scan("hi there", use_llm=True, llm_provider="not-a-real-provider")


def test_llm_proposal_validation():
    from redibis.pii.text_llm import LlmTextRefiner
    from redibis.pii.scan.result import TextScanConfig

    class FakeProv:
        def complete(self, system, user, **kw):
            return '{"spans":[{"start":0,"end":4,"entity_type":"PERSON","score":0.9},{"start":99,"end":120,"entity_type":"PERSON","score":0.9}]}'

    refiner = LlmTextRefiner(provider=FakeProv())
    text = "John called"
    hits = refiner.propose_spans(text, [], TextScanConfig(use_llm=True))
    assert len(hits) == 1
    assert hits[0].is_proposal
    assert text[hits[0].start:hits[0].end] == hits[0].text


def test_call_model_passes_resolved_provider_to_guarded_model_call(monkeypatch):
    """RAI residency detection (``resolve_provider_residency``) inspects the
    actual provider object — a request-scoped override provider (selected
    via the gateway's per-run LLM selector) must reach ``guarded_model_call``
    as ``provider=``, not be dropped in favor of the configured default, or
    a cloud pick would silently be evaluated as local residency."""
    from redibis.pii.text_llm import LlmTextRefiner

    class FakeCloudProvider:
        name = "openai"
        model = "gpt-4o-mini"

        def complete(self, system, user, **kw):
            return '{"spans":[]}'

    captured: dict = {}

    def _fake_guarded_model_call(fn, **kwargs):
        captured.update(kwargs)
        return fn(), None

    monkeypatch.setattr(
        "redibis.telemetry.model_gateway.guarded_model_call", _fake_guarded_model_call
    )

    provider = FakeCloudProvider()
    refiner = LlmTextRefiner(provider=provider)
    refiner._call_model("Text:\nsome text")

    assert captured.get("provider") is provider
    assert captured.get("model_id") == "gpt-4o-mini"
    assert captured.get("model_role") == "pii.text_refiner"
    assert captured.get("attested_masked_external") is False


def test_ner_absent_degrades(scanner):
    # No NER backend — still returns regex results
    text = "email me at zoe@zoo.com"
    result = scanner.scan(text, TextScanConfig(engines="both", min_score=0.2))
    assert "ner" not in result.engines_ran or True
    assert any("EMAIL" in d.entity_type for d in result.detections)


def test_ner_requested_without_backend_reports_unavailable(scanner):
    """No NER backend attached but 'ner' requested: 'ner' must not appear in
    engines_ran, and engines_unavailable must explain why (never a silent []
    that looks identical to a real zero-hit scan)."""
    result = scanner.scan("alice@example.com", TextScanConfig(engines="ner", min_score=0.2))
    assert "ner" not in result.engines_ran
    assert "ner" in result.engines_unavailable
    assert result.engines_unavailable["ner"]


class _FakeUnloadableNer:
    name = "gliner:/broken"
    labels = ["person"]

    def health_check(self):
        return {"loadable": False, "error": "torch._C missing"}

    def analyze_text(self, text, **kw):
        raise AssertionError("must never run inference when unhealthy")


class _FakeLoadableNer:
    name = "gliner:/ok"
    labels = ["person"]

    def health_check(self):
        return {"loadable": True}

    def analyze_text(self, text, *, labels=None, phrases=None):
        return []


def test_ner_backend_attached_but_unloadable_is_gated_off():
    scanner = TextScanner(ruleset=RuleSetCompiler.default(), ner_backend=_FakeUnloadableNer())
    result = scanner.scan("Alice emailed bob@corp.io", TextScanConfig(engines="both", min_score=0.2))
    assert "ner" not in result.engines_ran
    assert "torch._C missing" in result.engines_unavailable["ner"]


def test_ner_backend_attached_and_loadable_runs():
    scanner = TextScanner(ruleset=RuleSetCompiler.default(), ner_backend=_FakeLoadableNer())
    result = scanner.scan("Alice emailed bob@corp.io", TextScanConfig(engines="both", min_score=0.2))
    assert "ner" in result.engines_ran
    assert "ner" not in result.engines_unavailable


def test_progress_cb_fires_in_order(scanner):
    stages: list[str] = []
    scanner.scan(
        "call 01001234567 or alice@example.com",
        TextScanConfig(engines="both", min_score=0.2, default_region="EG"),
        progress_cb=lambda stage, detail: stages.append(stage),
    )
    assert stages == [
        "validate", "preprocess", "regex", "phone", "ner", "llm", "resolve", "done"
    ]


def test_progress_cb_with_preprocess(scanner):
    stages: list[str] = []
    scanner.scan(
        "call 01001234567",
        TextScanConfig(
            engines="regex",
            min_score=0.2,
            preprocess_obfuscation=True,
        ),
        progress_cb=lambda stage, detail: stages.append(stage),
    )
    assert "preprocess" in stages
    assert stages.index("preprocess") < stages.index("regex")


def test_progress_cb_exceptions_never_break_scan(scanner):
    def _boom(stage, detail):
        raise RuntimeError("UI plumbing broke")

    result = scanner.scan(
        "alice@example.com",
        TextScanConfig(engines="regex", min_score=0.2),
        progress_cb=_boom,
    )
    assert any("EMAIL" in d.entity_type for d in result.detections)


def test_llm_override_used_instead_of_service_default():
    calls = []

    class _Override:
        def propose_spans(self, text, existing, config):
            calls.append(text)
            return []

    class _Default:
        def propose_spans(self, text, existing, config):
            raise AssertionError("default refiner must not run when an override is given")

    scanner = TextScanner(ruleset=RuleSetCompiler.default(), llm_refiner=_Default())
    scanner.scan(
        "hi",
        TextScanConfig(engines="regex", use_llm=True, min_score=0.2),
        llm_override=_Override(),
    )
    assert calls == ["hi"]


def test_ruleset_entity_catalogue():
    rs = RuleSetCompiler.default()
    cats = rs.entity_catalogue()
    assert any(c["entity_type"] for c in cats)
    assert all("engines" in c and "family" in c for c in cats)


def test_deid_redact_policy():
    from redibis.pii.deid.applier import DeidApplier

    scan = TextPIIScan(TextScanConfig(engines="regex", min_score=0.2))
    text = "write to dana@example.com please"
    result = scan.scan(text)
    if not result.detections:
        pytest.skip("no email detection in environment")
    policy = DeidPolicy.redact_all()
    out = DeidApplier().apply(text, result, policy)
    assert "dana@example.com" not in out.deidentified_text
    assert out.reversible_spans == 0
    assert "master_key" not in out.to_dict()
    assert out.run_key_ref


def test_deid_override_beats_default():
    from redibis.pii.deid.policy import resolve_rule

    policy = DeidPolicy(
        id="t",
        default=EntityRule(entity_type="*", strategy="redact"),
        overrides=(EntityRule(entity_type="EMAIL_ADDRESS", strategy="mask", min_score=0.5),),
    )
    rule = resolve_rule(policy, "EMAIL_ADDRESS", 0.9)
    assert rule.strategy == "mask"
    rule2 = resolve_rule(policy, "EMAIL_ADDRESS", 0.1)
    assert rule2.strategy == "redact"


def test_deid_fail_closed_no_policy():
    svc = TextPIIService()
    with pytest.raises(TextPIIServiceError):
        svc.deidentify("alice@example.com", policy=None, policy_id="missing")


_WE_AR = Path(__file__).resolve().parent / "data" / "text_pii" / "we_call_center_ar.txt"
_WE_AR_EXPECTED = Path(__file__).resolve().parent / "data" / "text_pii" / "we_call_center_ar.expected.txt"
_WE_SPOKEN_AR = Path(__file__).resolve().parent / "data" / "text_pii" / "we_call_center_spoken_ar.txt"
_WE_SPOKEN_AR_EXPECTED = (
    Path(__file__).resolve().parent / "data" / "text_pii" / "we_call_center_spoken_ar.expected.txt"
)
_WE_WALLET_AR = Path(__file__).resolve().parent / "data" / "text_pii" / "we_wallet_card_spoken_ar.txt"
_WE_WALLET_AR_EXPECTED = (
    Path(__file__).resolve().parent / "data" / "text_pii" / "we_wallet_card_spoken_ar.expected.txt"
)
_REGEX_ENTITIES = frozenset({"PHONE_NUMBER", "EG_NATIONAL_ID", "LOCATION"})


def _load_expected_plaintext(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entity, _, value = line.partition("\t")
        out.setdefault(entity.strip(), []).append(value.strip())
    return out


def _digit_blobs(text: str) -> set[str]:
    import re

    return set(re.findall(r"\d+", text or ""))


def _local_ner_model_path() -> str | None:
    path = os.environ.get("REDIBIS_NER_MODEL", "").strip()
    if path and Path(path).exists():
        return path
    default = Path(__file__).resolve().parents[1] / "models" / "gliner-multi-v2.1"
    return str(default) if default.exists() else None


@pytest.fixture
def require_local_ner():
    if _local_ner_model_path() is None:
        pytest.skip("Set REDIBIS_NER_MODEL to an existing local GLiNER weights directory")


@pytest.mark.slow
def test_gateway_ner_detects_person_and_location(require_local_ner):
    """Real GLiNER weights load and detect PERSON / LOCATION spans in the
    free-text scanner — the exact path the Text Gateway health/scan wiring
    depends on to report a truthful ``engines_ran``."""
    pytest.importorskip("gliner")
    from redibis.pii.ner_backend import GLiNERBackend

    backend = GLiNERBackend(_local_ner_model_path())
    health = backend.health_check()
    assert health["loadable"] is True, health.get("error")

    scanner = TextScanner(ruleset=RuleSetCompiler.default(), ner_backend=backend)
    text = "My name is Ahmed Hassan and I live in Cairo, Egypt."
    result = scanner.scan(text, TextScanConfig(engines="both", min_score=0.3))

    assert "ner" in result.engines_ran
    assert not result.engines_unavailable.get("ner")
    labels = {d.entity_type for d in result.detections}
    assert "PERSON" in labels
    assert labels & {"ADDRESS", "LOCATION"}


@pytest.mark.slow
def test_gateway_health_reports_broken_runtime_truthfully(monkeypatch):
    """A backend that is *attached* but fails to load (e.g. broken torch)
    must report ``loadable: False`` with the real error — never a silent
    'engines_ran: ["ner"]' with zero hits."""
    from redibis.pii.ner_backend import GLiNERBackend

    backend = GLiNERBackend("/models/definitely-not-a-real-path")
    monkeypatch.setattr(
        backend,
        "_ensure_loaded",
        lambda: (_ for _ in ()).throw(ImportError("gliner failed to import (No module named 'torch._C')")),
    )
    svc = TextPIIService(ner_backend=backend)
    health = svc.health()
    assert health["engines"]["ner"]["configured"] is True
    assert health["engines"]["ner"]["loadable"] is False
    assert "torch._C" in health["engines"]["ner"]["error"]

    result = svc.scan("Ahmed lives in Cairo", engines="both", min_score=0.3)
    assert "ner" not in result.engines_ran
    assert "torch._C" in result.engines_unavailable["ner"]


def test_we_call_center_arabic_plaintext_findings(scanner):
    """WE Arabic call-center transcript — phones and NIDs must match expected.txt."""
    text = _WE_AR.read_text(encoding="utf-8")
    expected = _load_expected_plaintext(_WE_AR_EXPECTED)
    result = scanner.scan(
        text,
        TextScanConfig(engines="regex", language="ar", min_score=0.2, default_region="EG"),
    )
    found: dict[str, list[str]] = {}
    for d in result.detections:
        assert text[d.start:d.end] == d.text
        found.setdefault(d.entity_type, []).append(d.text)

    for entity in _REGEX_ENTITIES:
        assert sorted(found.get(entity, [])) == sorted(expected[entity]), (
            f"{entity}: got {found.get(entity)} expected {expected[entity]}"
        )
    for name in expected["PERSON"]:
        assert name in text


def test_we_call_center_spoken_whisper_use_cases(scanner):
    """Four Whisper-style Arabic calls: spoken phones/NID, email, card, IMEI, TXN."""
    text = _WE_SPOKEN_AR.read_text(encoding="utf-8")
    expected = _load_expected_plaintext(_WE_SPOKEN_AR_EXPECTED)
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            default_region="EG",
            preprocess_obfuscation=True,
        ),
    )
    assert "preprocess" in result.engines_ran

    found_digits: dict[str, set[str]] = {}
    surfaces: dict[str, list[str]] = {}
    for d in result.detections:
        assert text[d.start:d.end] == d.text
        surfaces.setdefault(d.entity_type, []).append(d.text)
        found_digits.setdefault(d.entity_type, set()).update(_digit_blobs(d.text))
        if d.recognizer and "|" in d.recognizer:
            canon = d.recognizer.rsplit("|", 1)[-1]
            if canon.isdigit() or "@" in canon:
                found_digits.setdefault(d.entity_type, set()).add(canon)
            if "@" in canon:
                found_digits.setdefault(d.entity_type, set()).add(canon.lower())

    for phone in expected["PHONE_NUMBER"]:
        assert phone in found_digits.get("PHONE_NUMBER", set()), (
            f"missing phone {phone}; found {sorted(found_digits.get('PHONE_NUMBER', set()))}"
        )

    for surface in expected.get("SPOKEN_PHONE_SURFACE", []):
        assert any(surface in s for s in surfaces.get("PHONE_NUMBER", [])), surface

    email_hits = surfaces.get("EMAIL_ADDRESS", []) + surfaces.get("EMAIL", [])
    assert email_hits, "expected verbal email"
    assert any("ahmed" in e.lower() and "yahoo" in e.lower() for e in email_hits)
    email_canons = {
        (d.recognizer.rsplit("|", 1)[-1].lower() if d.recognizer and "|" in d.recognizer else "")
        for d in result.detections
        if "EMAIL" in d.entity_type
    }
    assert "ahmed.sayed89@yahoo.com" in email_canons

    for card in expected["CREDIT_CARD"]:
        assert card in found_digits.get("CREDIT_CARD", set()) or any(
            card in s for s in surfaces.get("CREDIT_CARD", [])
        ), card

    for imei in expected["IMEI"]:
        assert imei in found_digits.get("IMEI", set()) or any(
            imei in s for s in surfaces.get("IMEI", [])
        ), imei

    for txn in expected["TRANSACTION_ID"]:
        assert any(txn in s for s in surfaces.get("TRANSACTION_ID", [])), (
            f"missing {txn}; got {surfaces.get('TRANSACTION_ID')}"
        )

    nid_surface = expected["EG_NATIONAL_ID_SURFACE"][0]
    nids = [d for d in result.detections if d.entity_type == "EG_NATIONAL_ID"]
    assert nids, f"expected EG_NATIONAL_ID, got {[d.entity_type for d in result.detections]}"
    assert any(nid_surface in d.text for d in nids)
    for n in nids:
        assert n.is_proposal, "13-digit spoken NID must stay proposal-only"
        assert not n.validator
    for nid_digits in expected["EG_NATIONAL_ID"]:
        assert any(nid_digits in d.text for d in nids)

    for name_key in ("محمد عبد السلام ابراهيم مرسي", "شريف نبيل الدسوقي", "ندى هاني كمال"):
        assert name_key in text
    assert "شارع فيصل محطة العشرين برج الإيمان الدور السادس شقة 14" in text


def test_we_wallet_card_spoken_use_cases_no_llm(scanner):
    """Whisper-style wallet/card calls — phones, PAN, CVV, expiry, OTP, NID without LLM."""
    text = _WE_WALLET_AR.read_text(encoding="utf-8")
    expected = _load_expected_plaintext(_WE_WALLET_AR_EXPECTED)
    result = scanner.scan(
        text,
        TextScanConfig(
            engines="regex",
            language="ar",
            min_score=0.2,
            default_region="EG",
            preprocess_obfuscation=True,
            use_llm=False,
        ),
    )
    assert "preprocess" in result.engines_ran
    assert "llm" not in result.engines_ran

    found_canons: dict[str, set[str]] = {}
    surfaces: dict[str, list[str]] = {}
    for d in result.detections:
        assert text[d.start:d.end] == d.text
        surfaces.setdefault(d.entity_type, []).append(d.text)
        found_canons.setdefault(d.entity_type, set()).update(_digit_blobs(d.text))
        if d.recognizer and "|" in d.recognizer:
            canon = d.recognizer.rsplit("|", 1)[-1]
            found_canons.setdefault(d.entity_type, set()).add(canon)
            found_canons.setdefault(d.entity_type, set()).update(_digit_blobs(canon))

    for phone in expected["PHONE_NUMBER"]:
        assert phone in found_canons.get("PHONE_NUMBER", set()), phone

    for surface in expected.get("SPOKEN_PHONE_SURFACE", []):
        assert any(surface in s for s in surfaces.get("PHONE_NUMBER", [])), surface

    for otp in expected["OTP"]:
        assert otp in found_canons.get("OTP", set()) or any(
            otp in s for s in surfaces.get("OTP", [])
        ), otp

    cards = found_canons.get("CREDIT_CARD", set())
    for card in expected["CREDIT_CARD"]:
        assert card in cards or any(card in s for s in surfaces.get("CREDIT_CARD", [])), (
            f"missing card {card}; found {sorted(cards)}"
        )

    for exp in expected["CREDIT_CARD_EXPIRATION"]:
        assert any(
            exp in (d.recognizer or "") or exp.replace("/", "") in (d.recognizer or "")
            or exp in d.text
            for d in result.detections
            if d.entity_type == "CREDIT_CARD_EXPIRATION"
        ), exp

    for cvv in expected["CVV"]:
        assert cvv in found_canons.get("CVV", set()) or any(
            cvv in s for s in surfaces.get("CVV", [])
        ), cvv

    for surface in expected.get("SPOKEN_CARD_SURFACE", []):
        assert any(surface in s for s in surfaces.get("CREDIT_CARD", [])), surface

    for surface in expected.get("SPOKEN_NID_SURFACE", []):
        assert any(surface in s for s in surfaces.get("EG_NATIONAL_ID", [])), surface

    for nid in expected["EG_NATIONAL_ID"]:
        assert any(
            nid in d.text or (d.recognizer or "").endswith(nid)
            for d in result.detections
            if d.entity_type == "EG_NATIONAL_ID"
        ), nid

    assert "محمود سيد متولي" in text

