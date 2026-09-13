"""Pin eval runs to an explicit rule overlay and optional pack."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from redibis.pii.eval.provenance import rules_checksum
from redibis.pii.text_rules import TextRuleOverlay, compile_text_rules, default_text_rules, merge_text_rules


class EvalPinError(ValueError):
    """Raised when eval rules or pack pinning cannot be resolved."""


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise EvalPinError("PyYAML is required to load eval rule files") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise EvalPinError(f"cannot read {path}: {exc}") from exc
    except Exception as exc:
        raise EvalPinError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalPinError(f"{path} must be a YAML mapping")
    return data


def load_text_rules_file(path: Path) -> tuple[TextRuleOverlay, bool]:
    """Return (overlay, replace_defaults)."""
    raw = _load_yaml_mapping(path)
    replace = bool(raw.get("replace_defaults"))
    overlay = TextRuleOverlay.from_dict(raw)
    return overlay, replace


def resolve_text_rules(
    *,
    rules_path: str | None = None,
    rules_defaults: bool = False,
    draft_rules_path: str | None = None,
    config_overlay: Any = None,
    stored_overlay: Any = None,
) -> tuple[TextRuleOverlay, str, bool]:
    """Return (overlay, rules_source, merge_builtin).

    ``merge_builtin`` tells ``RuleSetCompiler.default`` whether to fold
    shipped defaults under the overlay.
    """
    draft: TextRuleOverlay | None = None
    if draft_rules_path:
        draft_overlay, _ = load_text_rules_file(Path(draft_rules_path))
        draft = draft_overlay

    if rules_path:
        overlay, replace = load_text_rules_file(Path(rules_path))
        source = f"file:{Path(rules_path).as_posix()}"
        if draft is not None:
            overlay = overlay.merge(draft)
            source = source + "+draft"
        return overlay, source, not replace

    if rules_defaults:
        overlay = default_text_rules()
        source = "defaults"
        merge_builtin = False
        if draft is not None:
            overlay = overlay.merge(draft)
            source = "defaults+draft"
        return overlay, source, merge_builtin

    operator = merge_text_rules(config_overlay, stored_overlay)
    source = "config+stored" if (config_overlay or stored_overlay) else "defaults"
    if draft is not None:
        operator = operator.merge(draft)
        source = source + "+draft"
    return operator, source, True


def resolve_pack_stack(
    *,
    pack: str | None = None,
    pack_stack: str | None = None,
    redibis_config: Any = None,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Return (AppliedPackStack|None, pack_stack provenance dict)."""
    from redibis.pack.resolver import apply_packs, pack_stack_for_evidence
    from redibis.pack.stack_models import AppliedPackStack

    paths: list[str] = []
    if pack_stack:
        stack_path = Path(pack_stack)
        if stack_path.is_file():
            raw = _load_yaml_mapping(stack_path)
            layers = raw.get("layers") or raw.get("packs") or raw.get("paths") or []
            if isinstance(layers, list):
                for item in layers:
                    if isinstance(item, str) and item.strip():
                        paths.append(item.strip())
                    elif isinstance(item, Mapping) and item.get("path"):
                        paths.append(str(item["path"]))
        elif stack_path.is_dir():
            paths.append(str(stack_path))
        else:
            raise EvalPinError(f"pack stack not found: {pack_stack}")
    if pack:
        resolved = _resolve_pack_ref(pack, redibis_config=redibis_config)
        paths.append(resolved)

    if not paths:
        return None, None

    from redibis.config import RedibisConfig

    base = redibis_config or RedibisConfig()
    try:
        stack = apply_packs(base, paths)
    except Exception as exc:
        raise EvalPinError(f"cannot load pack(s): {exc}") from exc
    if not isinstance(stack, AppliedPackStack):
        return None, None
    try:
        header = pack_stack_for_evidence(stack)
    except Exception as exc:
        header = {
            "stack_uuid": "",
            "packs": [{"id": p, "path": p} for p in paths],
            "provenance_degraded": True,
            "provenance_degraded_reason": f"pack identity failed: {exc}",
        }
    return stack, header


def _pack_store_from_config(redibis_config: Any | None):
    from redibis.store.pack_store import PackStore
    from redibis.store.storage_backend import LocalBackend

    root = Path(getattr(redibis_config, "output_dir", None) or "./reports") / "_dev_storage"
    return PackStore(LocalBackend(str(root)), bucket="pii-reports")


def _resolve_pack_ref(ref: str, *, redibis_config: Any | None = None) -> str:
    raw = str(ref or "").strip()
    if not raw:
        raise EvalPinError("pack ref is empty")
    path = Path(raw)
    if path.exists():
        return str(path)
    if "@" in raw:
        ident, version = raw.rsplit("@", 1)
        ident, version = ident.strip(), version.strip()
        try:
            store = _pack_store_from_config(redibis_config)
            refs = store.list()
        except Exception as exc:
            raise EvalPinError(
                f"pack {raw!r} is not a filesystem path and the pack store is unavailable "
                f"(id@version requires a published pack): {exc}"
            ) from exc
        matches = [
            r for r in refs
            if r.version == version and (r.id == ident or r.family_id == ident)
        ]
        if not matches:
            raise EvalPinError(
                f"pack {raw!r} not found as a path or as id@version / family_id@version in the pack store"
            )
        chosen = matches[0]
        import tempfile

        data = store.get(chosen.uuid)
        dest_dir = Path(tempfile.mkdtemp(prefix="redibis-eval-pack-"))
        dest = dest_dir / f"{chosen.uuid}.zip"
        dest.write_bytes(data)
        return str(dest)
    raise EvalPinError(
        f"pack not found: {raw} (pass a pack directory/archive path, or id@version from the pack store)"
    )


def effective_overlay_and_checksum(
    overlay: TextRuleOverlay,
    *,
    merge_builtin: bool,
) -> tuple[TextRuleOverlay, str]:
    effective = compile_text_rules(overlay) if merge_builtin else overlay
    return effective, rules_checksum(effective)
