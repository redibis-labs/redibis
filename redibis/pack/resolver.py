"""Single merge point: resolve packs into effective RedibisConfig objects."""

from __future__ import annotations

import copy
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence, Union

from redibis.config import RedibisConfig, deep_merge
from redibis.pack.config_allowlist import ALLOWED_CONFIG_TOP_LEVEL, extract_portable_config
from redibis.pack.defaults import DEFAULT_PACK_ID, build_default_pack_files
from redibis.pack.errors import PackLoadError, PackValidationError
from redibis.pack.identity import (
    compute_stack_sha256,
    compute_stack_uuid,
    count_pack_contents,
    infer_pack_kind,
)
from redibis.pack.loader import load_pack
from redibis.pack.models import LoadedPack
from redibis.pack.stack_models import AppliedPackStack, PackLayerRef
from redibis.pii.regex_overrides import RegexOverrides

PathLike = Union[str, Path]

if TYPE_CHECKING:
    from redibis.store.pack_store import PackStore


def merge_regex_overrides(
    base: Optional[RegexOverrides],
    overlay: RegexOverrides,
) -> RegexOverrides:
    """Compose RegexOverrides layers (add unions, remove subtracts, replace_all truncates)."""
    if overlay.replace_all:
        add = dict(overlay.add)
        for name in overlay.remove:
            add.pop(name, None)
        return RegexOverrides(add=add, remove=list(overlay.remove), replace_all=True)

    base = base or RegexOverrides()
    if base.replace_all:
        add = dict(base.add)
        add.update(overlay.add)
        remove = list(dict.fromkeys([*base.remove, *overlay.remove]))
        for name in overlay.remove:
            add.pop(name, None)
        return RegexOverrides(add=add, remove=remove, replace_all=True)

    add = dict(base.add)
    add.update(overlay.add)
    remove = list(dict.fromkeys([*base.remove, *overlay.remove]))
    for name in overlay.remove:
        add.pop(name, None)
    return RegexOverrides(add=add, remove=remove, replace_all=False)


def _normalize_pattern_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key == "name":
            continue
        if isinstance(value, (tuple, list)):
            out[key] = list(value)
        else:
            out[key] = value
    return out


def is_builtin_catalog_snapshot(overrides: Optional[RegexOverrides]) -> bool:
    """True when overrides are an exact ``replace_all`` dump of shipped CATALOG.

    Applying the default pack must not rewrite ``pii.regex_overrides`` away from
    ``None`` when the pack only re-ships the engine catalog.
    """
    if overrides is None or not overrides.replace_all or overrides.remove:
        return False
    from redibis.pii.regex_catalog import CATALOG, _entry_to_dict

    if set(overrides.add) != set(CATALOG):
        return False
    for name, entry in CATALOG.items():
        expected = _normalize_pattern_entry(_entry_to_dict(name, entry))
        got = _normalize_pattern_entry(overrides.add.get(name) or {})
        if got != expected:
            return False
    return True


def _apply_config_layer(
    cfg: RedibisConfig,
    pack_cfg: Mapping[str, Any],
    *,
    mode: str,
) -> RedibisConfig:
    current = cfg.to_dict()
    overlay = dict(pack_cfg)
    if mode == "replace":
        defaults = extract_portable_config(RedibisConfig.default())
        for key in ALLOWED_CONFIG_TOP_LEVEL:
            current[key] = copy.deepcopy(defaults.get(key))
        current = deep_merge(current, overlay)
    else:
        current = deep_merge(current, overlay)
    return RedibisConfig.from_dict(current)


def _layer_from_loaded(
    pack: LoadedPack,
    *,
    mode: Optional[str] = None,
    path: Optional[str] = None,
    source: str = "path",
) -> PackLayerRef:
    md = pack.manifest.metadata
    size = 0
    if path:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = sum(len(item.data) for item in pack.files.values())
    else:
        size = sum(len(item.data) for item in pack.files.values())
    return PackLayerRef(
        id=md.id,
        version=md.version,
        checksum=pack.pack_sha256,
        mode=mode or pack.manifest.mode,
        path=path,
        source=source,
        uuid=md.uuid or "",
        family_id=md.family_id or "",
        parent_uuid=md.parent_uuid,
        author=md.author or "",
        kind=infer_pack_kind(pack.manifest.contents, pack_id=md.id),
        size_bytes=size,
        published_at=md.created or "",
        signature=pack.signature_status,
        contents_summary=count_pack_contents(
            {rel: item.data for rel, item in pack.files.items()},
            pack.manifest,
        ),
    )


