"""Built-in locale pack sections (ar-EG default behavior, fr-FR reference)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from redibis.pack.writer import write_pack
from redibis.pii.context_tokens import builtin_context_tokens

PathLike = Union[str, Path]

# Egypt-specific catalog keys removed when a non-EG locale pack is applied.
_EG_PATTERN_KEYS = [
    "msisdn_egypt_national",
    "msisdn_egypt_international",
    "msisdn_egypt_double_zero",
    "msisdn_egypt_any_format",
    "msisdn_egypt_vodafone",
    "msisdn_egypt_etisalat",
    "msisdn_egypt_orange",
    "msisdn_egypt_we",
    "msisdn_egypt_spaced",
    "msisdn_egypt_scan",
    "landline_egypt_cairo",
    "landline_egypt_alexandria",
    "landline_egypt_other_gov",
    "landline_egypt_scan",
    "national_id_egypt",
    "national_id_egypt_strict",
    "national_id_egypt_male",
    "national_id_egypt_female",
    "national_id_egypt_scan",
    "passport_egypt",
    "passport_egypt_scan",
    "iban_egypt",
    "swift_bic_egypt",
    "tax_id_egypt",
    "tax_id_egypt_plain",
    "vehicle_plate_egypt_arabic",
    "vehicle_plate_egypt_latin",
    "imsi_egypt",
    "iccid_egypt",
    "postcode_egypt",
    "scan_national_id_egypt",
    "address_keywords_egypt",
]


def ar_eg_tokens_document() -> dict[str, Any]:
    """Token table matching today's catalog bilingual hints (parity base)."""
    return dict(builtin_context_tokens())


def ar_eg_phone_document() -> dict[str, Any]:
    return {
        "default_regions": ["EG"],
        "msisdn_prefixes": ["010", "011", "012", "015"],
        "geofence": "egypt",
    }


def fr_fr_tokens_document() -> dict[str, Any]:
    return {
        "PHONE_NUMBER": ["téléphone", "portable", "tél", "numéro", "mobile", "phone"],
        "PERSON": ["nom", "prénom", "nom de famille", "name"],
        "ADDRESS": ["adresse", "rue", "ville", "code postal", "cp"],
        "EMAIL_ADDRESS": ["courriel", "email", "mél", "mail"],
        "NATIONAL_ID": ["nir", "sécurité sociale", "insee", "national", "id"],
        "normalize": ["casefold", "accent_fold"],
    }


def fr_fr_phone_document() -> dict[str, Any]:
    return {
        "default_regions": ["FR"],
        "msisdn_prefixes": ["+33", "0033", "06", "07"],
        "geofence": None,
    }


def fr_fr_regex_document() -> dict[str, Any]:
    return {
        "replace_all": False,
        "remove": list(_EG_PATTERN_KEYS),
        "add": {
            "fr_nir": {
                "pattern": r"^[12]\d{2}(0[1-9]|1[0-2])\d{2}\d{3}\d{3}\d{2}$",
                "entity_type": "NATIONAL_ID",
                "recognizer_group": "structured",
                "presidio_score": 0.92,
                "requires_validator": "nir_mod97",
                "context_hints": ["nir", "national", "id", "insee"],
            },
            "fr_mobile": {
                "pattern": r"^(?:\+33|0033|0)[67]\d{8}$",
                "entity_type": "PHONE_NUMBER",
                "recognizer_group": "structured",
                "presidio_score": 0.90,
                "context_hints": ["téléphone", "portable", "mobile", "phone"],
            },
        },
    }


def fr_fr_ner_document() -> dict[str, Any]:
    return {
        "active": None,
        "models": [],
        "phrases": {
            "NATIONAL_ID": "numéro de sécurité sociale",
            "ADDRESS": "adresse postale",
            "PHONE_NUMBER": "numéro de téléphone",
        },
        "gliner_min": 0.45,
    }


def build_ar_eg_pack_sections() -> dict[str, Any]:
    return {
        "locale/tokens.yaml": ar_eg_tokens_document(),
        "locale/phone.yaml": ar_eg_phone_document(),
    }


def build_fr_fr_pack_sections() -> dict[str, Any]:
    return {
        "config/redibis.yaml": {
            "pii": {
                "default_region": "FR",
                "msisdn_prefixes": ["+33", "0033", "06", "07"],
                "use_phonenumbers": True,
                "geo_egypt_geofence": False,
            },
            "masking": {"default_locale": "fr_FR"},
        },
        "locale/tokens.yaml": fr_fr_tokens_document(),
        "locale/phone.yaml": fr_fr_phone_document(),
        "locale/regex.yaml": fr_fr_regex_document(),
        "ner/models.yaml": fr_fr_ner_document(),
    }


def write_fr_fr_pack(path: PathLike, *, version: str = "1.0.0") -> str:
    """Write the reference France telecom locale pack."""
    manifest = {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {
            "id": "fr-FR",
            "version": version,
            "description": "France locale — NIR, FR mobiles, French column tokens.",
            "author": "redibis",
            "base": "redibis-default",
        },
        "requires": {
            "redibis": ">=0.5,<1",
            "registries": {
                "validators": ["nir_mod97", "validate_luhn"],
                "ner_backends": ["gliner"],
                "profilers": ["great_expectations"],
            },
        },
        "contents": {
            "config": True,
            "locale": True,
            "ner": True,
        },
        "mode": "overlay",
    }
    return write_pack(
        path,
        manifest,
        build_fr_fr_pack_sections(),
        readme="# fr-FR locale pack\n\nReference France deployment pack.\n",
    )


def write_ar_eg_pack(path: PathLike, *, version: str = "1.0.0") -> str:
    """Write the built-in Egypt locale pack (token/phone snapshot)."""
    manifest = {
        "apiVersion": "redibis.io/pack/v1",
        "kind": "RedibisPack",
        "metadata": {
            "id": "ar-EG",
            "version": version,
            "description": "Egypt locale — bilingual tokens + EG phone/geo defaults.",
            "author": "redibis",
            "base": "redibis-default",
        },
        "requires": {
            "redibis": ">=0.5,<1",
            "registries": {
                "validators": ["validate_luhn", "validate_egypt_national_id"],
                "ner_backends": ["gliner"],
                "profilers": ["great_expectations"],
            },
        },
        "contents": {"locale": True},
        "mode": "overlay",
    }
    return write_pack(
        path,
        manifest,
        build_ar_eg_pack_sections(),
        readme="# ar-EG locale pack\n\nBuilt-in Egypt defaults (parity with shipped catalog).\n",
    )


def valid_fr_nir(*, sex: str = "1", year: str = "90", month: str = "01") -> str:
    """Build a valid 15-digit NIR for tests (dept 75, commune 056, order 001)."""
    body = f"{sex}{year}{month}75056001"
    key = 97 - (int(body) % 97)
    return f"{body}{key:02d}"
