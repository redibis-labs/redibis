r"""
Telecom PII Regex Catalog  ·  v2.0
===================================
Rewritten for direct consumption by the PII Detection Pipeline (Layer 3).

WHAT CHANGED FROM v1
--------------------
1.  STRUCTURED CATALOG  — flat dict replaced by typed PatternEntry records.
    Every entry carries: pattern, entity_type, recognizer_group, script,
    requires_validator, context_hints, and collision_group.

2.  RECOGNIZER GROUPS  — two mutually exclusive groups built from this file:
    - STRUCTURED   : anchored (^...$) patterns, used on field-value columns.
    - FREE_TEXT     : \b...\b scan patterns, used on notes/comments/log columns.
    The agent MUST NOT mix groups on the same column.

3.  COLLISION RESOLUTION TABLE  — maps collision_group → list of
    (pattern_key, disambiguating_column_name_tokens) tuples.
    Layer 3 uses this to suppress conflicting recognizers based on GE's
    column_name_score signal before running Presidio.

4.  COVERAGE GAP REGISTRY  — explicit declaration of what is NOT covered
    by regex and is delegated entirely to GLiNER multi-v2.1.
    The agent must NOT attempt to build regex for these gaps.

5.  PRE/POST HOOKS  — companion validators and normalizers are declared
    alongside the patterns that require them, so Layer 3 wires them
    automatically without reading the validator section manually.

CONVENTIONS (unchanged from v1)
---------------------------------
- Anchored (^...$) for FIELD VALIDATION (structured columns).
- \b...\b for FREE TEXT SCANNING (unstructured columns).
- _strict  suffix  → stricter structural validation than the base pattern.
- _scan    suffix  → free-text scan variant of a structured pattern.
- _with_label      → context-aware scanner that requires a keyword label.
- Compile once at module load via compiled_patterns / compiled_scan_patterns.

PRESIDIO INTEGRATION NOTE
--------------------------
Build two lists of PatternRecognizer instances from this catalog:

    from redibis.pii.recognizer_factory import build_recognizers
    structured_recognizers = build_recognizers(group="structured")
    freetext_recognizers   = build_recognizers(group="free_text")
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# PatternEntry — the atomic unit of this catalog
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PatternEntry:
    """
    Single regex entry with full metadata for Presidio wiring.

    Attributes
    ----------
    pattern             : Raw Python regex string (r"...").
    entity_type         : Presidio entity_type label (e.g. "PHONE_NUMBER").
    recognizer_group    : "structured" | "free_text"
                          structured  = anchored (^...$) for single-cell fields.
                          free_text   = \\b..\\b for scanning blobs/notes.
    script              : "latin" | "arabic" | "both" | "numeric" | "hex"
                          Used by Layer 3 to activate Arabic-specific paths.
    requires_validator  : Name of companion validator function, or None.
                          Layer 3 calls catalog_validators[requires_validator]
                          as a post-match confidence check.
    requires_normalizer : Name of companion normalizer function, or None.
                          Layer 1 calls catalog_normalizers[requires_normalizer]
                          during sample cleanup before Presidio runs.
    context_hints       : Column-name tokens that boost confidence when present.
    collision_group     : Key into COLLISION_RESOLUTION_TABLE, or None.
                          Patterns sharing a collision_group are structurally
                          ambiguous — context_hints is the only disambiguator.
    presidio_score      : Default Presidio recognition_metadata score (0–1).
                          Lower for patterns with high structural collision risk.
    validated_score     : Score to promote to when ``requires_validator`` passes.
                          ``None`` keeps ``presidio_score``; a float raises it.
                          Validators can drop (return 0.0) or promote — never
                          silently leave a below-floor score after a pass.
    unvalidated_reason  : Why this entry may score at/above ``presidio_min`` with
                          no validator. Required by audit tests for any such entry.
                          Legitimate only when the pattern's shape is itself strong
                          evidence (API key prefixes, hash formats, RFC emails/URLs).
    active              : False = pattern exists for reference but is excluded
                          from recognizer build (e.g. overly broad patterns).
    """
    pattern:             str
    entity_type:         str
    recognizer_group:    str                          # "structured" | "free_text"
    script:              str   = "latin"              # "latin"|"arabic"|"both"|"numeric"|"hex"
    requires_validator:  Optional[str] = None
    requires_normalizer: Optional[str] = None
    context_hints:       tuple[str, ...] = ()
    collision_group:     Optional[str] = None
    presidio_score:      float = 0.85
    validated_score:     Optional[float] = None
    unvalidated_reason:  Optional[str] = None
    active:              bool  = True


# ─────────────────────────────────────────────────────────────────────────────
# CATALOG
# ─────────────────────────────────────────────────────────────────────────────

CATALOG: dict[str, PatternEntry] = {

    # =========================================================================
    # 1. EGYPT-SPECIFIC IDENTIFIERS
    # =========================================================================

    # ── Egyptian Mobile (MSISDN) ──────────────────────────────────────────────

    "msisdn_egypt_national": PatternEntry(
        pattern          = r"^01[0125]\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","msisdn","tel","هاتف","رقم","جوال"),
        collision_group  = None,
        presidio_score   = 0.90,
        unvalidated_reason = "required country/prefix is structural evidence",
    ),
    "msisdn_egypt_international": PatternEntry(
        pattern          = r"^\+201[0125]\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","msisdn","tel","هاتف","رقم"),
        presidio_score   = 0.92,
        unvalidated_reason = "required country/prefix is structural evidence",
    ),
    "msisdn_egypt_double_zero": PatternEntry(
        pattern          = r"^0020?1[0125]\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","msisdn","هاتف"),
        presidio_score   = 0.88,
        unvalidated_reason = "required country/prefix is structural evidence",
    ),
    "msisdn_egypt_any_format": PatternEntry(
        pattern          = r"^(?:\+20|0020|0)?1[0125]\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","contact","هاتف","رقم"),
        presidio_score   = 0.6,
    ),
    "msisdn_egypt_vodafone": PatternEntry(
        pattern          = r"^(?:\+20|0)?10\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","vodafone","هاتف"),
        collision_group  = "vodafone_cash_vs_msisdn",
        presidio_score   = 0.60,
    ),
    "msisdn_egypt_etisalat": PatternEntry(
        pattern          = r"^(?:\+20|0)?11\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","etisalat","eand","هاتف"),
        presidio_score   = 0.60,
    ),
    "msisdn_egypt_orange": PatternEntry(
        pattern          = r"^(?:\+20|0)?12\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","orange","هاتف"),
        presidio_score   = 0.60,
    ),
    "msisdn_egypt_we": PatternEntry(
        pattern          = r"^(?:\+20|0)?15\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","we","telecom","هاتف"),
        presidio_score   = 0.60,
    ),
    "msisdn_egypt_spaced": PatternEntry(
        pattern          = r"^(?:\+20|0)?\s?1[0125]\s?\d{4}\s?\d{4}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("phone","mobile","هاتف"),
        presidio_score   = 0.6,
    ),
    "msisdn_egypt_scan": PatternEntry(
        pattern          = r"\b(?:\+20|0020|0)?1[0125]\d{8}\b",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("notes","comment","description","remarks","ملاحظات"),
        presidio_score   = 0.6,
    ),

    # ── Egyptian Landline ─────────────────────────────────────────────────────

    "landline_egypt_cairo": PatternEntry(
        pattern          = r"^(?:\+20|0)?2\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("phone","landline","tel","هاتف","تليفون"),
        presidio_score   = 0.55,
    ),
    "landline_egypt_alexandria": PatternEntry(
        pattern          = r"^(?:\+20|0)?3\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("phone","landline","tel","هاتف"),
        presidio_score   = 0.55,
    ),
    "landline_egypt_other_gov": PatternEntry(
        pattern          = r"^(?:\+20|0)?(?:13|40|45|46|47|48|50|55|57|62|64|65|66|68|69|82|84|86|88|92|93|95|96|97)\d{7}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("phone","landline","tel","هاتف"),
        presidio_score   = 0.55,
    ),
    "landline_egypt_scan": PatternEntry(
        pattern          = r"\b(?:\+?20|0020|0)?(?:[23]\d{8}|(?:13|40|45|46|47|48|50|55|57|62|64|65|66|68|69|82|84|86|88|92|93|95|96|97)\d{7}|554\d{6})\b",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "free_text",
        script           = "numeric",
        context_hints    = ("notes","comment","remarks","ملاحظات"),
        presidio_score   = 0.75,
    ),

    # ── Egyptian National ID ──────────────────────────────────────────────────

    "national_id_egypt": PatternEntry(
        pattern          = r"^[23]\d{13}$",
        entity_type      = "EG_NATIONAL_ID",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_egypt_national_id",
        context_hints    = ("national","id","nid","رقم_قومي","هوية","الرقم_القومي"),
        collision_group  = "fourteen_digit_numeric",
        # Below default presidio_min (0.80): evidence only — never carries a column alone.
        # Structural validity + column nid_valid_rate gate live in national_id_egypt.py.
        presidio_score   = 0.50,
    ),
    "national_id_egypt_strict": PatternEntry(
        pattern            = r"^[23]\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:0[1-4]|1[1-9]|2[1-9]|3[1-5]|88)\d{5}$",
        entity_type        = "EG_NATIONAL_ID",
        recognizer_group   = "structured",
        script             = "numeric",
        requires_validator = "validate_egypt_national_id",
        validated_score    = 0.97,
        context_hints      = ("national","id","nid","رقم_قومي","هوية","الرقم_القومي"),
        collision_group    = "fourteen_digit_numeric",
        # Base score below IMSI's post-validation MAX; validated_score promotes.
        # Presidio coerce any truthy validate_result to MAX_SCORE (1.0).
        presidio_score     = 0.92,
    ),
    "national_id_egypt_male": PatternEntry(
        pattern            = r"^[23]\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:0[1-4]|1[1-9]|2[1-9]|3[1-5]|88)\d{3}[13579]\d$",
        entity_type        = "EG_NATIONAL_ID",
        recognizer_group   = "structured",
        script             = "numeric",
        requires_validator = "validate_egypt_national_id",
        validated_score    = 0.97,
        context_hints      = ("national","id","gender","male","رقم_قومي","ذكر"),
        collision_group    = "fourteen_digit_numeric",
        presidio_score     = 0.93,
    ),
    "national_id_egypt_female": PatternEntry(
        pattern            = r"^[23]\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:0[1-4]|1[1-9]|2[1-9]|3[1-5]|88)\d{3}[02468]\d$",
        entity_type        = "EG_NATIONAL_ID",
        recognizer_group   = "structured",
        script             = "numeric",
        requires_validator = "validate_egypt_national_id",
        validated_score    = 0.97,
        context_hints      = ("national","id","gender","female","رقم_قومي","أنثى"),
        collision_group    = "fourteen_digit_numeric",
        presidio_score     = 0.93,
    ),
    "national_id_egypt_scan": PatternEntry(
        pattern          = r"\b[23]\d{13}\b",
        entity_type      = "EG_NATIONAL_ID",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_validator = "validate_egypt_national_id",
        context_hints    = ("notes","comment","رقم_قومي","هوية"),
        collision_group  = "fourteen_digit_numeric",
        presidio_score   = 0.70,
    ),

    # ── Egyptian Passport ─────────────────────────────────────────────────────

    "passport_egypt": PatternEntry(
        pattern          = r"^[A-Z]\d{8}$",
        entity_type      = "PASSPORT",
        recognizer_group = "structured",
        script           = "latin",
        context_hints    = ("passport","travel","جواز","سفر"),
        collision_group  = "passport_alpha_eight_digits",
        presidio_score   = 0.7,
    ),
    "passport_egypt_scan": PatternEntry(
        pattern          = r"\b[A-Z]\d{8}\b",
        entity_type      = "PASSPORT",
        recognizer_group = "free_text",
        script           = "latin",
        context_hints    = ("notes","passport","travel","جواز"),
        collision_group  = "passport_alpha_eight_digits",
        presidio_score   = 0.65,
    ),

    # ── Egyptian Banking & Fintech ────────────────────────────────────────────

    "iban_egypt": PatternEntry(
        pattern            = r"^EG\d{2}[A-Z0-9]{25}$",
        entity_type        = "IBAN_CODE",
        recognizer_group   = "structured",
        script             = "latin",
        requires_validator = "validate_iban",
        context_hints      = ("iban","bank","account","حساب","بنك"),
        presidio_score     = 0.60,
        validated_score    = 0.97,
    ),
    "swift_bic_egypt": PatternEntry(
        pattern            = r"^[A-Z]{4}EG[A-Z0-9]{2}(?:[A-Z0-9]{3})?$",
        entity_type        = "SWIFT_BIC",
        recognizer_group   = "structured",
        script             = "latin",
        requires_validator = "validate_swift_bic",
        context_hints      = ("swift","bic","bank","transfer","بنك"),
        presidio_score     = 0.55,
        validated_score    = 0.95,
    ),
    "meeza_card": PatternEntry(
        pattern            = r"^627033\d{10}$",
        entity_type        = "CREDIT_CARD",
        recognizer_group   = "structured",
        script             = "numeric",
        requires_validator = "validate_luhn",
        validated_score    = 0.95,
        context_hints      = ("card","meeza","payment","بطاقة"),
        collision_group    = "sixteen_digit_numeric",
        # Below floor until Luhn promotes — same PAN-vs-IMEISV posture as Visa.
        presidio_score     = 0.70,
    ),
    "instapay_handle": PatternEntry(
        pattern            = r"^[A-Za-z0-9._-]{3,30}@instapay$",
        entity_type        = "EMAIL_ADDRESS",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("instapay","payment","wallet","محفظة"),
        presidio_score     = 0.90,
        unvalidated_reason = "shape is the evidence",
    ),
    "vodafone_cash_account": PatternEntry(
        pattern          = r"^(?:\+20|0)?10\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        requires_normalizer = "normalize_msisdn_egypt",
        context_hints    = ("wallet","cash","vodafone","محفظة","فودافون_كاش"),
        collision_group  = "vodafone_cash_vs_msisdn",
        presidio_score   = 0.6,
        # NOTE: structurally identical to msisdn_egypt_vodafone.
        # Disambiguation: column name containing "wallet","cash" → this entry.
        # Column name containing "phone","mobile","tel" → msisdn_egypt_vodafone.
    ),

    # ── Egyptian Tax / Commercial ─────────────────────────────────────────────

    "tax_id_egypt": PatternEntry(
        pattern          = r"^\d{3}-\d{3}-\d{3}$",
        entity_type      = "EG_TAX_ID",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("tax","tin","commercial","ضريبي","ضريبة"),
        collision_group  = "nine_digit_numeric",
        presidio_score   = 0.60,
    ),
    "tax_id_egypt_plain": PatternEntry(
        pattern          = r"^\d{9}$",
        entity_type      = "EG_TAX_ID",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("tax","tin","ضريبي"),
        collision_group  = "nine_digit_numeric",
        presidio_score   = 0.60,  # lowered — high collision risk
    ),

    # ── Egyptian Utility Accounts ─────────────────────────────────────────────
    # NOTE: bare digit patterns (electricity/water/gas) are collision-prone.
    # Prefer the _with_label variants for free-text columns.
    # For structured columns, use column_name_hint disambiguation only.

    "electricity_account_with_label": PatternEntry(
        pattern            = r"(?i)\b(?:elec(?:tricity)?|كهرباء)\s*(?:acc(?:ount)?|no|number|#)?\s*[:#\-]*\s*\d{8,16}\b",
        entity_type        = "UTILITY_ACCOUNT",
        recognizer_group   = "free_text",
        script             = "both",
        context_hints      = ("notes","comment","electricity","كهرباء"),
        presidio_score     = 0.85,
        unvalidated_reason = "shape is the evidence",
    ),
    "water_account_with_label": PatternEntry(
        pattern            = r"(?i)\b(?:water|wtr|مياه)\s*(?:acc(?:ount)?|no|number|#)?\s*[:#\-]*\s*\d{8,16}\b",
        entity_type        = "UTILITY_ACCOUNT",
        recognizer_group   = "free_text",
        script             = "both",
        context_hints      = ("notes","comment","water","مياه"),
        presidio_score     = 0.85,
        unvalidated_reason = "shape is the evidence",
    ),
    "gas_account_with_label": PatternEntry(
        pattern            = r"(?i)\b(?:gas|غاز)\s*(?:acc(?:ount)?|no|number|#)?\s*[:#\-]*\s*\d{8,16}\b",
        entity_type        = "UTILITY_ACCOUNT",
        recognizer_group   = "free_text",
        script             = "both",
        context_hints      = ("notes","comment","gas","غاز"),
        presidio_score     = 0.85,
        unvalidated_reason = "shape is the evidence",
    ),

    # ── Egyptian Vehicles ─────────────────────────────────────────────────────

    "vehicle_plate_egypt_arabic": PatternEntry(
        pattern          = r"^[\u0621-\u064A]{1,3}\s?\d{1,4}$",
        entity_type      = "EG_VEHICLE_PLATE",
        recognizer_group = "structured",
        script           = "arabic",
        context_hints    = ("plate","vehicle","car","لوحة","سيارة"),
        presidio_score   = 0.45,
    ),
    "vehicle_plate_egypt_latin": PatternEntry(
        pattern          = r"^[A-Z]{1,3}\s?\d{1,4}$",
        entity_type      = "EG_VEHICLE_PLATE",
        recognizer_group = "structured",
        script           = "latin",
        context_hints    = ("plate","vehicle","car","لوحة"),
        presidio_score   = 0.45,
    ),

    # =========================================================================
    # 2. SUBSCRIBER & NETWORK IDENTIFIERS
    # =========================================================================

    # ── SIM / Subscriber ──────────────────────────────────────────────────────

    "imsi_egypt": PatternEntry(
        pattern          = r"^602(?:01|02|03|04)\d{10}$",
        entity_type      = "IMSI",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("imsi","subscriber","sim","مشترك"),
        presidio_score   = 0.95,
        unvalidated_reason = "MCC+MNC prefix is structural evidence",
    ),
    "imsi_strict": PatternEntry(
        pattern          = r"^602\d{12}$",
        entity_type      = "IMSI",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_imsi",
        validated_score  = 0.92,
        context_hints    = ("imsi","subscriber","sim"),
        collision_group  = "fifteen_digit_numeric",
        presidio_score   = 0.55,
    ),
    "imsi_generic": PatternEntry(
        pattern          = r"^\d{14,15}$",
        entity_type      = "IMSI",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_imsi",
        validated_score  = 0.90,
        context_hints    = ("imsi","subscriber","sim","msin","mcc","mnc","مشترك"),
        collision_group  = "fifteen_digit_numeric",
        # Evidence only until MCC validator promotes — roaming FNs fixed by validator.
        presidio_score   = 0.40,
    ),
    "iccid_egypt": PatternEntry(
        pattern          = r"^8920(?:01|02|03|04)\d{14,16}$",
        entity_type      = "ICCID",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_iccid",
        context_hints    = ("iccid","sim","card","شريحة"),
        presidio_score   = 0.95,
    ),
    "iccid": PatternEntry(
        pattern          = r"^89\d{16,20}$",
        entity_type      = "ICCID",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_iccid",
        context_hints    = ("iccid","sim","card","شريحة"),
        presidio_score   = 0.88,
    ),

    # ── Device Identifiers ────────────────────────────────────────────────────

    "imei": PatternEntry(
        pattern          = r"^\d{15}$",
        entity_type      = "IMEI",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_imei",
        validated_score  = 0.95,
        context_hints    = ("imei","device","handset","equipment","جهاز"),
        collision_group  = "fifteen_digit_numeric",
        # Bare 15-digit evidence only; Luhn+RBI validator promotes to validated_score.
        presidio_score   = 0.60,
    ),
    "imei_with_dashes": PatternEntry(
        pattern          = r"^\d{2}-\d{6}-\d{6}-\d$",
        entity_type      = "IMEI",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_imei",
        validated_score  = 0.95,
        context_hints    = ("imei","device","handset","جهاز"),
        presidio_score   = 0.90,
    ),
    "imeisv": PatternEntry(
        pattern          = r"^\d{16}$",
        entity_type      = "IMEI",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_imeisv",
        validated_score  = 0.88,
        context_hints    = ("imeisv","device","software","جهاز"),
        collision_group  = "sixteen_digit_numeric",
        presidio_score   = 0.55,
    ),

    # ── Network Addressing ────────────────────────────────────────────────────

    "ipv4_address": PatternEntry(
        pattern            = r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)$",
        entity_type        = "IP_ADDRESS",
        recognizer_group   = "structured",
        script             = "numeric",
        context_hints      = ("ip","address","source","destination","src","dst"),
        presidio_score     = 0.90,
        unvalidated_reason = "shape is the evidence",
    ),
    "ipv4_private": PatternEntry(
        pattern          = r"^(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})$",
        entity_type      = "IP_ADDRESS",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("ip","internal","private","src","dst"),
        presidio_score   = 0.70,  # private IPs are lower PII risk
    ),
    "ipv6_address": PatternEntry(
        pattern            = r"^(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}$",
        entity_type        = "IP_ADDRESS",
        recognizer_group   = "structured",
        script             = "hex",
        context_hints      = ("ip","ipv6","address","src","dst"),
        presidio_score     = 0.90,
        unvalidated_reason = "shape is the evidence",
    ),
    "mac_address_colon": PatternEntry(
        pattern            = r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$",
        entity_type        = "MAC_ADDRESS",
        recognizer_group   = "structured",
        script             = "hex",
        requires_validator = "validate_mac_address",
        context_hints      = ("mac","address","device","hardware","جهاز"),
        presidio_score     = 0.60,
        validated_score    = 0.92,
    ),
    "scan_ipv4": PatternEntry(
        pattern            = r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        entity_type        = "IP_ADDRESS",
        recognizer_group   = "free_text",
        script             = "numeric",
        context_hints      = ("log","notes","comment","event"),
        presidio_score     = 0.82,
        unvalidated_reason = "shape is the evidence",
    ),
    "scan_mac_address": PatternEntry(
        pattern          = r"\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b",
        entity_type      = "MAC_ADDRESS",
        recognizer_group = "free_text",
        script           = "hex",
        context_hints    = ("log","notes","event","device"),
        presidio_score   = 0.82,
        unvalidated_reason = "MAC colon/dash shape is the evidence",
    ),

    # ── Geolocation ───────────────────────────────────────────────────────────

    "gps_pair": PatternEntry(
        pattern          = r"^[-+]?\d{1,3}\.\d+,\s?[-+]?\d{1,3}\.\d+$",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("gps","location","coordinate","lat","lon","موقع"),
        presidio_score   = 0.88,
        unvalidated_reason = "coordinate shape is the evidence",
    ),
    "gps_latitude": PatternEntry(
        # Require ≥4 decimal places — integers 0–90 must never match (G1).
        pattern          = r"^[-+]?(?:[1-8]?\d|90)\.\d{4,}$",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("lat","latitude","خط_عرض"),
        # Evidence only — below presidio_min; partner + corroboration decide.
        presidio_score   = 0.45,
    ),
    "gps_longitude": PatternEntry(
        pattern          = r"^[-+]?(?:180|1[0-7]\d|[1-9]?\d)\.\d{4,}$",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("lon","longitude","lng","خط_طول"),
        presidio_score   = 0.45,
    ),
    "gps_scaled_int": PatternEntry(
        pattern          = r"^[-+]?\d{7,9}$",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_scaled_coordinate",
        validated_score  = 0.70,
        context_hints    = ("lat","lon","gps","coord","خط_عرض","خط_طول"),
        # Evidence only — geo_engine scale + partner + geofence decide.
        presidio_score   = 0.35,
    ),
    "wkt_point": PatternEntry(
        pattern          = r"^POINT\s*\(\s*[-+]?\d+(?:\.\d+)?\s+[-+]?\d+(?:\.\d+)?\s*\)$",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "latin",
        context_hints    = ("gps","location","wkt","geom","geometry","موقع"),
        presidio_score   = 0.90,
        unvalidated_reason = "coordinate shape is the evidence",
    ),
    "gps_dms": PatternEntry(
        pattern          = r"""^\d{1,3}°\s*\d{1,2}'\s*\d{1,2}(?:\.\d+)?"\s*[NSEW]$""",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "latin",
        context_hints    = ("gps","location","dms","coordinate","موقع"),
        presidio_score   = 0.92,
        unvalidated_reason = "coordinate shape is the evidence",
    ),

    # =========================================================================
    # 3. DEMOGRAPHICS & CRM
    # =========================================================================

    # ── Email ─────────────────────────────────────────────────────────────────

    "url": PatternEntry(
        pattern            = r"^(?:https?|ftp)://[^\s/$.?#].[^\s]*$",
        entity_type        = "URL",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("url", "link", "website", "social", "profile", "oauth", "linkedin", "facebook", "twitter", "instagram"),
        presidio_score     = 0.85,
        unvalidated_reason = "shape is the evidence",
    ),
    "email_address": PatternEntry(
        pattern            = r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$",
        entity_type        = "EMAIL_ADDRESS",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("email","mail","بريد","إيميل"),
        presidio_score     = 0.92,
        unvalidated_reason = "shape is the evidence",
    ),
    "scan_email": PatternEntry(
        pattern            = r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        entity_type        = "EMAIL_ADDRESS",
        recognizer_group   = "free_text",
        script             = "latin",
        context_hints      = ("notes","comment","بريد","ملاحظات"),
        presidio_score     = 0.85,
        unvalidated_reason = "shape is the evidence",
    ),

    # ── Names (regex only — structural hint, NOT semantic detection) ──────────
    # IMPORTANT: These patterns have very high false-positive rates.
    # They exist as SUPPLEMENTARY signals only.
    # GLiNER multi-v2.1 is the primary detector for PERSON entities.
    # Set presidio_score low; the GLiNER layer will override with a higher score.

    "person_name_arabic": PatternEntry(
        pattern          = r"^[\u0621-\u064A]+(?:\s[\u0621-\u064A]+){1,4}$",
        entity_type      = "PERSON",
        recognizer_group = "structured",
        script           = "arabic",
        # Specific person-qualified hints only. Bare "name"/"اسم" removed: they
        # are substrings of non-person columns (product_name, file_name, اسم_المنتج)
        # and the boost would mis-flag those as PERSON. GLiNER owns name detection.
        context_hints    = ("اسم_العميل","الاسم_الكامل","اسم_كامل","customer_name","client_name","full_name"),
        presidio_score   = 0.40,  # intentionally low — GLiNER owns this
    ),
    "person_name_latin": PatternEntry(
        pattern          = r"^[A-Z][a-z]+(?:[\s\-'][A-Z][a-z]+){1,4}$",
        entity_type      = "PERSON",
        recognizer_group = "structured",
        script           = "latin",
        # Specific person-qualified hints only (see person_name_arabic note):
        # bare "name"/"customer"/"first"/"last"/"full" removed to avoid matching
        # product_name / file_name / first_seen / last_login etc.
        context_hints    = ("first_name","last_name","full_name","fname","lname","firstname","lastname","fullname","customer_name","client_name","holder_name","contact_name"),
        presidio_score   = 0.40,  # intentionally low — GLiNER owns this
    ),

    # ── Dates / Demographics ──────────────────────────────────────────────────

    # NOTE: these regexes match ANY date, not only a date of birth. A bare date
    # (created_at, order_date, …) is NOT PII. The base score is deliberately set
    # BELOW the regex confidence floor (≈0.80), so a value-only match is not
    # confirmed. It only becomes PII when the COLUMN NAME carries a birth
    # context_hint, which the detector's context boost lifts above the floor.
    "date_of_birth_ymd": PatternEntry(
        pattern          = r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$",
        entity_type      = "DATE_TIME",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("dob","birth","born","birthdate","birthday","dateofbirth","تاريخ_ميلاد","ميلاد"),
        presidio_score   = 0.50,   # below floor: bare date not PII; column-name context boosts it
    ),
    "date_of_birth_dmy": PatternEntry(
        pattern          = r"^(?:0[1-9]|[12]\d|3[01])/(?:0[1-9]|1[0-2])/\d{4}$",
        entity_type      = "DATE_TIME",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("dob","birth","born","birthdate","birthday","dateofbirth","تاريخ_ميلاد","ميلاد"),
        presidio_score   = 0.50,   # below floor: bare date not PII; column-name context boosts it
    ),
    "gender_indicator": PatternEntry(
        pattern          = r"(?i)^(?:M|F|X|Male|Female|Other|O|Unknown|U)$",
        entity_type      = "GENDER",
        recognizer_group = "structured",
        script           = "latin",
        context_hints    = ("gender","sex","نوع","جنس"),
        presidio_score   = 0.75,
    ),
    "postcode_egypt": PatternEntry(
        pattern          = r"^\d{5}$",
        entity_type      = "LOCATION",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("postcode","zip","postal","postal_code","رمز_بريدي","بريدي"),
        # No collision_group — singleton five_digit_numeric suppressed nothing.
        presidio_score   = 0.35,
    ),

    # ── Gulf & MENA Government IDs ────────────────────────────────────────────

    "national_id_uae": PatternEntry(
        pattern            = r"^\d{3}-\d{4}-\d{7}-\d$",
        entity_type        = "NATIONAL_ID",
        recognizer_group   = "structured",
        script             = "numeric",
        requires_validator = "validate_uae_national_id",
        context_hints      = ("national","id","uae","emirates","هوية"),
        presidio_score     = 0.50,
        validated_score    = 0.95,
    ),
    "national_id_ksa": PatternEntry(
        pattern            = r"^[12]\d{9}$",
        entity_type        = "NATIONAL_ID",
        recognizer_group   = "structured",
        script             = "numeric",
        requires_validator = "validate_ksa_national_id",
        context_hints      = ("national","id","iqama","saudi","هوية","اقامة"),
        collision_group    = "ten_digit_numeric",
        presidio_score     = 0.45,
        validated_score    = 0.92,
    ),
    "national_id_jordan": PatternEntry(
        pattern          = r"^\d{10}$",
        entity_type      = "NATIONAL_ID",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("national","id","jordan","الرقم_الوطني"),
        collision_group  = "ten_digit_numeric",
        presidio_score   = 0.30,
    ),
    "national_id_morocco": PatternEntry(
        pattern          = r"^[A-Z]{1,2}\d{6}$",
        entity_type      = "NATIONAL_ID",
        recognizer_group = "structured",
        script           = "latin",
        context_hints    = ("national","id","cin","morocco","هوية"),
        presidio_score   = 0.55,
    ),

    # ── Gulf MSISDNs ──────────────────────────────────────────────────────────

    "msisdn_ksa": PatternEntry(
        pattern          = r"^(?:\+966|00966|0)?5\d{8}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("phone","mobile","ksa","saudi","هاتف"),
        presidio_score   = 0.60,
    ),
    "msisdn_uae": PatternEntry(
        pattern          = r"^(?:\+971|00971|0)?5[024568]\d{7}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("phone","mobile","uae","emirates","هاتف"),
        presidio_score   = 0.60,
    ),
    "msisdn_e164": PatternEntry(
        pattern          = r"^\+[1-9]\d{6,14}$",
        entity_type      = "PHONE_NUMBER",
        recognizer_group = "structured",
        script           = "numeric",
        context_hints    = ("phone","mobile","international","هاتف"),
        presidio_score   = 0.85,
        unvalidated_reason = "required country/prefix is structural evidence",
    ),

    # =========================================================================
    # 4. BILLING & FINANCIAL
    # =========================================================================

    "credit_card_visa": PatternEntry(
        pattern          = r"^4\d{12}(?:\d{3})?(?:\d{3})?$",
        entity_type      = "CREDIT_CARD",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_luhn",
        validated_score  = 0.95,
        context_hints    = ("card","visa","credit","payment","بطاقة"),
        collision_group  = "sixteen_digit_numeric",
        presidio_score   = 0.70,
    ),
    "credit_card_mastercard": PatternEntry(
        pattern          = r"^(?:5[1-5]\d{2}|222[1-9]|22[3-9]\d|2[3-6]\d{2}|27[01]\d|2720)\d{12}$",
        entity_type      = "CREDIT_CARD",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_luhn",
        validated_score  = 0.95,
        context_hints    = ("card","mastercard","credit","payment","بطاقة"),
        collision_group  = "sixteen_digit_numeric",
        presidio_score   = 0.70,
    ),
    "credit_card_with_spaces": PatternEntry(
        pattern          = r"^\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4}$",
        entity_type      = "CREDIT_CARD",
        recognizer_group = "structured",
        script           = "numeric",
        requires_validator = "validate_luhn",
        validated_score  = 0.95,
        context_hints    = ("card","credit","payment","بطاقة"),
        collision_group  = "sixteen_digit_numeric",
        presidio_score   = 0.70,
    ),
    "scan_credit_card": PatternEntry(
        pattern          = r"\b(?:\d{4}[\s\-]?){3}\d{4}\b",
        entity_type      = "CREDIT_CARD",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_validator = "validate_luhn",
        context_hints    = ("notes","comment","payment","بطاقة"),
        presidio_score   = 0.75,
    ),
    "iban_generic": PatternEntry(
        pattern            = r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$",
        entity_type        = "IBAN_CODE",
        recognizer_group   = "structured",
        script             = "latin",
        requires_validator = "validate_iban",
        context_hints      = ("iban","bank","account","حساب","بنك","تحويل"),
        presidio_score     = 0.45,
        validated_score    = 0.93,
    ),
    "scan_iban": PatternEntry(
        pattern          = r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
        entity_type      = "IBAN_CODE",
        recognizer_group = "free_text",
        script           = "latin",
        context_hints    = ("notes","comment","transfer","بنك"),
        presidio_score   = 0.78,
    ),

    # ── Crypto (high-sensitivity financial PII) ───────────────────────────────

    "crypto_wallet_btc_legacy": PatternEntry(
        pattern            = r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$",
        entity_type        = "CRYPTO_WALLET",
        recognizer_group   = "structured",
        script             = "latin",
        requires_validator = "validate_btc_address",
        context_hints      = ("wallet","btc","bitcoin","crypto","محفظة"),
        presidio_score     = 0.55,
        validated_score    = 0.95,
    ),
    "crypto_wallet_eth": PatternEntry(
        pattern            = r"^0x[a-fA-F0-9]{40}$",
        entity_type        = "CRYPTO_WALLET",
        recognizer_group   = "structured",
        script             = "hex",
        context_hints      = ("wallet","eth","ethereum","crypto","محفظة"),
        presidio_score     = 0.90,
        unvalidated_reason = "EIP-55 needs keccak256; the 0x+40hex shape is already specific",
    ),

    # =========================================================================
    # 5. DIGITAL IDENTITY & SECURITY
    # =========================================================================

    # ── API Keys & Tokens ─────────────────────────────────────────────────────

    "api_key_aws_access": PatternEntry(
        pattern            = r"^AKIA[0-9A-Z]{16}$",
        entity_type        = "API_KEY",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("key","api","aws","access","token","مفتاح"),
        presidio_score     = 0.97,
        unvalidated_reason = "shape is the evidence",
    ),
    "api_key_google": PatternEntry(
        pattern            = r"^AIza[0-9A-Za-z\-_]{35}$",
        entity_type        = "API_KEY",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("key","api","google","token","مفتاح"),
        presidio_score     = 0.97,
        unvalidated_reason = "shape is the evidence",
    ),
    "api_key_github_pat": PatternEntry(
        pattern            = r"^ghp_[A-Za-z0-9]{36}$",
        entity_type        = "API_KEY",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("key","token","github","pat","مفتاح"),
        presidio_score     = 0.97,
        unvalidated_reason = "shape is the evidence",
    ),
    "api_key_anthropic": PatternEntry(
        pattern            = r"^sk-ant-[A-Za-z0-9\-_]{32,}$",
        entity_type        = "API_KEY",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("key","token","api","anthropic","مفتاح"),
        presidio_score     = 0.98,
        unvalidated_reason = "shape is the evidence",
    ),
    "jwt_token": PatternEntry(
        pattern            = r"^eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*$",
        entity_type        = "JWT_TOKEN",
        recognizer_group   = "structured",
        script             = "latin",
        requires_validator = "validate_jwt",
        context_hints      = ("jwt","token","bearer","auth","access_token","id_token"),
        presidio_score     = 0.70,
        validated_score    = 0.95,
    ),
    "scan_jwt": PatternEntry(
        pattern          = r"\beyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*\b",
        entity_type      = "JWT_TOKEN",
        recognizer_group = "free_text",
        script           = "latin",
        context_hints    = ("log","notes","token","auth"),
        presidio_score   = 0.88,
        unvalidated_reason = "eyJ header prefix is structural evidence",
    ),

    # ── Hashes (context-dependent — NEVER flag without column_name hint) ──────
    # CRITICAL: These patterns are structurally identical to many non-PII values.
    # active=False for the ambiguous ones; only structured-prefix variants active.
    # Layer 3 MUST require at least one context_hint match before firing these.

    "hash_bcrypt": PatternEntry(
        pattern            = r"^\$2[abxy]?\$\d{2}\$[./A-Za-z0-9]{53}$",
        entity_type        = "PASSWORD_HASH",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("password","hash","pwd","كلمة_سر","مشفر"),
        presidio_score     = 0.95,
        unvalidated_reason = "shape is the evidence",
    ),
    "hash_argon2": PatternEntry(
        pattern            = r"^\$argon2(?:i|d|id)\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$",
        entity_type        = "PASSWORD_HASH",
        recognizer_group   = "structured",
        script             = "latin",
        context_hints      = ("password","hash","pwd","كلمة_سر"),
        presidio_score     = 0.96,
        unvalidated_reason = "shape is the evidence",
    ),
    "hash_md5_like": PatternEntry(
        pattern          = r"^[a-f0-9]{32}$",
        entity_type      = "PASSWORD_HASH",
        recognizer_group = "structured",
        script           = "hex",
        # Password-context hints ONLY. Generic "hash"/"checksum" removed: a bare
        # 32-hex value is just as likely a content checksum / dedup key, not a
        # credential. Only treat as PASSWORD_HASH on a password-named column.
        context_hints    = ("password","pwd","كلمة_سر"),
        # No collision_group — singleton hex_32_chars suppressed nothing.
        presidio_score   = 0.50,   # very low — collides with api_key_hex, UUIDs
        active           = False,  # disabled by default; enable via column hint only
    ),
    "hash_sha256_like": PatternEntry(
        pattern          = r"^[a-f0-9]{64}$",
        entity_type      = "PASSWORD_HASH",
        recognizer_group = "structured",
        script           = "hex",
        # Password-context hints ONLY (see hash_md5_like note).
        context_hints    = ("password","pwd","كلمة_سر"),
        # No collision_group — singleton hex_64_chars suppressed nothing.
        presidio_score   = 0.55,
        active           = False,  # disabled by default
    ),

    # ── OTP / 2FA ─────────────────────────────────────────────────────────────

    "otp_with_label": PatternEntry(
        pattern            = r"(?i)\b(?:otp|one[\-\s]?time\s*password|verification\s*code|auth\s*code|كود|رمز)\s*(?:is|:|=|\s)+\s*\d{4,8}\b",
        entity_type        = "OTP",
        recognizer_group   = "free_text",
        script             = "both",
        context_hints      = ("notes","sms","message","otp","verification","رمز","كود"),
        presidio_score     = 0.90,
        unvalidated_reason = "shape is the evidence",
    ),
    "scan_otp_in_sms": PatternEntry(
        pattern            = r"(?i)(?:OTP|code|verification|كود|رمز)[^\d]{0,20}\b(\d{4,8})\b",
        entity_type        = "OTP",
        recognizer_group   = "free_text",
        script             = "both",
        context_hints      = ("sms","message","body","نص"),
        presidio_score     = 0.85,
        unvalidated_reason = "shape is the evidence",
    ),
    "puk_with_label": PatternEntry(
        pattern            = r"(?i)\b(?:puk|personal\s*unlock(?:ing)?\s*key)\s*[:=]?\s*\d{8}\b",
        entity_type        = "SIM_PUK",
        recognizer_group   = "free_text",
        script             = "latin",
        context_hints      = ("puk","sim","unlock","notes"),
        presidio_score     = 0.92,
        unvalidated_reason = "shape is the evidence",
    ),
    "cvv_with_label": PatternEntry(
        pattern            = r"(?i)\b(?:cvv|cvc|cvv2|security\s*code)\s*[:=]?\s*\d{3,4}\b",
        entity_type        = "CREDIT_CARD",
        recognizer_group   = "free_text",
        script             = "latin",
        context_hints      = ("cvv","card","payment","notes","بطاقة"),
        presidio_score     = 0.92,
        unvalidated_reason = "shape is the evidence",
    ),

    # =========================================================================
    # 6. GDPR ARTICLE 9 — SPECIAL-CATEGORY DATA
    # =========================================================================
    # Keyword vocabulary scanners — NOT shape evidence. An ICD-10 table, drug
    # catalogue, or device-capability column can contain these words without
    # being personal data. Scores stay below ``presidio_min`` (evidence-only);
    # the equation / context hints decide. Do not add ``unvalidated_reason``
    # claiming "shape is the evidence".

    "gdpr_religion": PatternEntry(
        pattern          = r"(?i)\b(?:muslim|christian|coptic|catholic|orthodox|jewish|buddhist|hindu|atheist|islam|christianity|الإسلام|مسلم|مسلمة|مسيحي|مسيحية|قبطي|قبطية|يهودي)\b",
        entity_type      = "GDPR_SPECIAL_CATEGORY",
        recognizer_group = "free_text",
        script           = "both",
        context_hints    = ("religion","faith","notes","ملاحظات","ديانة"),
        presidio_score   = 0.55,
    ),
    "gdpr_health_condition": PatternEntry(
        pattern          = r"(?i)\b(?:HIV|AIDS|cancer|diabetes|hepatitis|psychiatric|mental\s+(?:illness|health)|disability|الإيدز|سرطان|سكري|إعاقة|اضطراب\s+نفسي)\b",
        entity_type      = "GDPR_SPECIAL_CATEGORY",
        recognizer_group = "free_text",
        script           = "both",
        context_hints    = ("health","medical","notes","ملاحظات","صحة"),
        # Evidence-only: keyword hit ≠ special-category personal data.
        presidio_score   = 0.55,
    ),
    "gdpr_biometric_data": PatternEntry(
        pattern          = r"(?i)\b(?:fingerprint|iris\s+scan|facial\s+recognition|voice\s+print|biometric|بصمة|قزحية|بصمة\s+الوجه)\b",
        entity_type      = "GDPR_SPECIAL_CATEGORY",
        recognizer_group = "free_text",
        script           = "both",
        context_hints    = ("biometric","identity","notes","ملاحظات","بصمة"),
        # Evidence-only: device/capability vocab is not about an identified person.
        presidio_score   = 0.55,
    ),
    "gdpr_minor_data_flag": PatternEntry(
        pattern          = r"(?i)\b(?:child|children|minor|underage|under\s*18|guardian\s+of|طفل|أطفال|قاصر|تحت\s+السن)\b",
        entity_type      = "GDPR_SPECIAL_CATEGORY",
        recognizer_group = "free_text",
        script           = "both",
        context_hints    = ("notes","legal","guardian","ملاحظات","قاصر"),
        presidio_score   = 0.55,
    ),

    # =========================================================================
    # 7. CDR / LOG FREE-TEXT SCANNERS
    # =========================================================================

    "scan_national_id_egypt": PatternEntry(
        pattern          = r"\b[23]\d{13}\b",
        entity_type      = "EG_NATIONAL_ID",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_validator = "validate_egypt_national_id",
        context_hints    = ("log","notes","cdr","رقم_قومي"),
        collision_group  = "fourteen_digit_numeric",
        presidio_score   = 0.68,
    ),
    "scan_imei": PatternEntry(
        pattern          = r"\b\d{15}\b",
        entity_type      = "IMEI",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_validator = "validate_imei",
        validated_score  = 0.92,
        context_hints    = ("log","cdr","imei","device","جهاز"),
        collision_group  = "fifteen_digit_numeric",
        presidio_score   = 0.55,
    ),
    "scan_imsi": PatternEntry(
        pattern          = r"\b(?:602(?:01|02|03|04)\d{10}|602\d{12})\b",
        entity_type      = "IMSI",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_validator = "validate_imsi",
        validated_score  = 0.92,
        context_hints    = ("log","cdr","imsi","subscriber","sim","مشترك"),
        collision_group  = "fifteen_digit_numeric",
        presidio_score   = 0.55,
    ),
    "scan_iccid": PatternEntry(
        pattern          = r"\b89\d{16,20}\b",
        entity_type      = "ICCID",
        recognizer_group = "free_text",
        script           = "numeric",
        requires_validator = "validate_iccid",
        context_hints    = ("log","cdr","iccid","sim","شريحة"),
        presidio_score   = 0.82,
    ),
    "scan_transaction_id": PatternEntry(
        pattern          = r"\bTXN[-_]?\d{4,12}\b",
        entity_type      = "TRANSACTION_ID",
        recognizer_group = "free_text",
        script           = "latin",
        context_hints    = ("txn","transaction","عملية","كود_العملية","reference","ref"),
        presidio_score   = 0.75,
        unvalidated_reason = "TXN-prefixed reference codes are strong shape evidence in "
                             "wallet / billing call-center transcripts.",
    ),
    "scan_passport": PatternEntry(
        pattern          = r"\b[A-Z]\d{8}\b",
        entity_type      = "PASSPORT",
        recognizer_group = "free_text",
        script           = "latin",
        context_hints    = ("passport","travel","notes","جواز"),
        collision_group  = "passport_alpha_eight_digits",
        presidio_score   = 0.60,
    ),

    # =========================================================================
    # 8. EGYPT ADDRESS KEYWORDS
    # =========================================================================

    "address_keywords_egypt": PatternEntry(
        pattern          = r"(?i)\b(?:street|st\.?|road|rd\.?|building|floor|apartment|apt\.?|flat|villa|district|governorate|cairo|giza|alexandria|nasr\s*city|maadi|zamalek|الشارع|شارع|طريق|عمارة|دور|شقة|محافظة|القاهرة|الجيزة|الإسكندرية|مدينة\s+نصر|المعادي|الزمالك)\b",
        entity_type      = "LOCATION",
        recognizer_group = "free_text",
        script           = "both",
        context_hints    = ("address","location","notes","عنوان","ملاحظات"),
        presidio_score   = 0.65,  # keyword presence only, not full address
    ),

}


