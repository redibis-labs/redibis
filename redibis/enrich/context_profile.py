"""Resolved enrichment context profiles (normal vs multistep) and custom overlays."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from redibis.enrich.prompt_store import (
    PACK_ENRICH_PROMPT_PREFIX,
    EnrichPromptStore,
    get_enrich_prompt_store,
)
from redibis.enrich.workflow import LLM_STAGE_KINDS

PathLike = Union[str, Path]

PROFILE_MODES = ("shared", "normal", "multistep")
ALLOWED_CONTEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml"}
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
MAX_CONTEXT_FILE_BYTES = 200_000
MANIFEST_NAME = "context-manifest.yaml"
PACK_MANIFEST_NAME = "manifest.yaml"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def default_custom_dir() -> Path:
    env = os.environ.get("REDIBIS_ENRICH_CONTEXT_DIR")
    if env:
        return Path(env).expanduser()
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "enrich-context" / "custom"


def _validate_overlay_name(name: str) -> str:
    n = (name or "").strip().replace("\\", "/")
    if n.startswith("/") or ".." in n.split("/") or n.startswith("../"):
        raise ValueError(f"invalid context filename: {name!r}")
    n = n.split("/")[-1]
    stem, _, suffix = n.rpartition(".")
    if not stem or f".{suffix.lower()}" not in ALLOWED_CONTEXT_SUFFIXES:
        raise ValueError(f"invalid context filename: {name!r}")
    if not _SAFE_FILE.match(n):
        raise ValueError(f"invalid context filename: {name!r}")
    return n


def overlay_relpath(mode: str, filename: str, *, stage: str = "") -> str:
    """Relative path under the custom overlay root or table context prefix."""
    safe = _validate_overlay_name(filename)
    mode_n = (mode or "shared").strip().lower()
    stage_n = (stage or "").strip().lower()
    if mode_n not in PROFILE_MODES:
        raise ValueError(f"unknown context mode {mode_n!r}; allowed: {list(PROFILE_MODES)}")
    if stage_n and stage_n not in LLM_STAGE_KINDS:
        raise ValueError(
            f"unknown workflow step kind {stage_n!r}; allowed: {sorted(LLM_STAGE_KINDS)}"
        )
    if mode_n == "shared":
        if stage_n:
            raise ValueError("--stage is only valid with --mode multistep")
        return f"shared/{safe}"
    if mode_n == "normal":
        if stage_n:
            raise ValueError("--stage is only valid with --mode multistep")
        return f"normal/{safe}"
    if stage_n:
        return f"multistep/steps/{stage_n}/{safe}"
    return f"multistep/shared/{safe}"


def _iter_overlay_files(root: Path) -> list[tuple[str, str]]:
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_CONTEXT_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        if ".." in rel.split("/"):
            continue
        try:
            text = _normalize_text(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        out.append((rel, text))
    return out


def _select_overlay_rels(
    rels: list[tuple[str, str]],
    *,
    mode: str,
    stage_kind: Optional[str] = None,
) -> list[tuple[str, str]]:
    mode_n = (mode or "normal").strip().lower()
    kind = (stage_kind or "").strip().lower()
    selected: list[tuple[str, str]] = []
    for rel, text in rels:
        n = rel.replace("\\", "/")
        if n.startswith("shared/") and n.count("/") == 1:
            selected.append((n, text))
            continue
        if mode_n == "normal" and n.startswith("normal/") and n.count("/") == 1:
            selected.append((n, text))
        elif mode_n == "multistep":
            if n.startswith("multistep/shared/") and n.count("/") == 2:
                selected.append((n, text))
            if kind and n.startswith(f"multistep/steps/{kind}/"):
                selected.append((n, text))
    return selected


@dataclass
class ContextFile:
    relpath: str
    text: str
    sha256: str
    source: str


@dataclass
class ResolvedContextProfile:
    files: list[ContextFile] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    include_custom: bool = False
    pack_identity: str = ""
    pack_version: str = ""
    pack_sha256: str = ""

    def files_for(self, *, mode: str, stage_kind: Optional[str] = None) -> list[ContextFile]:
        mode_n = (mode or "normal").strip().lower()
        kind = (stage_kind or "").strip().lower()
        out: list[ContextFile] = []
        for item in self.files:
            n = item.relpath
            if mode_n == "normal" and n.startswith("normal/"):
                out.append(item)
            elif mode_n == "multistep":
                if n.startswith("multistep/shared/"):
                    out.append(item)
                if kind and n.startswith(f"multistep/steps/{kind}/"):
                    out.append(item)
        return out

    def compose(self, *, mode: str = "normal", stage_kind: Optional[str] = None) -> str:
        parts = [f.text.strip() for f in self.files_for(mode=mode, stage_kind=stage_kind) if f.text.strip()]
        return ("\n\n".join(parts).rstrip() + "\n") if parts else ""

    def to_manifest(self) -> dict[str, Any]:
        return {
            "apiVersion": "redibis.context-profile/v1",
            "kind": "EnrichmentContextProfile",
            "include_custom": self.include_custom,
            "pack": {
                "identity": self.pack_identity,
                "version": self.pack_version,
                "sha256": self.pack_sha256,
            },
            "sources": list(self.sources),
            "layer_order": [
                "pack_or_prompt_store",
                "custom_global",
            ] if self.include_custom else ["pack_or_prompt_store"],
            "stage_kinds": sorted(LLM_STAGE_KINDS),
            "files": [
                {
                    "path": f.relpath,
                    "sha256": f.sha256,
                    "source": f.source,
                    "chars": len(f.text),
                }
                for f in sorted(self.files, key=lambda x: x.relpath)
            ],
        }


def _prompt_store_files(store: EnrichPromptStore) -> list[ContextFile]:
    store.ensure_seeded()
    files: list[ContextFile] = []
    names = store.list_files()
    nested = [n for n in names if n.startswith("normal/") or n.startswith("multistep/")]
    chosen = nested if nested else [n for n in names if "/" not in n]
    for name in chosen:
        text = _normalize_text(store.read(name))
        rel = name if "/" in name else f"normal/{name}"
        # Flat-only stores also populate multistep from the same files so export
        # always has both folders.
        files.append(ContextFile(
            relpath=rel, text=text, sha256=_sha256_text(text), source="prompt_store",
        ))
    if not nested:
        # Duplicate flat files into multistep shared so the export contract is stable.
        extra: list[ContextFile] = []
        for item in files:
            shared = ContextFile(
                relpath=f"multistep/shared/{Path(item.relpath).name}",
                text=item.text,
                sha256=item.sha256,
                source="prompt_store",
            )
            extra.append(shared)
        files.extend(extra)
    return files


def _files_from_enrichment_pack(pack: Any) -> list[ContextFile]:
    files: list[ContextFile] = []
    prompt = pack.manifest.prompt
    mapping: list[tuple[str, list[str]]] = [
        ("normal", list(getattr(prompt, "normal", None) or [])),
        ("multistep/shared", list(getattr(prompt, "shared", None) or [])),
    ]
    stages = getattr(prompt, "stages", None) or {}
    if isinstance(stages, dict):
        for kind, paths in stages.items():
            mapping.append((f"multistep/steps/{kind}", list(paths or [])))
    else:
        for kind in LLM_STAGE_KINDS:
            paths = getattr(stages, kind, None) or []
            mapping.append((f"multistep/steps/{kind}", list(paths)))

    used: set[str] = set()
    for dest_prefix, paths in mapping:
        for src in paths:
            text = pack.text(src)
            if not text:
                continue
            name = Path(src).name
            rel = f"{dest_prefix}/{name}"
            body = _normalize_text(text)
            files.append(ContextFile(
                relpath=rel, text=body, sha256=_sha256_text(body), source="enrichment_pack",
            ))
            used.add(src)

    # Also pick up exported layout files sitting at pack-root normal/ and multistep/.
    for rel, asset in (pack.texts or {}).items():
        n = str(rel).replace("\\", "/")
        if n in used or n in {PACK_MANIFEST_NAME, MANIFEST_NAME}:
            continue
        if n.startswith("normal/") or n.startswith("multistep/"):
            body = _normalize_text(asset.text if hasattr(asset, "text") else str(asset))
            files.append(ContextFile(
                relpath=n, text=body, sha256=_sha256_text(body), source="enrichment_pack",
            ))
    return files


def _files_from_rdbpack(path: Path) -> list[ContextFile]:
    from redibis.pack import load_pack

    loaded = load_pack(path)
    files: list[ContextFile] = []
    for rel, item in loaded.files.items():
        n = str(rel).replace("\\", "/")
        if not n.startswith(PACK_ENRICH_PROMPT_PREFIX) or not n.endswith(".md"):
            continue
        rest = n[len(PACK_ENRICH_PROMPT_PREFIX) :]
        text = item.data.decode("utf-8") if isinstance(item.data, (bytes, bytearray)) else str(item.data)
        body = _normalize_text(text)
        relpath = rest if "/" in rest else f"normal/{rest}"
        files.append(ContextFile(
            relpath=relpath, text=body, sha256=_sha256_text(body), source="rdbpack",
        ))
    return files


def resolve_context_profile(
    *,
    pack_path: Optional[PathLike] = None,
    include_custom: bool = False,
    custom_dir: Optional[PathLike] = None,
    prompt_store: Optional[EnrichPromptStore] = None,
    configured_pack: str = "",
) -> ResolvedContextProfile:
    """Resolve the effective reusable context profile (not table-rendered prompts)."""
    sources: list[dict[str, Any]] = []
    files: list[ContextFile] = []
    pack_identity = ""
    pack_version = ""
    pack_sha256 = ""

    chosen = pack_path or configured_pack or None
    loaded_from_pack = False
    if chosen:
        p = Path(str(chosen)).expanduser()
        if p.exists():
            try:
                from redibis.enrich.packs.loader import load_enrichment_pack

                pack = load_enrichment_pack(p)
                files = _files_from_enrichment_pack(pack)
                if files:
                    loaded_from_pack = True
                    pack_identity = pack.manifest.release_identity
                    pack_version = pack.manifest.metadata.version
                    pack_sha256 = pack.sha256
                    sources.append({
                        "kind": "enrichment_pack",
                        "path": str(p),
                        "identity": pack_identity,
                        "sha256": pack_sha256,
                    })
            except Exception:
                loaded_from_pack = False
            if not loaded_from_pack:
                try:
                    files = _files_from_rdbpack(p)
                    if files:
                        loaded_from_pack = True
                        sources.append({"kind": "rdbpack", "path": str(p)})
                except Exception:
                    loaded_from_pack = False

    if not loaded_from_pack:
        store = prompt_store or get_enrich_prompt_store()
        files = _prompt_store_files(store)
        sources.append({"kind": "prompt_store", "root": str(store.enrich_dir)})

    # Same basename declared under two source paths collapses to one destination.
    deduped: dict[str, ContextFile] = {}
    for item in files:
        deduped[item.relpath] = item
    files = list(deduped.values())

    if include_custom:
        root = Path(custom_dir).expanduser() if custom_dir else default_custom_dir()
        overlays = _iter_overlay_files(root)
        by_rel = {f.relpath: i for i, f in enumerate(files)}
        for rel, text in overlays:
            item = ContextFile(
                relpath=rel, text=text, sha256=_sha256_text(text), source="custom_global",
            )
            if rel in by_rel:
                files[by_rel[rel]] = item
            else:
                by_rel[rel] = len(files)
                files.append(item)
        sources.append({"kind": "custom_global", "root": str(root)})

    files.sort(key=lambda f: f.relpath)
    return ResolvedContextProfile(
        files=files,
        sources=sources,
        include_custom=include_custom,
        pack_identity=pack_identity,
        pack_version=pack_version,
        pack_sha256=pack_sha256,
    )


def export_context_profile(
    dest: PathLike,
    *,
    pack_path: Optional[PathLike] = None,
    include_custom: bool = False,
    custom_dir: Optional[PathLike] = None,
    configured_pack: str = "",
    prompt_store: Optional[EnrichPromptStore] = None,
) -> dict[str, Any]:
    """Write the resolved two-folder profile plus manifests. Returns the manifest dict."""
    profile = resolve_context_profile(
        pack_path=pack_path,
        include_custom=include_custom,
        custom_dir=custom_dir,
        configured_pack=configured_pack,
        prompt_store=prompt_store,
    )
    dest_p = Path(dest).expanduser()
    dest_p.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for item in profile.files:
        path = dest_p / item.relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.text, encoding="utf-8", newline="\n")
        written.append(item.relpath)

    manifest = profile.to_manifest()
    (dest_p / MANIFEST_NAME).write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )

    # Enrichment-pack manifest so the export is a valid ``--pack DIR``.
    pack_manifest = _export_pack_manifest(profile)
    (dest_p / PACK_MANIFEST_NAME).write_text(
        yaml.safe_dump(pack_manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _export_pack_manifest(profile: ResolvedContextProfile) -> dict[str, Any]:
    # Mode-agnostic ``shared/`` overlays apply to both modes, and every written
    # file must be declared or the enrichment-pack loader rejects the directory.
    both = [f.relpath for f in profile.files if f.relpath.startswith("shared/")]
    normal = [f.relpath for f in profile.files if f.relpath.startswith("normal/")] + both
    shared = [
        f.relpath for f in profile.files if f.relpath.startswith("multistep/shared/")
    ] + both
    stages: dict[str, list[str]] = {}
    for kind in sorted(LLM_STAGE_KINDS):
        prefix = f"multistep/steps/{kind}/"
        paths = [f.relpath for f in profile.files if f.relpath.startswith(prefix)]
        if paths:
            stages[kind] = paths
    identity = profile.pack_identity or "redibis/context-profile"
    name = identity.split("/")[-1].split("@")[0] or "context-profile"
    publisher = identity.split("/")[0] if "/" in identity else "redibis"
    return {
        "apiVersion": "redibis.enrichment/v1",
        "kind": "EnrichmentPack",
        "metadata": {
            "publisherId": publisher,
            "name": name,
            "version": profile.pack_version or "0.0.0-export",
            "displayName": "Exported enrichment context profile",
            "description": "Resolved normal + multistep prompt profile",
            "license": "LicenseRef-Proprietary",
        },
        "compatibility": {
            "redibis": ">=0.6.0",
            "outputContract": "redibis.enrichment-delta/v1",
        },
        "prompt": {
            "normal": normal,
            "shared": shared,
            "stages": stages,
        },
        "context": {},
        "selection": {
            "glossaryTopK": 20,
            "examplesTopK": 3,
            "maxContextCharacters": 50_000,
        },
        "examples": [],
    }


class CustomContextStore:
    """Global custom overlays under ``$REDIBIS_CONFIGS_DIR/enrich-context/custom/``."""

    def __init__(self, root: Optional[PathLike] = None) -> None:
        self.root = Path(root).expanduser() if root else default_custom_dir()

    def add(self, files: list[Path], *, mode: str, stage: str = "") -> list[dict[str, Any]]:
        written: list[dict[str, Any]] = []
        for src in files:
            p = Path(src)
            if not p.is_file():
                raise FileNotFoundError(f"context file not found: {p}")
            if p.is_symlink():
                raise ValueError(f"symlinks are not allowed: {p}")
            if p.stat().st_size > MAX_CONTEXT_FILE_BYTES:
                raise ValueError(f"context file exceeds {MAX_CONTEXT_FILE_BYTES} bytes: {p}")
            rel = overlay_relpath(mode, p.name, stage=stage)
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            text = _normalize_text(p.read_text(encoding="utf-8"))
            dest.write_text(text, encoding="utf-8", newline="\n")
            written.append({"path": rel, "chars": len(text), "abs": str(dest)})
        return written

    def list_files(self) -> list[dict[str, Any]]:
        out = []
        for rel, text in _iter_overlay_files(self.root):
            out.append({"path": rel, "chars": len(text), "sha256": _sha256_text(text)})
        return out

    def remove(self, relpath: str) -> bool:
        n = (relpath or "").replace("\\", "/").lstrip("/")
        if ".." in n.split("/"):
            raise ValueError(f"invalid context path: {relpath!r}")
        path = self.root / n
        if not path.is_file():
            return False
        path.unlink()
        return True

    def read_for(self, *, mode: str, stage_kind: Optional[str] = None) -> list[tuple[str, str]]:
        return _select_overlay_rels(
            _iter_overlay_files(self.root), mode=mode, stage_kind=stage_kind,
        )
