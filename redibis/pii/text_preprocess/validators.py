"""Route canonicalized digit / email / age strings through existing validators."""

from __future__ import annotations

import re
from typing import Optional

from redibis.pii.rules.recognizers import RecognizeContext
from redibis.pii.text_preprocess.registry import register_text_validator
from redibis.pii.text_preprocess.surface import ValidationOutcome

_PHONE_HINTS = frozenset({
    "phone", "mobile", "msisdn", "موبايل", "هاتف", "تليفون", "تواصل", "رقم",
})
_NID_HINTS = frozenset({
    "nid", "national", "قومي", "هوية", "رقم_قومي", "الرقم_القومي",
})
_IMEI_HINTS = frozenset({"imei", "device", "جهاز"})
_IMSI_HINTS = frozenset({"imsi", "subscriber", "مشترك"})
_ICCID_HINTS = frozenset({"iccid", "sim", "شريحة"})
_CARD_HINTS = frozenset({
    "card", "visa", "mastercard", "بطاقة", "ائتمان", "فيزا", "كارت", "ميزة",
    "مشتريات", "credit_card",
})
_CVV_HINTS = frozenset({"cvv", "cvc", "سي في", "ضهر", "ظهر"})
_OTP_HINTS = frozenset({"otp", "كود", "رمز", "تأكيد", "تحقق"})
_EXPIRY_HINTS = frozenset({"صلاحية", "انتهاء", "expiry", "expiration", "شهر"})
_PARTIAL_CARD_HINTS = frozenset({"آخر", "اخر", "أول", "اول", "bin"})
_AGE_HINTS = frozenset({"age", "years", "سنة", "سنوات", "عمر", "سني"})


def _label_blob(label: str, entity_hint: str) -> str:
    return f"{label} {entity_hint}".lower()


def _has_any(blob: str, tokens: frozenset[str]) -> bool:
    return any(t in blob for t in tokens)


def _truthy(result) -> bool:
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if hasattr(result, "valid"):
        return bool(getattr(result, "valid"))
    return bool(result)