def _apply_loaded_pack(
    stack: AppliedPackStack,
    pack: LoadedPack,
    *,
    mode: Optional[str] = None,
    path: Optional[str] = None,
    source: str = "path",
) -> AppliedPackStack:
    from dataclasses import replace

    from redibis.pii.context_tokens import merge_context_tokens, parse_tokens_document

    effective_mode = mode or pack.manifest.mode or "overlay"
    layer = _layer_from_loaded(pack, mode=effective_mode, path=path, source=source)

    if pack.manifest.contents.config and "config/redibis.yaml" in pack.files:
        pack_cfg = pack.yaml("config/redibis.yaml") or {}
        if not isinstance(pack_cfg, dict):
            raise PackValidationError(
                "config/redibis.yaml must be a mapping",
                errors=["config/redibis.yaml must be a mapping"],
            )
        stack.config = _apply_config_layer(stack.config, pack_cfg, mode=effective_mode)

    regex_path = None
    if "locale/regex.yaml" in pack.files:
        regex_path = "locale/regex.yaml"
    elif "locale/regex.yml" in pack.files:
        regex_path = "locale/regex.yml"
    if regex_path:
        raw = pack.yaml(regex_path) or {}
        if not isinstance(raw, dict):
            raise PackValidationError(
                f"{regex_path} must be a mapping",
                errors=[f"{regex_path} must be a mapping"],
            )
        overlay = RegexOverrides.from_dict(raw)
        stack.regex_overrides = merge_regex_overrides(stack.regex_overrides, overlay)
        pii = stack.config.pii
        existing = None
        if pii.regex_overrides:
            existing = RegexOverrides.from_dict(pii.regex_overrides)
        merged = merge_regex_overrides(existing, overlay)
        # Identity catalog dump (default pack) stays as engine built-in — do not
        # materialize a redundant replace_all blob onto RedibisConfig.pii.
        if is_builtin_catalog_snapshot(merged) and not existing:
            stack.regex_overrides = None
        else:
            stack.config = replace(
                stack.config,
                pii=replace(pii, regex_overrides=merged.to_dict()),
            )

    tokens_path = None
    if "locale/tokens.yaml" in pack.files:
        tokens_path = "locale/tokens.yaml"
    elif "locale/tokens.yml" in pack.files:
        tokens_path = "locale/tokens.yml"
    if tokens_path:
        try:
            overlay_tokens, remove, norms = parse_tokens_document(pack.yaml(tokens_path))
        except ValueError as exc:
            raise PackValidationError(str(exc), errors=[str(exc)]) from exc
        stack.context_tokens = merge_context_tokens(
            stack.context_tokens, overlay_tokens, remove=remove
        )
        if norms:
            stack.token_normalizers = tuple(norms)

    phone_path = None
    if "locale/phone.yaml" in pack.files:
        phone_path = "locale/phone.yaml"
    elif "locale/phone.yml" in pack.files:
        phone_path = "locale/phone.yml"
    if phone_path:
        phone = pack.yaml(phone_path) or {}
        if not isinstance(phone, dict):
            raise PackValidationError(
                f"{phone_path} must be a mapping",
                errors=[f"{phone_path} must be a mapping"],
            )
        stack.phone_locale = {**stack.phone_locale, **phone}
        pii = stack.config.pii
        regions = phone.get("default_regions") or []
        prefixes = phone.get("msisdn_prefixes")
        updates: dict[str, Any] = {}
        if regions:
            updates["default_region"] = str(regions[0])
        if prefixes is not None:
            updates["msisdn_prefixes"] = [str(p) for p in prefixes]
        if phone.get("geofence") is None and "geofence" in phone:
            updates["geo_egypt_geofence"] = False
        elif phone.get("geofence") == "egypt":
            updates["geo_egypt_geofence"] = True
        if updates:
            stack.config = replace(stack.config, pii=replace(pii, **updates))

    ner_path = None
    if "ner/models.yaml" in pack.files:
        ner_path = "ner/models.yaml"
    elif "ner/models.yml" in pack.files:
        ner_path = "ner/models.yml"
    if ner_path:
        ner_doc = pack.yaml(ner_path) or {}
        if isinstance(ner_doc, dict):
            phrases = ner_doc.get("phrases") or {}
            if isinstance(phrases, dict):
                stack.ner_phrases = {**stack.ner_phrases, **{str(k): str(v) for k, v in phrases.items()}}
            floor = ner_doc.get("gliner_min")
            if floor is not None:
                pii = stack.config.pii
                thr = pii.thresholds
                from dataclasses import replace as _replace

                stack.config = _replace(
                    stack.config,
                    pii=_replace(pii, thresholds=_replace(thr, gliner_min=float(floor))),
                )

    for rel, _item in pack.files.items():
        if rel.startswith("quality/rulesets/") and rel.endswith((".yaml", ".yml")):
            stack.quality_rulesets[Path(rel).stem] = pack.yaml(rel)
        if rel.startswith("masking/plans/") and rel.endswith((".yaml", ".yml")):
            stack.masking_plans[Path(rel).stem] = pack.yaml(rel)
        if rel.startswith("classification/packs/") and rel.endswith((".yaml", ".yml")):
            stack.classification_packs[Path(rel).stem] = pack.yaml(rel)
        if rel.startswith("assets/prompts/"):
            text = pack.text(rel)
            if text is not None:
                stack.prompt_templates[Path(rel).name] = text
        if rel.startswith("behavior/") and rel.endswith((".yaml", ".yml")):
            stack.behavior_policy_paths.append(rel)

    stack.layers.append(layer)
    stack.warnings.extend(pack.warnings)
    return stack


