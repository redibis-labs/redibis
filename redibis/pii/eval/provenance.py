"""Run identity, rules checksums, and comparable report stripping."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from redibis.pii.text_rules import TextRuleOverlay


def _redibis_version() -> str:
    from redibis import __version__

    return str(__version__)


def new_run_uuid(explicit: str | None = None) -> str:
    raw = str(explicit or "").strip()
    if raw:
        return raw
    return str(uuid.uuid4())


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    blob = data if isinstance(data, bytes) else data.encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def rules_checksum(overlay: TextRuleOverlay | Mapping[str, Any] | None) -> str:
    if overlay is None:
        raise ValueError("rules overlay is missing; refusing to checksum {} as if it were a ruleset")
    if isinstance(overlay, TextRuleOverlay):
        payload = overlay.to_dict()
    else:
        payload = dict(overlay)
    return "sha256:" + sha256_hex(canonical_json_bytes(payload))


def dataset_checksum(dataset: Mapping[str, Any] | None) -> str:
    if not dataset:
        return "sha256:" + sha256_hex(b"{}")
    return "sha256:" + sha256_hex(canonical_json_bytes(dict(dataset)))


def ner_weight_digest(model_path: str | None) -> str:
    """Fingerprint NER weights so a silent model swap cannot look identical."""
    raw = str(model_path or "").strip()
    if not raw:
        return ""
    root = Path(raw)
    if not root.exists():
        return ""
    digest = hashlib.sha256()
    if root.is_file():
        digest.update(root.read_bytes())
        return "sha256:" + digest.hexdigest()
    entries: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        st = path.stat()
        rel = path.relative_to(root).as_posix()
        if st.st_size <= 2_000_000:
            digest.update(rel.encode("utf-8"))
            digest.update(path.read_bytes())
        else:
            digest.update(f"{rel}:{st.st_size}".encode("utf-8"))
        entries.append(rel)
    if not entries:
        return ""
    return "sha256:" + digest.hexdigest()


def collect_scan_inventory(svc: Any, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """NER / regex / expander / threshold facts for eval provenance."""
    opts = dict(options or {})
    extra: dict[str, Any] = {
        "engines": opts.get("engines") or "",
        "min_score": float(opts.get("min_score") or 0.35),
        "preprocess_expanders": list(opts.get("preprocess_expanders") or []),
        "preprocess_obfuscation": bool(opts.get("preprocess_obfuscation", True)),
        "overlap_iou": float(opts.get("overlap_iou") or 0.5),
    }
    ruleset = getattr(svc, "_ruleset", None)
    if ruleset is not None:
        patterns = getattr(ruleset, "patterns", None) or {}
        extra["regex_inventory"] = sorted(str(name) for name in patterns)
        extra["ner_labels"] = list(getattr(ruleset, "ner_labels", ()) or ())
        thresholds = getattr(ruleset, "thresholds", None)
        if thresholds is not None:
            extra["thresholds"] = {
                name: getattr(thresholds, name)
                for name in (
                    "presidio_min", "gliner_min", "llm_min", "phone_min",
                    "nid_min", "imei_min", "imsi_min",
                )
                if hasattr(thresholds, name)
            }
        overlay = getattr(ruleset, "text_rules", None)
        if overlay is not None and hasattr(overlay, "to_dict"):
            extra["text_rules_keys"] = sorted(overlay.to_dict().keys())
    ner = getattr(svc, "_ner", None)
    if ner is not None:
        extra["ner_backend"] = str(getattr(ner, "name", "") or type(ner).__name__)
        model_path = str(getattr(ner, "_model_path", "") or getattr(ner, "model_path", "") or "")
        extra["ner_model_id"] = model_path or extra["ner_backend"]
        extra["ner_model_sha256"] = ner_weight_digest(model_path)
    return extra


def comparable_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Drop run-specific fields so two machines can assert identical scores."""
    skip = {"generated_at", "timestamp", "started_at", "finished_at"}
    out = _strip(report, skip)
    return _strip_run_identity(out)


def _strip(value: Any, skip: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip(v, skip) for k, v in value.items() if k not in skip}
    if isinstance(value, list):
        return [_strip(v, skip) for v in value]
    return value


def _is_provenance_block(value: Mapping[str, Any]) -> bool:
    return any(
        key in value
        for key in ("rules_checksum", "rules_source", "normalization_profile", "stack_uuid")
    ) or ("run_uuid" in value and "redibis_version" in value)


def _strip_run_identity(value: Any, *, in_provenance: bool = False) -> Any:
    if isinstance(value, Mapping):
        provenance = in_provenance or _is_provenance_block(value)
        out: dict[str, Any] = {}
        for key, item in value.items():
            if provenance and key in {"run_uuid", "label"}:
                continue
            out[key] = _strip_run_identity(item, in_provenance=(key == "provenance" or provenance))
        return out
    if isinstance(value, list):
        return [_strip_run_identity(v, in_provenance=in_provenance) for v in value]
    return value


def base_provenance(
    *,
    run_uuid: str,
    label: str = "",
    rules_checksum_value: str = "",
    rules_source: str = "",
    normalization_profile: str = "",
    pack_stack: Optional[Mapping[str, Any]] = None,
    tiers: Optional[list[str]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    unpinned = str(rules_source or "").startswith("config+stored")
    block: dict[str, Any] = {
        "run_uuid": run_uuid,
        "label": str(label or ""),
        "redibis_version": _redibis_version(),
        "rules_checksum": rules_checksum_value,
        "rules_source": rules_source,
        "rules_unpinned": unpinned,
        "normalization_profile": normalization_profile,
        "pack_stack": dict(pack_stack) if pack_stack else None,
        "tiers": list(tiers or ("strict", "value", "overlap", "type")),
    }
    if unpinned:
        block["warning"] = (
            "rules_source is config+stored (unpinned). Pin with --rules or "
            "--rules-defaults before comparing machines."
        )
    if extra:
        block.update(dict(extra))
    return block