@register_text_validator("digit_router")
class DigitRouterValidator:
    """Disambiguate digit runs into phone / NID / IMEI / IMSI / ICCID / card."""

    name = "digit_router"
    entity_types = frozenset({
        "PHONE_NUMBER", "EG_NATIONAL_ID", "IMEI", "IMSI", "ICCID", "CREDIT_CARD",
        "CVV", "OTP", "CREDIT_CARD_EXPIRATION",
    })

    def validate(
        self,
        canonical: str,
        *,
        ctx: RecognizeContext,
        entity_hint: str = "",
        label: str = "",
    ) -> ValidationOutcome:
        raw = (canonical or "").strip()
        digits = re.sub(r"\D", "", raw)
        if not digits and not raw:
            return ValidationOutcome(ok=False, reason="empty")

        blob = _label_blob(label, entity_hint)
        hint = (entity_hint or "").upper().replace(" ", "_")
        nid_context = hint in {"EG_NATIONAL_ID", "NATIONAL_ID"} or _has_any(blob, _NID_HINTS)
        phone_context = hint in {"PHONE_NUMBER", "PHONE", "MSISDN"} or _has_any(blob, _PHONE_HINTS)
        card_context = hint in {"CREDIT_CARD", "PAN"} or _has_any(blob, _CARD_HINTS)
        cvv_context = hint == "CVV" or _has_any(blob, _CVV_HINTS)
        otp_context = hint == "OTP" or _has_any(blob, _OTP_HINTS)
        expiry_context = hint in {"CREDIT_CARD_EXPIRATION", "EXPIRATION"} or _has_any(
            blob, _EXPIRY_HINTS
        )
        partial_context = _has_any(blob, _PARTIAL_CARD_HINTS)

        if cvv_context and 3 <= len(digits) <= 4:
            return ValidationOutcome(
                ok=True,
                validator="",
                entity_type="CVV",
                score=0.75,
                is_proposal=True,
                reason="context_cvv",
            )

        if otp_context and 4 <= len(digits) <= 8:
            return ValidationOutcome(
                ok=True,
                validator="labeled_secret",
                entity_type="OTP",
                score=0.8,
                is_proposal=False,
                reason="context_otp",
            )

        if expiry_context and 3 <= len(digits) <= 4:
            mm = int(digits[:2]) if len(digits) == 4 else int(digits[0])
            yy = int(digits[2:]) if len(digits) == 4 else int(digits[1:])
            if 1 <= mm <= 12 and 0 <= yy <= 99:
                return ValidationOutcome(
                    ok=True,
                    validator="",
                    entity_type="CREDIT_CARD_EXPIRATION",
                    score=0.7,
                    is_proposal=True,
                    reason="context_expiry",
                )

        if nid_context:
            nid = self._validate_nid(digits)
            if nid.ok:
                return nid
            if 12 <= len(digits) <= 15:
                return ValidationOutcome(
                    ok=True,
                    validator="",
                    entity_type="EG_NATIONAL_ID",
                    score=0.4,
                    is_proposal=True,
                    reason="context_nid_unvalidated",
                )

        if phone_context and not nid_context and not card_context:
            phone = self._validate_phone(digits, ctx)
            if phone.ok:
                return phone
            if _has_any(blob, _PHONE_HINTS) and 8 <= len(digits) <= 15:
                return ValidationOutcome(
                    ok=True,
                    validator="",
                    entity_type="PHONE_NUMBER",
                    score=0.45,
                    is_proposal=True,
                    reason="context_phone_unvalidated",
                )

        if hint == "IMEI" or _has_any(blob, _IMEI_HINTS):
            imei = self._validate_imei(digits)
            if imei.ok:
                return imei
            if _has_any(blob, _IMEI_HINTS) and len(digits) == 15:
                return ValidationOutcome(
                    ok=True,
                    validator="",
                    entity_type="IMEI",
                    score=0.45,
                    is_proposal=True,
                    reason="context_imei_unvalidated",
                )

        if hint == "IMSI" or _has_any(blob, _IMSI_HINTS):
            imsi = self._validate_imsi(digits)
            if imsi.ok:
                return imsi

        if hint == "ICCID" or _has_any(blob, _ICCID_HINTS):
            iccid = self._validate_iccid(digits)
            if iccid.ok:
                return iccid

        if card_context or partial_context or hint == "CREDIT_CARD":
            card = self._validate_card(digits)
            if card.ok:
                return card
            if 13 <= len(digits) <= 19:
                return ValidationOutcome(
                    ok=True,
                    validator="",
                    entity_type="CREDIT_CARD",
                    score=0.5,
                    is_proposal=True,
                    reason="context_card_unvalidated",
                )
            if partial_context or card_context:
                if 4 <= len(digits) <= 8:
                    return ValidationOutcome(
                        ok=True,
                        validator="",
                        entity_type="CREDIT_CARD",
                        score=0.55,
                        is_proposal=True,
                        reason="context_partial_card",
                    )

        # Length / structure cascade (validated only — no unlabeled proposals).
        phone = self._validate_phone(digits, ctx)
        if phone.ok and not phone.is_proposal:
            return phone
        for fn in (
            self._validate_nid,
            self._validate_iccid,
            self._validate_imei,
            self._validate_imsi,
            self._validate_card,
        ):
            out = fn(digits)
            if out.ok and not out.is_proposal:
                return out
        return ValidationOutcome(ok=False, reason="no_match")

    def _validate_phone(self, digits: str, ctx: RecognizeContext) -> ValidationOutcome:
        from redibis.pii.regex_catalog import normalize_msisdn_egypt
        from redibis.pii.rules.recognizers import PhoneRecognizer

        region = getattr(ctx, "language", "ar")
        # Prefer EG for Arabic; PhoneRecognizer will map language → region
        # via ruleset when available — fall back to EG.
        try:
            region_code = "EG" if (region or "").startswith("ar") else "EG"
        except Exception:
            region_code = "EG"

        candidates = [digits]
        canon = normalize_msisdn_egypt(digits)
        if canon:
            candidates.append(canon)
            # also national form with leading 0
            if canon.startswith("+20") and len(canon) == 13:
                candidates.append("0" + canon[3:])

        for value in candidates:
            if PhoneRecognizer._is_valid_phone(value, region_code):
                return ValidationOutcome(
                    ok=True,
                    validator="phonenumbers:valid",
                    entity_type="PHONE_NUMBER",
                    score=0.92,
                )
        return ValidationOutcome(ok=False, reason="phone_invalid")

    @staticmethod
    def _validate_nid(digits: str) -> ValidationOutcome:
        from redibis.pii.national_id_egypt import validate_egypt_national_id

        result = validate_egypt_national_id(digits)
        if _truthy(result):
            return ValidationOutcome(
                ok=True,
                validator="validate_egypt_national_id",
                entity_type="EG_NATIONAL_ID",
                score=0.95,
            )
        return ValidationOutcome(ok=False, reason="nid_invalid")

    @staticmethod
    def _validate_imei(digits: str) -> ValidationOutcome:
        from redibis.pii.device_id import validate_imei

        result = validate_imei(digits)
        if _truthy(result):
            return ValidationOutcome(
                ok=True,
                validator="validate_imei",
                entity_type="IMEI",
                score=0.93,
            )
        return ValidationOutcome(ok=False, reason="imei_invalid")

    @staticmethod
    def _validate_imsi(digits: str) -> ValidationOutcome:
        from redibis.pii.subscriber_id import validate_imsi

        result = validate_imsi(digits)
        if _truthy(result):
            return ValidationOutcome(
                ok=True,
                validator="validate_imsi",
                entity_type="IMSI",
                score=0.93,
            )
        return ValidationOutcome(ok=False, reason="imsi_invalid")

    @staticmethod
    def _validate_iccid(digits: str) -> ValidationOutcome:
        from redibis.pii.regex_catalog import validate_iccid

        if validate_iccid(digits):
            return ValidationOutcome(
                ok=True,
                validator="validate_iccid",
                entity_type="ICCID",
                score=0.93,
            )
        return ValidationOutcome(ok=False, reason="iccid_invalid")

    @staticmethod
    def _validate_card(digits: str) -> ValidationOutcome:
        from redibis.pii.regex_catalog import validate_luhn

        if 13 <= len(digits) <= 19 and validate_luhn(digits):
            return ValidationOutcome(
                ok=True,
                validator="validate_luhn",
                entity_type="CREDIT_CARD",
                score=0.9,
            )
        return ValidationOutcome(ok=False, reason="card_invalid")