def builtin_default_layer() -> PackLayerRef:
    """Content-addressed ref for the in-process shipped defaults (no archive)."""
    from redibis import __version__
    from redibis.pack.defaults import build_default_manifest
    from redibis.pack.identity import backfill_uuid, ensure_pack_identity, infer_family_id

    files, sha = build_default_pack_files()
    manifest = build_default_manifest(version=__version__, sections=files)
    ensure_pack_identity(manifest, sha)
    md = manifest.metadata
    family_id = md.family_id or infer_family_id(
        author=md.author or "redibis",
        kind="default",
        pack_id=DEFAULT_PACK_ID,
    )
    uid = md.uuid or backfill_uuid(family_id, __version__, sha)
    return PackLayerRef(
        id=DEFAULT_PACK_ID,
        version=__version__,
        checksum=sha,
        mode="overlay",
        path=None,
        source="builtin",
        uuid=uid,
        family_id=family_id,
        parent_uuid=md.parent_uuid,
        author=md.author or "redibis",
        kind="default",
        size_bytes=sum(len(b) for b in files.values()),
        published_at=md.created or "",
        signature=None,
        contents_summary=count_pack_contents(files, manifest),
    )


def apply_packs(
    base: RedibisConfig,
    paths: Sequence[PathLike] | None = None,
    *,
    include_builtin_default: bool = True,
    modes: Mapping[str, str] | None = None,
    seed_builtin_tokens: bool = True,
) -> AppliedPackStack:
    """Resolve packs onto ``base`` into one effective config + layer audit.

    ``include_builtin_default`` records the shipped defaults as layer 0 without
    re-merging them (``base`` is already those defaults unless customized).

    ``seed_builtin_tokens`` loads today's EN+AR catalog tokens as the locale
    base layer (ar-EG-equivalent) so overlays union onto current behavior.
    """
    from redibis.pii.context_tokens import builtin_context_tokens

    stack = AppliedPackStack(config=base)
    if seed_builtin_tokens:
        stack.context_tokens = builtin_context_tokens()
    if include_builtin_default:
        stack.layers.append(builtin_default_layer())

    mode_by_path = {str(Path(k)): v for k, v in (modes or {}).items()}
    for raw in paths or []:
        path = Path(raw).expanduser()
        if not path.exists():
            raise PackLoadError(f"pack path not found: {path}")
        pack = load_pack(path)
        stack = _apply_loaded_pack(
            stack,
            pack,
            mode=mode_by_path.get(str(path)),
            path=str(path),
            source="path",
        )
    return stack


