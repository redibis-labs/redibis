"""Rule-level diff between two published pack versions (PLAN §5.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from redibis.pack.archive import detect_archive_kind, read_tar_files, read_zip_files
from redibis.pack.canonical import MANIFEST_NAME, parse_yaml_bytes
from redibis.pack.loader import load_pack, pack_files_from_loaded

PathLike = Union[str, Path]


def _files_from_archive(path: Path) -> dict[str, bytes]:
    kind = detect_archive_kind(path)
    if kind == "zip":
        return read_zip_files(path, manifest_name=MANIFEST_NAME)
    if kind in ("tar", "tar.gz"):
        return read_tar_files(path)
    pack = load_pack(path)
    return pack_files_from_loaded(pack)


def _yaml(data: bytes | None) -> Any:
    if not data:
        return None
    try:
        return parse_yaml_bytes(data)
    except Exception:
        return None


def _regex_inventory(files: Mapping[str, bytes]) -> dict[str, Any]:
    raw = files.get("locale/regex.yaml") or files.get("locale/regex.yml")
    doc = _yaml(raw)
    if not isinstance(doc, dict):
        return {}
    add = doc.get("add") or {}
    if isinstance(add, dict):
        return {str(k): v for k, v in add.items()}
    if isinstance(add, list):
        out: dict[str, Any] = {}
        for i, item in enumerate(add):
            if isinstance(item, dict) and item.get("name"):
                out[str(item["name"])] = item
            else:
                out[str(i)] = item
        return out
    return {}


def _ner_inventory(files: Mapping[str, bytes]) -> dict[str, Any]:
    raw = files.get("ner/models.yaml") or files.get("ner/models.yml")
    doc = _yaml(raw)
    if not isinstance(doc, dict):
        return {}
    labels = doc.get("labels")
    phrases = doc.get("phrases") if isinstance(doc.get("phrases"), dict) else {}
    out: dict[str, Any] = {}
    if isinstance(labels, list) and labels:
        for lab in labels:
            key = str(lab)
            out[key] = phrases.get(lab, True) if phrases else True
        return out
    if phrases:
        return {str(k): v for k, v in phrases.items()}
    return {}


def _custom_rules_inventory(files: Mapping[str, bytes]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rel, data in files.items():
        if not rel.startswith("classification/packs/"):
            continue
        if not rel.endswith((".yaml", ".yml")):
            continue
        doc = _yaml(data)
        if not isinstance(doc, dict):
            continue
        for i, rule in enumerate(doc.get("edge_rules") or []):
            if not isinstance(rule, dict):
                continue
            rid = str(rule.get("id") or rule.get("name") or f"{rel}:{i}")
            out[rid] = rule
    return out


def _validators_inventory(files: Mapping[str, bytes]) -> dict[str, Any]:
    doc = _yaml(files.get(MANIFEST_NAME) or files.get("pack.yaml"))
    if not isinstance(doc, dict):
        return {}
    requires = doc.get("requires") if isinstance(doc.get("requires"), dict) else {}
    registries = requires.get("registries") if isinstance(requires.get("registries"), dict) else {}
    validators = registries.get("validators") or []
    if isinstance(validators, list):
        return {str(v): v for v in validators}
    if isinstance(validators, dict):
        return {str(k): v for k, v in validators.items()}
    return {}


def _thresholds_inventory(files: Mapping[str, bytes]) -> dict[str, Any]:
    raw = files.get("config/redibis.yaml") or files.get("config/redibis.yml")
    doc = _yaml(raw)
    if not isinstance(doc, dict):
        return {}
    pii = doc.get("pii") if isinstance(doc.get("pii"), dict) else {}
    thresholds = pii.get("thresholds") or doc.get("thresholds") or {}
    return dict(thresholds) if isinstance(thresholds, dict) else {}


def _canon(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except TypeError:
        return repr(value)


def _section_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, list[str]]:
    left_keys, right_keys = set(left), set(right)
    added = sorted(right_keys - left_keys)
    removed = sorted(left_keys - right_keys)
    changed = sorted(
        k for k in (left_keys & right_keys) if _canon(left[k]) != _canon(right[k])
    )
    return {"added": added, "removed": removed, "changed": changed}


def inventory_from_files(files: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        "regex_patterns": _regex_inventory(files),
        "custom_rules": _custom_rules_inventory(files),
        "ner_entities": _ner_inventory(files),
        "validators": _validators_inventory(files),
        "thresholds": _thresholds_inventory(files),
    }


def diff_inventories(
    left: Mapping[str, dict[str, Any]],
    right: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, list[str]]]:
    keys = ("regex_patterns", "custom_rules", "ner_entities", "validators", "thresholds")
    return {
        key: _section_diff(dict(left.get(key) or {}), dict(right.get(key) or {}))
        for key in keys
    }


def diff_pack_files(
    left_files: Mapping[str, bytes],
    right_files: Mapping[str, bytes],
) -> dict[str, dict[str, list[str]]]:
    return diff_inventories(inventory_from_files(left_files), inventory_from_files(right_files))


def diff_pack_paths(left: PathLike, right: PathLike) -> dict[str, dict[str, list[str]]]:
    return diff_pack_files(_files_from_archive(Path(left)), _files_from_archive(Path(right)))


def format_pack_diff(
    diff: Mapping[str, Mapping[str, list[str]]],
    *,
    left_label: str = "a",
    right_label: str = "b",
) -> str:
    lines = [f"pack diff  {left_label} → {right_label}"]
    labels = {
        "regex_patterns": "regex patterns",
        "custom_rules": "custom rules",
        "ner_entities": "NER entities",
        "validators": "validators",
        "thresholds": "thresholds",
    }
    any_change = False
    for key, title in labels.items():
        section = diff.get(key) or {}
        added = list(section.get("added") or [])
        removed = list(section.get("removed") or [])
        changed = list(section.get("changed") or [])
        if not (added or removed or changed):
            continue
        any_change = True
        lines.append(f"  {title}:")
        for name in added:
            lines.append(f"    + {name}")
        for name in removed:
            lines.append(f"    - {name}")
        for name in changed:
            lines.append(f"    ~ {name}")
    if not any_change:
        lines.append("  (no rule-level changes)")
    return "\n".join(lines)