@register_text_validator("email_shape")
class EmailShapeValidator:
    name = "email_shape"
    entity_types = frozenset({"EMAIL_ADDRESS"})

    _RE = re.compile(
        r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
    )

    def validate(
        self,
        canonical: str,
        *,
        ctx: RecognizeContext,
        entity_hint: str = "",
        label: str = "",
    ) -> ValidationOutcome:
        value = (canonical or "").strip()
        if self._RE.match(value):
            return ValidationOutcome(
                ok=True,
                validator="email_shape",
                entity_type="EMAIL_ADDRESS",
                score=0.9,
            )
        return ValidationOutcome(ok=False, reason="email_invalid")


@register_text_validator("age_range")
class AgeRangeValidator:
    name = "age_range"
    entity_types = frozenset({"AGE"})

    def validate(
        self,
        canonical: str,
        *,
        ctx: RecognizeContext,
        entity_hint: str = "",
        label: str = "",
    ) -> ValidationOutcome:
        try:
            age = int(re.sub(r"\D", "", canonical or ""))
        except ValueError:
            return ValidationOutcome(ok=False, reason="age_parse")
        if 1 <= age <= 120:
            blob = _label_blob(label, entity_hint)
            boosted = _has_any(blob, _AGE_HINTS)
            return ValidationOutcome(
                ok=True,
                validator="age_range",
                entity_type="AGE",
                score=0.85 if boosted else 0.7,
            )
        return ValidationOutcome(ok=False, reason="age_oob")


@register_text_validator("labeled_secret")
class LabeledSecretValidator:
    name = "labeled_secret"
    entity_types = frozenset({"PASSWORD_HASH", "API_KEY", "SECRET", "OTP", "SIM_PUK"})

    def validate(
        self,
        canonical: str,
        *,
        ctx: RecognizeContext,
        entity_hint: str = "",
        label: str = "",
    ) -> ValidationOutcome:
        value = (canonical or "").strip()
        if not value or len(value) < 4:
            return ValidationOutcome(ok=False, reason="secret_short")
        hint = (entity_hint or "SECRET").upper()
        if hint not in self.entity_types:
            hint = "SECRET"
        return ValidationOutcome(
            ok=True,
            validator="labeled_secret",
            entity_type=hint,
            score=0.8,
        )


def route_canonical(
    canonical: str,
    *,
    ctx: RecognizeContext,
    entity_hint: str = "",
    label: str = "",
    kind: str = "digits",
) -> ValidationOutcome:
    """Convenience router used by the pipeline."""
    if kind == "email":
        return EmailShapeValidator().validate(
            canonical, ctx=ctx, entity_hint=entity_hint, label=label
        )
    if kind == "age":
        return AgeRangeValidator().validate(
            canonical, ctx=ctx, entity_hint=entity_hint, label=label
        )
    if kind == "secret":
        return LabeledSecretValidator().validate(
            canonical, ctx=ctx, entity_hint=entity_hint, label=label
        )
    return DigitRouterValidator().validate(
        canonical, ctx=ctx, entity_hint=entity_hint, label=label
    )