# ─────────────────────────────────────────────────────────────────────────────
# COLLISION RESOLUTION TABLE
# ─────────────────────────────────────────────────────────────────────────────
# Structure:
#   collision_group → list of (pattern_key, disambiguating_column_tokens)
#
# HOW LAYER 3 USES THIS:
#   For each column, GE provides column_name_hint tokens (lowercase).
#   For each collision_group that has multiple active recognizers:
#     - Find which entry's disambiguating_column_tokens best match the column name.
#     - Suppress all other recognizers in that group for this column.
#   If no tokens match → activate all recognizers in the group but lower
#     presidio_score by 0.15 and route to LLM refiner (Layer 4).

COLLISION_RESOLUTION_TABLE: dict[str, list[tuple[str, tuple[str, ...]]]] = {

    "fourteen_digit_numeric": [
        ("national_id_egypt_strict",   ("national","id","nid","رقم_قومي","هوية","الرقم_القومي","قومي")),
        ("national_id_egypt",          ("national","id","nid","رقم_قومي","هوية")),
        # drivers_license_egypt is structurally identical (14 digits).
        # Suppress NID recognizers if column hints suggest a license context.
        ("_suppress_nid_if",           ("license","driving","driver","رخصة","قيادة")),
    ],

    "fifteen_digit_numeric": [
        # MCC-known IMSI beats Luhn coincidence (S4). Name tokens still help.
        ("imsi_generic", ("imsi","subscriber","sim","identity","msin","mcc","mnc","مشترك")),
        ("imsi_strict", ("imsi","subscriber","sim","identity","مشترك")),
        ("imei",    ("imei","device","handset","equipment","terminal","جهاز","هاتف")),
    ],

    "sixteen_digit_numeric": [
        # Luhn-valid PAN wins over IMEISV — mislabelling a card is worse.
        ("credit_card_visa", ("card","visa","credit","payment","بطاقة")),
        ("credit_card_mastercard", ("card","mastercard","credit","payment","بطاقة")),
        ("credit_card_with_spaces", ("card","credit","payment","بطاقة")),
        ("meeza_card", ("card","meeza","payment","بطاقة","ميزا")),
        ("imeisv", ("imeisv","device","software","imei","جهاز")),
    ],

    "nine_digit_numeric": [
        ("tax_id_egypt_plain",      ("tax","tin","fiscal","ضريبي","ضريبة","سجل")),
        ("tax_id_egypt",            ("tax","tin","commercial","ضريبي","ضريبة")),
        # social_insurance_egypt is also 9-digit numeric.
        ("_suppress_tax_if",        ("social","insurance","pension","تأمين","معاش")),
    ],

    "vodafone_cash_vs_msisdn": [
        ("msisdn_egypt_vodafone",    ("phone","mobile","msisdn","tel","contact","هاتف","رقم","جوال")),
        ("vodafone_cash_account",    ("wallet","cash","ewallet","fintech","محفظة","فودافون_كاش","كاش")),
    ],

    "passport_alpha_eight_digits": [
        ("passport_egypt",           ("passport","travel","nationality","جواز","سفر","جنسية")),
        ("passport_us",              ("passport","us","american","usa","جواز")),
        ("passport_ksa",             ("passport","saudi","ksa","جواز","سعودي")),
        # All are [A-Z]\d{8}. Country of origin resolves this if known.
        # Use customer country_code column as secondary signal if available.
    ],

    "ten_digit_numeric": [
        ("national_id_ksa",          ("national","id","iqama","saudi","هوية","اقامة")),
        ("national_id_jordan",       ("national","id","jordan","هوية","أردني")),
        # Various account numbers, subscriber IDs share this length.
        # Requires column name match to activate.
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# COVERAGE GAP REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
# These PII types are NOT covered by regex in this catalog.
# They are fully delegated to GLiNER multi-v2.1.
# The agent MUST NOT attempt to build regex recognizers for these.

COVERAGE_GAPS: dict[str, dict] = {

    "arabic_person_names": {
        "entity_type":  "PERSON",
        "reason":       "Arabic names have no fixed structure or length. "
                        "Regex false-positive rate is unacceptably high. "
                        "GLiNER multi-v2.1 handles via NER.",
        "gliner_label": "person",
        "notes":        "person_name_arabic in CATALOG has presidio_score=0.40 "
                        "as a weak supplementary signal only.",
    },

    "english_person_names": {
        "entity_type":  "PERSON",
        "reason":       "Capitalized multi-word strings match too many non-name values. "
                        "GLiNER handles reliably.",
        "gliner_label": "person",
    },

    "arabic_addresses": {
        "entity_type":  "LOCATION",
        "reason":       "Arabic address structure is free-form and context-dependent. "
                        "address_keywords_egypt in CATALOG provides keyword signals only. "
                        "Full address extraction delegated to GLiNER.",
        "gliner_label": "address",
    },

    "english_addresses": {
        "entity_type":  "LOCATION",
        "reason":       "Free-form English addresses not reliably matchable by regex. "
                        "street_address_loose in CATALOG is too broad for production use.",
        "gliner_label": "address",
    },

    "north_africa_national_ids": {
        "entity_type":  "NATIONAL_ID",
        "reason":       "Libya, Sudan, Algeria, Tunisia structured national IDs are "
                        "not included in this version. MSISDN patterns for these "
                        "countries are covered in CATALOG. "
                        "Add PatternEntry records when those markets are in scope.",
        "gliner_label": "id number",
        "markets":      ["LY", "SD", "DZ"],  # Tunisia TN has basic pattern above
        "status":       "known_gap_v2",
    },

    "organization_names": {
        "entity_type":  "ORGANIZATION",
        "reason":       "Company and organization names have no pattern. GLiNER handles.",
        "gliner_label": "organization",
    },

    "mixed_script_freetext": {
        "entity_type":  "PERSON | LOCATION | ORGANIZATION",
        "reason":       "Mixed Arabic/English text in notes/comments fields. "
                        "Regex scan variants cover structured PII (phone, ID) in these fields. "
                        "Semantic entity extraction (names, orgs, locations) fully delegated "
                        "to GLiNER multi-v2.1 which natively handles mixed-script input.",
        "gliner_label": "person, address, organization",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# COMPANION VALIDATORS
# ─────────────────────────────────────────────────────────────────────────────
# Called by Layer 3 AFTER a regex match, before emitting a detection.
# Registered by name so PatternEntry.requires_validator wires them automatically.

def validate_luhn(number: str) -> bool:
    """Luhn checksum — required for IMEI, ICCID, credit cards."""
    import re as _re
    digits = [int(d) for d in _re.sub(r"\D", "", number)]
    if not digits:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def validate_jwt(raw: str) -> bool:
    """A JWT header segment must base64url-decode to JSON carrying ``alg``."""
    import base64
    import json as _json

    text = (raw or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return False
    head = parts[0]
    try:
        padded = head + "=" * (-len(head) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        obj = _json.loads(decoded.decode("utf-8"))
    except Exception:
        return False
    return isinstance(obj, dict) and "alg" in obj


def validate_ksa_national_id(raw: str) -> bool:
    """KSA Iqama/National ID: 10 digits, prefix 1 or 2, Luhn check digit."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) != 10 or digits[0] not in ("1", "2"):
        return False
    return validate_luhn(digits)


# ISO 13616 IBAN lengths by country. Extend as needed.
_IBAN_LENGTHS: dict[str, int] = {
    "EG": 29, "AE": 23, "SA": 24, "JO": 30, "KW": 30, "QA": 29, "BH": 22,
    "OM": 23, "LB": 28, "MA": 28, "TN": 24, "DZ": 26, "LY": 25,
    "GB": 22, "DE": 22, "FR": 27, "IT": 27, "ES": 24, "NL": 18, "CH": 21,
    "TR": 26, "PK": 24, "IN": 0,
}


def validate_iban(raw: str) -> bool:
    """ISO 13616: country-length check + mod-97 == 1."""
    s = "".join(str(raw or "").split()).upper().replace("-", "")
    if len(s) < 15 or len(s) > 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    expected = _IBAN_LENGTHS.get(s[:2])
    if expected and len(s) != expected:
        return False
    rearranged = s[4:] + s[:4]
    total = 0
    for ch in rearranged:
        if ch.isdigit():
            total = (total * 10 + int(ch)) % 97
        elif ch.isalpha():
            total = (total * 100 + (ord(ch) - 55)) % 97
        else:
            return False
    return total == 1


_ISO3166_ALPHA2 = frozenset({
    "AE","BH","DZ","EG","FR","DE","GB","IN","IQ","JO","KW","LB","LY","MA","OM",
    "PK","PS","QA","SA","SD","SY","TN","TR","US","YE",
})  # extend as needed; unknown codes fail closed


def validate_swift_bic(raw: str) -> bool:
    """ISO 9362: AAAA CC LL [BBB]; country code must be a real ISO 3166 alpha-2."""
    s = "".join(str(raw or "").split()).upper()
    if len(s) not in (8, 11) or not s[:4].isalpha():
        return False
    if s[4:6] not in _ISO3166_ALPHA2:
        return False
    if not s[6:8].isalnum():
        return False
    return len(s) == 8 or s[8:11].isalnum()


def validate_uae_national_id(raw: str) -> bool:
    """UAE Emirates ID: 784-YYYY-NNNNNNN-C, 15 digits, Luhn check digit."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) != 15 or not digits.startswith("784"):
        return False
    year = int(digits[3:7])
    if not (1900 <= year <= 2100):
        return False
    return validate_luhn(digits)


def validate_mac_address(raw: str) -> bool:
    """Reject all-zero, broadcast, and multicast MACs — not device identities."""
    hexs = "".join(ch for ch in str(raw or "") if ch in "0123456789abcdefABCDEF")
    if len(hexs) != 12:
        return False
    if hexs.lower() in ("000000000000", "ffffffffffff"):
        return False
    return not (int(hexs[:2], 16) & 0x01)  # multicast bit


def validate_eth_address(raw: str) -> bool:
    """EIP-55: all-lower / all-upper accepted; mixed case must match the checksum."""
    s = str(raw or "").strip()
    if not s.startswith("0x") or len(s) != 42:
        return False
    body = s[2:]
    if not all(c in "0123456789abcdefABCDEF" for c in body):
        return False
    if body == body.lower() or body == body.upper():
        return True                      # no checksum encoded — shape only
    try:
        from hashlib import sha3_256      # keccak approximation guard
    except ImportError:
        return True
    # Without a keccak256 implementation we cannot verify EIP-55; accept shape.
    return True


def validate_btc_address(raw: str) -> bool:
    """Base58Check: decode and verify the 4-byte double-SHA256 checksum."""
    import hashlib

    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    s = str(raw or "").strip()
    if not s or any(c not in alphabet for c in s):
        return False
    num = 0
    for c in s:
        num = num * 58 + alphabet.index(c)
    raw_bytes = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    pad = len(s) - len(s.lstrip("1"))
    payload = b"\x00" * pad + raw_bytes
    if len(payload) < 5:
        return False
    body, checksum = payload[:-4], payload[-4:]
    digest = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return digest == checksum


def validate_egypt_national_id(nid: str):
    """
    Validate Egyptian National ID and extract embedded demographics.

    Implementation lives in ``redibis.pii.national_id_egypt`` (pure module).
    Re-exported here for one release so ``telecom_signals`` /
    ``recognizer_factory`` / ``catalog_validators`` keep working unchanged.

    Returns ``NidResult`` (dict-compatible via ``.as_dict()`` / ``__getitem__``).
    """
    from redibis.pii.national_id_egypt import validate_egypt_national_id as _validate

    return _validate(nid)


def validate_iccid(iccid: str) -> bool:
    """ICCID = 89... prefix + Luhn checksum, 19–20 digits."""
    import re as _re
    return bool(_re.match(r"^89\d{17,18}$", iccid)) and validate_luhn(iccid)


def validate_nir_mod97(nir: str) -> bool:
    """French NIR (INSEE) key = ``97 - (body % 97)`` over the first 13 digits.

    Corsica department codes ``2A`` / ``2B`` are accepted as ``19`` / ``18``
    when letters appear in positions 6–7 of the 15-character form.
    """
    import re as _re

    raw = str(nir or "").strip().upper().replace(" ", "")
    if not raw:
        return False
    # Allow 2A/2B in dept slot then map to digits for mod-97.
    if len(raw) == 15 and raw[5:7] in {"2A", "2B"}:
        mapped = raw[:5] + ("19" if raw[5:7] == "2A" else "18") + raw[7:]
    else:
        mapped = raw
    digits = _re.sub(r"\D", "", mapped)
    if len(digits) != 15:
        return False
    body, key = digits[:13], int(digits[13:15])
    return (97 - (int(body) % 97)) == key


# Registry — Layer 3 resolves requires_validator strings via this dict.
def validate_imei(raw: str):
    """Catalog bridge → ``redibis.pii.device_id.validate_imei``."""
    from redibis.pii.device_id import validate_imei as _v
    return _v(raw)


def validate_imeisv(raw: str):
    """Catalog bridge → ``redibis.pii.device_id.validate_imeisv``."""
    from redibis.pii.device_id import validate_imeisv as _v
    return _v(raw)


def validate_imsi(raw: str):
    """Catalog bridge → ``redibis.pii.subscriber_id.validate_imsi``."""
    from redibis.pii.subscriber_id import validate_imsi as _v
    return _v(raw)


def validate_scaled_coordinate(raw: str) -> bool:
    """Value-level gate for ``gps_scaled_int`` — digit length only.

    Scale + partner + geofence agreement live in ``geo_engine.detect_coordinate_scale``
    (column-level). This validator only confirms the surface form so Presidio
    can emit evidence without promoting orphaned integers.
    """
    import re as _re
    s = str(raw or "").strip().lstrip("+-")
    return bool(_re.fullmatch(r"\d{7,9}", s))


catalog_validators: dict[str, Callable] = {
    "validate_luhn":               validate_luhn,
    "validate_jwt":                validate_jwt,
    "validate_ksa_national_id":    validate_ksa_national_id,
    "validate_iban":               validate_iban,
    "validate_swift_bic":          validate_swift_bic,
    "validate_uae_national_id":    validate_uae_national_id,
    "validate_mac_address":        validate_mac_address,
    "validate_eth_address":        validate_eth_address,
    "validate_btc_address":        validate_btc_address,
    "validate_egypt_national_id":  validate_egypt_national_id,
    "validate_iccid":              validate_iccid,
    "validate_nir_mod97":          validate_nir_mod97,
    "nir_mod97":                   validate_nir_mod97,
    "validate_imei":               validate_imei,
    "validate_imeisv":             validate_imeisv,
    "validate_imsi":               validate_imsi,
    "validate_scaled_coordinate":  validate_scaled_coordinate,
}


# ─────────────────────────────────────────────────────────────────────────────
# COMPANION NORMALIZERS
# ─────────────────────────────────────────────────────────────────────────────
# Called by Layer 1 during sample cleanup on columns GE flagged as phone candidates.
# Registered by name so PatternEntry.requires_normalizer wires them automatically.

def normalize_msisdn_egypt(raw) -> str | None:
    """
    Normalize a dirty Egyptian MSISDN to canonical +20XXXXXXXXXX form.
    Handles: Excel float coercion (.0 suffix), apostrophe prefix,
    spaces/dashes, 00-prefix, missing country code.
    Returns canonical string or None if unparseable.
    """
    import re as _re
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() in {"NULL", "N/A", "NONE", "NAN", "0"}:
        return None
    if s.startswith("'"):
        s = s[1:]
    if s.endswith(".0"):
        s = s[:-2]
    has_plus = s.lstrip().startswith("+")
    digits = _re.sub(r"\D", "", s)
    if not digits:
        return None
    if has_plus and digits.startswith("20"):
        digits = digits[2:]
    elif digits.startswith("0020"):
        digits = digits[4:]
    elif digits.startswith("20") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and _re.match(r"^1[0125]\d{8}$", digits):
        return f"+20{digits}"
    return None


def egypt_operator_from_msisdn(raw) -> str | None:
    """
    Return original Egyptian operator name from an MSISDN.
    NOTE: Egypt has had MNP since 2008 — this returns the ORIGINAL
    allocation, not the current operator. Real routing requires HLR lookup.
    """
    canon = normalize_msisdn_egypt(raw)
    if not canon:
        return None
    prefix = canon[3:5]
    return {
        "10": "Vodafone",
        "11": "Etisalat (e&)",
        "12": "Orange",
        "15": "WE / Telecom Egypt",
    }.get(prefix)


# Registry — Layer 1 resolves requires_normalizer strings via this dict.
catalog_normalizers: dict[str, Callable] = {
    "normalize_msisdn_egypt": normalize_msisdn_egypt,
}


def regex_test_candidates(raw, normalizer: str | None) -> list[tuple[str, str]]:
    """
    Build labeled text variants for regex smoke-tests.

    When a catalog pattern declares ``requires_normalizer``, the UI test box
    tries raw text first, then preprocessed forms (e.g. digits-only MSISDN).
    """
    import re as _re
    text = str(raw).strip()
    out: list[tuple[str, str]] = [("raw", text)]
    if not normalizer:
        return out
    fn = catalog_normalizers.get(normalizer)
    if not fn:
        return out
    seen = {text}
    if normalizer == "normalize_msisdn_egypt":
        digits = _re.sub(r"\D", "", text)
        if digits and digits not in seen:
            out.append(("digits_only", digits))
            seen.add(digits)
        canon = fn(text)
        if canon and canon not in seen:
            out.append(("normalized", canon))
            seen.add(canon)
            if canon.startswith("+20") and len(canon) == 13:
                national = "0" + canon[3:]
                if national not in seen:
                    out.append(("national", national))
                    seen.add(national)
    else:
        normed = fn(text)
        if normed and str(normed) not in seen:
            out.append(("normalized", str(normed)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# COMPILED PATTERN SETS  (pre-compiled at import time — never compile per-row)
# ─────────────────────────────────────────────────────────────────────────────

import re as _re

# All active patterns compiled
compiled_patterns: dict[str, _re.Pattern] = {
    name: _re.compile(entry.pattern)
    for name, entry in CATALOG.items()
    if entry.active
}

# Structured group only (for field-value columns)
compiled_structured: dict[str, _re.Pattern] = {
    name: _re.compile(entry.pattern)
    for name, entry in CATALOG.items()
    if entry.active and entry.recognizer_group == "structured"
}

# Free-text group only (for notes/comment/blob columns)
compiled_free_text: dict[str, _re.Pattern] = {
    name: _re.compile(entry.pattern)
    for name, entry in CATALOG.items()
    if entry.active and entry.recognizer_group == "free_text"
}

# Arabic-script aware patterns (activate when GE detects Arabic characters)
compiled_arabic_aware: dict[str, _re.Pattern] = {
    name: _re.compile(entry.pattern)
    for name, entry in CATALOG.items()
    if entry.active and entry.script in ("arabic", "both")
}


# ─────────────────────────────────────────────────────────────────────────────
# CATALOG QUERY UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
# Programmatic access for API endpoints, interactive review, and testing.

def _entry_to_dict(name: str, entry: PatternEntry) -> dict:
    """Serialize a PatternEntry to a plain dict."""
    return {
        "name":                name,
        "pattern":             entry.pattern,
        "entity_type":         entry.entity_type,
        "recognizer_group":    entry.recognizer_group,
        "script":              entry.script,
        "requires_validator":  entry.requires_validator,
        "requires_normalizer": entry.requires_normalizer,
        "context_hints":       list(entry.context_hints),
        "collision_group":     entry.collision_group,
        "presidio_score":      entry.presidio_score,
        "validated_score":     entry.validated_score,
        "unvalidated_reason":  entry.unvalidated_reason,
        "active":              entry.active,
    }


def list_catalog(
    group: str | None = None,
    active_only: bool = True,
    entity_type: str | None = None,
    script: str | None = None,
) -> list[dict]:
    """
    Return all patterns as serializable dicts, optionally filtered.

    Parameters
    ----------
    group : "structured" | "free_text" | None
        Filter by recognizer_group.  None = all groups.
    active_only : bool
        If True, skip patterns with ``active=False``.
    entity_type : str | None
        Filter by entity_type (e.g. "PHONE_NUMBER").  None = all.
    script : str | None
        Filter by script (e.g. "arabic", "numeric").  None = all.

    Returns
    -------
    list[dict] — each dict has all PatternEntry fields plus ``name``.
    """
    results = []
    for name, entry in CATALOG.items():
        if active_only and not entry.active:
            continue
        if group is not None and entry.recognizer_group != group:
            continue
        if entity_type is not None and entry.entity_type != entity_type:
            continue
        if script is not None and entry.script != script:
            continue
        results.append(_entry_to_dict(name, entry))
    return results


def get_pattern(name: str) -> dict | None:
    """
    Get a single pattern by key.

    Returns None if the key does not exist in the CATALOG.
    """
    entry = CATALOG.get(name)
    if entry is None:
        return None
    return _entry_to_dict(name, entry)


def build_effective_catalog(
    overrides: "RegexOverrides | None" = None,
) -> dict[str, PatternEntry]:
    """
    Return the final catalog after applying overrides.

    Delegates to ``RuleSetCompiler.compile_patterns`` — the canonical compile path.
    """
    from redibis.pii.rules.ruleset import RuleSetCompiler

    return RuleSetCompiler.compile_patterns(overrides)


def _dict_to_pattern_entry(d: dict) -> PatternEntry:
    """
    Build a PatternEntry from a plain dict.

    Required keys: ``pattern``, ``entity_type``, ``recognizer_group``.
    All other PatternEntry fields are optional with defaults.
    """
    hints = d.get("context_hints", ())
    if isinstance(hints, list):
        hints = tuple(hints)

    return PatternEntry(
        pattern             = d["pattern"],
        entity_type         = d["entity_type"],
        recognizer_group    = d.get("recognizer_group", "structured"),
        script              = d.get("script", "latin"),
        requires_validator  = d.get("requires_validator"),
        requires_normalizer = d.get("requires_normalizer"),
        context_hints       = hints,
        collision_group     = d.get("collision_group"),
        presidio_score      = float(d.get("presidio_score", 0.85)),
        validated_score     = (
            float(d["validated_score"])
            if d.get("validated_score") is not None
            else None
        ),
        unvalidated_reason  = d.get("unvalidated_reason"),
        active              = bool(d.get("active", True)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SMOKE TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print(f"CATALOG entries     : {len(CATALOG)}")
    print(f"Active entries      : {sum(1 for e in CATALOG.values() if e.active)}")
    print(f"Structured patterns : {len(compiled_structured)}")
    print(f"Free-text patterns  : {len(compiled_free_text)}")
    print(f"Arabic-aware        : {len(compiled_arabic_aware)}")
    print(f"Collision groups    : {len(COLLISION_RESOLUTION_TABLE)}")
    print(f"Coverage gaps       : {len(COVERAGE_GAPS)}")
    print()

    # Validator tests
    assert validate_luhn("490154203237518"),        "IMEI Luhn failed"
    assert not validate_luhn("490154203237519"),     "IMEI Luhn false positive"
    assert validate_iccid("8910010100000000016"),    "ICCID failed"

    nid = validate_egypt_national_id("29001011401234")
    assert nid["valid"],                             "NID strict failed"
    assert nid["governorate"] == "Qalyubia",         f"NID governorate wrong: {nid}"
  