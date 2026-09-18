"""Content-addressed scan configuration provenance (Plan 2).

``provenance_uuid`` is uuid5 over a canonical payload. Identical configuration
yields the same UUID, including when the record is degraded (HTTP NER, missing
layer digest). ``description`` is an operator label and is not part of identity.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from redibis.pii.eval.provenance import canonical_json_bytes, rules_checksum, sha256_hex

# Fixed namespace — never change. Air-gapped installs of the same redibis
# release must agree on provenance_uuid for a given configuration.
_PROV_NS = uuid.UUID("c3e8a41f-7b29-4d60-9e15-2f8c6a0d473b")

_NER_WEIGHT_NAMES = (
    "model.safetensors",
    "pytorch_model.bin",
    "model.bin",
    "gliner_config.json",
)

_ENGINE_ORDER = ("regex", "phone", "ner")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redibis_version() -> str:
    from redibis import __version__

    return str(__version__)


def _sha256_prefixed(payload: Any) -> str:
    return "sha256:" + sha256_hex(canonical_json_bytes(payload))


@dataclass(frozen=True)
class ScanProvenance:
    """Immutable scan-configuration record. Minted once; referenced by UUID."""

    provenance_uuid: str
    description: str
    created: str
    redibis_version: str
    stack_uuid: str
    stack_sha256: str
    pack_layers: tuple[dict, ...]
    ruleset_id: str
    ruleset_version: str
    rules_checksum: str
    rules_source: tuple[str, ...]
    regex_inventory: tuple[str, ...]
    regex_inventory_sha256: str
    number_rules_sha256: str
    ner_backend: str
    ner_model_id: str
    ner_model_sha256: str
    ner_labels: tuple[str, ...]
    ner_phrases_sha256: str
    expanders: tuple[str, ...]
    validators: tuple[str, ...]
    span_policies: tuple[str, ...]
    thresholds: dict
    llm: dict
    gazetteers: tuple[dict, ...]
    lexicons: tuple[dict, ...]
    normalization_profile: str = ""
    provenance_degraded: bool = False
    provenance_degraded_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "provenance_uuid": self.provenance_uuid,
            "description": self.description,
            "created": self.created,
            "redibis_version": self.redibis_version,
            "stack_uuid": self.stack_uuid,
            "stack_sha256": self.stack_sha256,
            "pack_layers": [dict(x) for x in self.pack_layers],
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
            "rules_checksum": self.rules_checksum,
            "rules_source": list(self.rules_source),
            "regex_inventory": list(self.regex_inventory),
            "regex_inventory_sha256": self.regex_inventory_sha256,
            "number_rules_sha256": self.number_rules_sha256,
            "ner_backend": self.ner_backend,
            "ner_model_id": self.ner_model_id,
            "ner_model_sha256": self.ner_model_sha256,
            "ner_labels": list(self.ner_labels),
            "ner_phrases_sha256": self.ner_phrases_sha256,
            "expanders": list(self.expanders),
            "validators": list(self.validators),
            "span_policies": list(self.span_policies),
            "thresholds": dict(self.thresholds),
            "llm": dict(self.llm),
            "gazetteers": [dict(x) for x in self.gazetteers],
            "lexicons": [dict(x) for x in self.lexicons],
            "normalization_profile": self.normalization_profile,
            "provenance_degraded": self.provenance_degraded,
            "provenance_degraded_reason": self.provenance_degraded_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ScanProvenance":
        raw = dict(data or {})
        return cls(
            provenance_uuid=str(raw.get("provenance_uuid") or ""),
            description=str(raw.get("description") or ""),
            created=str(raw.get("created") or ""),
            redibis_version=str(raw.get("redibis_version") or ""),
            stack_uuid=str(raw.get("stack_uuid") or ""),
            stack_sha256=str(raw.get("stack_sha256") or ""),
            pack_layers=tuple(dict(x) for x in (raw.get("pack_layers") or []) if isinstance(x, dict)),
            ruleset_id=str(raw.get("ruleset_id") or ""),
            ruleset_version=str(raw.get("ruleset_version") or ""),
            rules_checksum=str(raw.get("rules_checksum") or ""),
            rules_source=tuple(str(x) for x in (raw.get("rules_source") or ())),
            regex_inventory=tuple(str(x) for x in (raw.get("regex_inventory") or ())),
            regex_inventory_sha256=str(raw.get("regex_inventory_sha256") or ""),
            number_rules_sha256=str(raw.get("number_rules_sha256") or ""),
            ner_backend=str(raw.get("ner_backend") or ""),
            ner_model_id=str(raw.get("ner_model_id") or ""),
            ner_model_sha256=str(raw.get("ner_model_sha256") or ""),
            ner_labels=tuple(str(x) for x in (raw.get("ner_labels") or ())),
            ner_phrases_sha256=str(raw.get("ner_phrases_sha256") or ""),
            expanders=tuple(str(x) for x in (raw.get("expanders") or ())),
            validators=tuple(str(x) for x in (raw.get("validators") or ())),
            span_policies=tuple(str(x) for x in (raw.get("span_policies") or ())),
            thresholds=dict(raw.get("thresholds") or {}),
            llm=dict(raw.get("llm") or {}),
            gazetteers=tuple(dict(x) for x in (raw.get("gazetteers") or []) if isinstance(x, dict)),
            lexicons=tuple(dict(x) for x in (raw.get("lexicons") or []) if isinstance(x, dict)),
            normalization_profile=str(raw.get("normalization_profile") or ""),
            provenance_degraded=bool(raw.get("provenance_degraded")),
            provenance_degraded_reason=str(raw.get("provenance_degraded_reason") or ""),
        )


def _identity_payload(rec: ScanProvenance) -> dict[str, Any]:
    """Fields that participate in ``provenance_uuid``.

    Operator labels and wall-clock must not change the address: identical
    configuration yields an identical UUID regardless of ``description``.
    Degrade flags are derived, not identity.
    """
    data = rec.to_dict()
    for key in (
        "provenance_uuid",
        "description",
        "created",
        "provenance_degraded",
        "provenance_degraded_reason",
    ):
        data.pop(key, None)
    return data


def digest_ner_backend(ner_backend: object | None) -> tuple[str, str, str, Optional[str]]:
    """Return ``(kind, model_id, sha256, degrade_reason)``.

    No backend is not a degrade. HTTP NER cannot be digested. A local path
    without a hashable weight file degrades.
    """
    if ner_backend is None:
        return "", "", "", None
    name = str(getattr(ner_backend, "name", "") or "")
    if name.startswith("http:") or name.startswith("http/"):
        return "http", name, "", "http NER cannot be digested"
    cached = getattr(ner_backend, "_redibis_model_sha256", None)
    model_id = str(
        getattr(ner_backend, "_model_path", None)
        or getattr(ner_backend, "model_path", None)
        or name
    )
    kind = "gliner" if name.startswith("gliner") or "gliner" in name.lower() else (
        name.split(":", 1)[0] if ":" in name else (name or "ner")
    )
    if isinstance(cached, str) and cached:
        return kind, model_id, cached, None
    path = Path(str(model_id)).expanduser() if model_id else None
    if path is None or not str(model_id).strip():
        return kind, model_id, "", "NER model path is empty; refuse to mint provenance"
    if not path.exists():
        return kind, model_id, "", f"NER model path not found: {path}"
    target = path
    if path.is_dir():
        target = None
        for fname in _NER_WEIGHT_NAMES:
            candidate = path / fname
            if candidate.is_file():
                target = candidate
                break
        if target is None:
            return (
                kind,
                model_id,
                "",
                f"NER model at {path} has no digestible weights",
            )
    try:
        digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as exc:
        return kind, model_id, "", f"NER model digest failed: {exc}"
    try:
        object.__setattr__(ner_backend, "_redibis_model_sha256", digest)
    except Exception:
        try:
            setattr(ner_backend, "_redibis_model_sha256", digest)
        except Exception:
            pass
    return kind, model_id, digest, None


def _named_docs_inventory(docs: Mapping[str, Any] | None) -> tuple[dict, ...]:
    rows: list[dict] = []
    for name, doc in sorted((docs or {}).items()):
        version = ""
        if isinstance(doc, dict):
            version = str(doc.get("version") or doc.get("metadata", {}).get("version") or "")
            if isinstance(doc.get("metadata"), dict) and not version:
                version = str(doc["metadata"].get("version") or "")
        rows.append(
            {
                "name": str(name),
                "version": version,
                "sha256": _sha256_prefixed(doc if doc is not None else {}),
            }
        )
    return tuple(rows)


def _thresholds_dict(ruleset: Any, scan_config: Any) -> dict:
    thr = getattr(ruleset, "thresholds", None)
    min_score = float(getattr(scan_config, "min_score", 0.35) or 0.35) if scan_config else 0.35
    if thr is None:
        return {"min_score": min_score}
    return {
        "presidio_min": float(getattr(thr, "presidio_min", 0) or 0),
        "gliner_min": float(getattr(thr, "gliner_min", 0) or 0),
        "phone_min": float(getattr(thr, "phone_min", 0) or 0),
        "nid_min": float(getattr(thr, "nid_min", 0) or 0),
        "min_score": min_score,
    }


def _llm_dict(redibis_config: Any, scan_config: Any) -> dict:
    pii = getattr(redibis_config, "pii", None) if redibis_config is not None else None
    llm = getattr(pii, "llm", None) if pii is not None else None
    use_llm = bool(getattr(scan_config, "use_llm", False)) if scan_config else False
    enabled = bool(getattr(llm, "enabled", False)) if llm is not None else False
    return {
        "enabled": enabled or use_llm,
        "provider": str(getattr(llm, "provider", "") or "") if llm is not None else "",
        "model": str(getattr(llm, "model_name", "") or "") if llm is not None else "",
        "allow_external_raw_text": bool(
            getattr(llm, "allow_external_raw_text", False)
        )
        if llm is not None
        else False,
    }


def _regex_inventory(ruleset: Any) -> tuple[str, ...]:
    try:
        patterns = ruleset.patterns_for_group("free_text")
    except Exception:
        patterns = getattr(ruleset, "patterns", {}) or {}
    names = sorted(
        str(name)
        for name, entry in (patterns or {}).items()
        if getattr(entry, "active", True)
    )
    return tuple(names)


def _engine_set(engines: str | None) -> set[str]:
    e = (engines or "both").lower().strip()
    if e in ("none", "off"):
        return set()
    if e in ("both", "all"):
        return {"regex", "phone", "ner"}
    if e == "regex":
        return {"regex", "phone"}
    if e == "ner":
        return {"ner"}
    if e == "phone":
        return {"phone"}
    return {p.strip() for p in e.split(",") if p.strip()} or {"regex", "phone", "ner"}


def _span_policies(scan_config: Any) -> tuple[str, ...]:
    """Stages that actually run for this scan config — not the full catalogue."""
    stages = ["validate"]
    obfuscation = bool(getattr(scan_config, "preprocess_obfuscation", False)) if scan_config else False
    if obfuscation:
        stages.append("preprocess")
    engines = _engine_set(getattr(scan_config, "engines", None) if scan_config else None)
    if scan_config is None:
        engines = set()
    for name in _ENGINE_ORDER:
        if name in engines:
            stages.append(name)
    if scan_config is not None and getattr(scan_config, "use_llm", False):
        stages.append("llm")
    stages.append("resolve")
    return tuple(stages)


def _expander_names(scan_config: Any) -> tuple[str, ...]:
    if scan_config is None or not getattr(scan_config, "preprocess_obfuscation", False):
        return ()
    names = tuple(getattr(scan_config, "preprocess_expanders", ()) or ())
    if names:
        return tuple(str(n) for n in names)
    try:
        from redibis.pii.text_preprocess.registry import expander_registry

        return tuple(sorted(expander_registry()))
    except Exception:
        return ()


def _validator_names(scan_config: Any) -> tuple[str, ...]:
    if scan_config is None or not getattr(scan_config, "preprocess_obfuscation", False):
        return ()
    try:
        from redibis.pii.text_preprocess.registry import validator_registry

        return tuple(sorted(validator_registry()))
    except Exception:
        return ()


def mint_scan_provenance(
    *,
    ruleset: Any,
    stack: Any = None,
    ner_backend: object | None = None,
    redibis_config: Any = None,
    scan_config: Any = None,
    description: str = "",
    normalization_profile: str = "",
) -> ScanProvenance:
    """Mint a fail-closed ScanProvenance for this compiled configuration."""
    degrade_reasons: list[str] = []
    stack_uuid = ""
    stack_sha256 = ""
    pack_layers: tuple[dict, ...] = ()
    layers = list(getattr(stack, "layers", None) or [])
    if layers:
        try:
            from redibis.pack.identity import compute_stack_sha256, compute_stack_uuid

            # Last-layer-wins is the intended stack-mode policy, not an accident:
            # compute_stack_uuid takes one mode, so we hash the top layer's overlay
            # policy. Per-layer mode stays on pack_layers[].mode and is not part
            # of the stack hash.
            mode = str(getattr(layers[-1], "mode", None) or "overlay")
            stack_uuid = compute_stack_uuid(layers, mode=mode)
            stack_sha256 = compute_stack_sha256(layers, mode=mode)
            pack_layers = tuple(
                L.to_dict() if hasattr(L, "to_dict") else dict(L) for L in layers
            )
        except (TypeError, ValueError) as exc:
            degrade_reasons.append(str(exc))
            pack_layers = tuple(
                L.to_dict() if hasattr(L, "to_dict") else {"repr": repr(L)}
                for L in layers
            )

    overlay = getattr(ruleset, "text_rules", None)
    checksum = rules_checksum(overlay)
    inventory = _regex_inventory(ruleset)
    overlay_dict = overlay.to_dict() if overlay is not None and hasattr(overlay, "to_dict") else {}
    number_payload = overlay_dict.get("number_rules") if isinstance(overlay_dict, dict) else {}

    ner_kind, ner_id, ner_sha, ner_reason = digest_ner_backend(ner_backend)
    if ner_reason:
        degrade_reasons.append(ner_reason)

    phrases = dict(getattr(ruleset, "ner_phrases", {}) or {})
    gazetteers = _named_docs_inventory(getattr(stack, "text_gateway_gazetteers", None) if stack else None)
    lexicons = _named_docs_inventory(getattr(stack, "text_gateway_lexicons", None) if stack else None)

    rec = ScanProvenance(
        provenance_uuid="",
        description=str(description or ""),
        created=_utc_now(),
        redibis_version=_redibis_version(),
        stack_uuid=stack_uuid,
        stack_sha256=stack_sha256,
        pack_layers=pack_layers,
        ruleset_id=str(getattr(ruleset, "id", "") or ""),
        ruleset_version=str(getattr(ruleset, "version", "") or ""),
        rules_checksum=checksum,
        rules_source=tuple(str(x) for x in (getattr(ruleset, "rules_source", ()) or ())),
        regex_inventory=inventory,
        regex_inventory_sha256=_sha256_prefixed(list(inventory)),
        number_rules_sha256=_sha256_prefixed(number_payload or {}),
        ner_backend=ner_kind,
        ner_model_id=ner_id,
        ner_model_sha256=ner_sha,
        ner_labels=tuple(str(x) for x in (getattr(ruleset, "ner_labels", ()) or ())),
        ner_phrases_sha256=_sha256_prefixed(phrases),
        expanders=_expander_names(scan_config),
        validators=_validator_names(scan_config),
        span_policies=_span_policies(scan_config),
        thresholds=_thresholds_dict(ruleset, scan_config),
        llm=_llm_dict(redibis_config, scan_config),
        gazetteers=gazetteers,
        lexicons=lexicons,
        normalization_profile=str(normalization_profile or ""),
        provenance_degraded=bool(degrade_reasons),
        provenance_degraded_reason="; ".join(degrade_reasons),
    )
    uid = str(uuid.uuid5(_PROV_NS, canonical_json_bytes(_identity_payload(rec)).decode("utf-8")))
    return replace(rec, provenance_uuid=uid)


__all__ = [
    "ScanProvenance",
    "digest_ner_backend",
    "mint_scan_provenance",
]
