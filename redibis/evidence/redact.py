"""Dual-copy redaction: exact restricted records vs shareable redacted copies."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from redibis.evidence.sanitize import is_secret_key, sanitize_mapping

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d \-()]{8,}\d)")
_LONG_DIGIT_RE = re.compile(r"\d{9,}")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_AR_NAME_RE = re.compile(r"[\u0600-\u06FF]{2,}(?:\s+[\u0600-\u06FF]{2,})+")
_ADDRESS_RE = re.compile(
    r"\b(?:\d{1,5}\s+)?(?:street|st\.|road|rd\.|avenue|ave\.|blvd|lane|ln\.|"
    r"drive|dr\.|شارع|حي|ميدان)\b[^\n,]{0,40}",
    re.I,
)

_PAYLOAD_FIELDS = (
    "system_prompt",
    "user_prompt",
    "response_text",
    "parsed_result",
    "context",
    "request_params",
    "rai",
    "validation",
    "error",
)

# Free-text LLM fields that must never leave the governed spool.
_OMIT_LLM_KEYS = frozenset({
    "llm_reasoning",
    "system_prompt",
    "user_prompt",
    "response_text",
})
_OMIT_REASONING_PARENTS = frozenset({
    "llm_refiner", "llm", "pii_evidence", "parsed_result",
})


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def redact_text(text: str) -> str:
    out = text or ""
    out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)
    out = _PHONE_RE.sub("[REDACTED_PHONE]", out)
    out = _SSN_RE.sub("[REDACTED_ID]", out)
    out = _IBAN_RE.sub("[REDACTED_ID]", out)
    out = _CC_RE.sub("[REDACTED_ID]", out)
    out = _LONG_DIGIT_RE.sub("[REDACTED_ID]", out)
    out = _IPV4_RE.sub("[REDACTED_IP]", out)
    out = _UUID_RE.sub("[REDACTED_ID]", out)
    out = _ADDRESS_RE.sub("[REDACTED_ADDRESS]", out)
    out = _NAME_RE.sub("[REDACTED_NAME]", out)
    out = _AR_NAME_RE.sub("[REDACTED_NAME]", out)
    return out


def looks_like_residual_pii(text: str) -> bool:
    """True when *text* still appears to hold identifiers after redaction."""
    if not text:
        return False
    sample = text
    if "[REDACTED_" in text:
        sample = _EMAIL_RE.sub("", text)
        sample = _PHONE_RE.sub("", sample)
        sample = _LONG_DIGIT_RE.sub("", sample)
        sample = _UUID_RE.sub("", sample)
        sample = _NAME_RE.sub("", sample)
        sample = _AR_NAME_RE.sub("", sample)
        sample = _ADDRESS_RE.sub("", sample)
        sample = _SSN_RE.sub("", sample)
        sample = _IBAN_RE.sub("", sample)
        sample = _IPV4_RE.sub("", sample)
    return bool(
        _EMAIL_RE.search(sample)
        or _PHONE_RE.search(sample)
        or _LONG_DIGIT_RE.search(sample)
        or _UUID_RE.search(sample)
        or _NAME_RE.search(sample)
        or _AR_NAME_RE.search(sample)
        or _ADDRESS_RE.search(sample)
        or _SSN_RE.search(sample)
        or _IBAN_RE.search(sample)
    )


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {str(k): _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def _obj_has_residual_pii(obj: Any) -> bool:
    if isinstance(obj, str):
        return looks_like_residual_pii(obj)
    if isinstance(obj, dict):
        return any(_obj_has_residual_pii(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_obj_has_residual_pii(v) for v in obj)
    return False


def sanitize_shareable_payload(obj: Any, *, parent_key: str = "") -> Any:
    """Fail-closed sanitizer for LLM-bearing structures on shareable egress.

    Omits free-text LLM fields, secret keys, and residual identifiers. Hashes
    and non-payload metadata are retained.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        parent = str(parent_key or "").lower()
        for key, value in obj.items():
            sk = str(key)
            low = sk.lower()
            if low in _OMIT_LLM_KEYS:
                continue
            if low == "reasoning" and parent in _OMIT_REASONING_PARENTS:
                continue
            if is_secret_key(sk):
                continue
            cleaned = sanitize_shareable_payload(value, parent_key=sk)
            if isinstance(cleaned, str) and looks_like_residual_pii(cleaned):
                continue
            out[sk] = cleaned
        return out
    if isinstance(obj, list):
        return [
            sanitize_shareable_payload(v, parent_key=parent_key) for v in obj
        ]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def shareable_llm_call(record: dict[str, Any]) -> dict[str, Any]:
    """Redact prompts/context/response; omit fields that cannot be de-identified."""
    out = copy.deepcopy(record)
    system = str(out.get("system_prompt") or "")
    user = str(out.get("user_prompt") or "")
    response = str(out.get("response_text") or "")
    hashes = dict(out.get("hashes") or {})
    hashes.setdefault("system_prompt", sha256_text(system)[:16])
    hashes.setdefault("user_prompt", sha256_text(user)[:16])
    hashes.setdefault("response", sha256_text(response)[:16])
    out["hashes"] = hashes
    out["system_prompt"] = redact_text(system)
    out["user_prompt"] = redact_text(user)
    out["response_text"] = redact_text(response)
    out["context"] = _redact_obj(sanitize_mapping(out.get("context") or {}))
    out["config_sanitized"] = sanitize_mapping(out.get("config_sanitized") or {})
    out["request_params"] = _redact_obj(sanitize_mapping(out.get("request_params") or {}))
    out["parsed_result"] = _redact_obj(out.get("parsed_result"))
    if "rai" in out:
        out["rai"] = _redact_obj(sanitize_mapping(out.get("rai") or {}))
    if "validation" in out:
        out["validation"] = _redact_obj(out.get("validation") or {})
    if out.get("error"):
        out["error"] = redact_text(str(out.get("error") or ""))

    omitted = list(out.get("omitted") or [])
    residual = False
    for field in _PAYLOAD_FIELDS:
        if _obj_has_residual_pii(out.get(field)):
            residual = True
            omitted.append({
                "field": field,
                "reason": "de-identification could not be established",
            })
            out[field] = None if field in {
                "parsed_result", "context", "request_params", "rai", "validation",
            } else ""

    out["omitted"] = omitted
    sensitivity = dict(out.get("sensitivity") or {})
    sensitivity.update({
        "contains_raw_pii": False,
        "sample_mode": "omitted" if residual else "redacted",
        "egress": "allow",
        "copy": "shareable",
    })
    if residual:
        sensitivity["note"] = (
            "Unsafe payload fields omitted; hashes and metadata retained."
        )
    out["sensitivity"] = sensitivity
    return out


def restricted_llm_call(record: dict[str, Any]) -> dict[str, Any]:
    """Exact prompts/response with credentials already stripped."""
    out = copy.deepcopy(record)
    out["config_sanitized"] = sanitize_mapping(out.get("config_sanitized") or {})
    out["request_params"] = sanitize_mapping(out.get("request_params") or {})
    sensitivity = dict(out.get("sensitivity") or {})
    sensitivity.update({
        "contains_raw_pii": True,
        "sample_mode": "raw",
        "egress": "deny",
        "copy": "restricted",
        "note": (
            "Local governed spool only. Steward authorization required to read. "
            "Never upload to the runs bucket."
        ),
    })
    out["sensitivity"] = sensitivity
    return out