def apply_packs_from_config(cfg: RedibisConfig) -> AppliedPackStack:
    """Apply ``cfg.packs`` path list (deployment wiring)."""
    refs = getattr(cfg, "packs", None) or []
    paths: list[str] = []
    modes: dict[str, str] = {}
    for item in refs:
        if isinstance(item, Mapping):
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            paths.append(path)
            mode = str(item.get("mode") or "overlay").strip() or "overlay"
            modes[str(Path(path).expanduser())] = mode
        else:
            path = str(getattr(item, "path", "") or "").strip()
            if not path:
                continue
            paths.append(path)
            mode = str(getattr(item, "mode", "overlay") or "overlay")
            modes[str(Path(path).expanduser())] = mode
    # Start from cfg with packs list cleared of merge side-effects? Keep as-is.
    return apply_packs(cfg, paths, include_builtin_default=True, modes=modes)


def _signature_for_evidence(layer: PackLayerRef, stored_sig: Any) -> Any:
    if isinstance(stored_sig, dict):
        return stored_sig
    if isinstance(layer.signature, dict):
        return dict(layer.signature)
    return None


def _available_locally(layer: PackLayerRef, store: "PackStore | None") -> bool:
    if store is not None and layer.uuid:
        return store.head(layer.uuid) is not None
    if layer.source == "builtin":
        return True
    if layer.path:
        return Path(layer.path).expanduser().exists()
    return False


def pack_stack_for_evidence(
    resolved_stack: AppliedPackStack,
    store: "PackStore | None" = None,
) -> dict[str, Any]:
    """Header block per PLAN §3 ``header.provenance.pack_stack``."""
    layers = list(resolved_stack.layers)
    applied = [L for L in layers if L.source != "builtin"] or layers
    mode = (applied[-1].mode if applied else "overlay") or "overlay"

    packs_out: list[dict[str, Any]] = []
    for layer in layers:
        stored = store.head(layer.uuid) if store is not None and layer.uuid else None
        uuid_value = (stored.uuid if stored else layer.uuid) or ""
        family_id = (stored.family_id if stored else layer.family_id) or ""
        sha = (stored.sha256 if stored else layer.checksum) or ""
        size = stored.size_bytes if stored is not None else layer.size_bytes
        author = (stored.author if stored else layer.author) or ""
        published_at = (stored.published_at if stored else layer.published_at) or ""
        parent_uuid = stored.parent_uuid if stored is not None else layer.parent_uuid
        kind = ""
        if stored is not None and stored.kind:
            kind = stored.kind
        elif layer.kind:
            kind = layer.kind
        else:
            kind = "default" if layer.id == DEFAULT_PACK_ID else "pack"
        contents = dict(stored.contents) if stored is not None else dict(layer.contents_summary)
        signature = _signature_for_evidence(
            layer, stored.signature if stored is not None else None
        )
        packs_out.append(
            {
                "uuid": uuid_value,
                "family_id": family_id,
                "kind": kind,
                "id": (stored.id if stored else layer.id),
                "version": (stored.version if stored else layer.version),
                "parent_uuid": parent_uuid,
                "sha256": sha,
                "size_bytes": int(size or 0),
                "published_at": published_at,
                "author": author,
                "signature": signature,
                "contents": contents,
                "available_locally": _available_locally(layer, store),
            }
        )

    @dataclass
    class _StackItem:
        uuid: str
        sha256: str

    stack_refs = [_StackItem(uuid=p["uuid"], sha256=p["sha256"]) for p in packs_out]
    return {
        "stack_uuid": compute_stack_uuid(stack_refs, mode=mode),
        "stack_sha256": compute_stack_sha256(stack_refs, mode=mode),
        "mode": mode,
        "packs": packs_out,
    }
