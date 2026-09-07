"""Built-in default Redibis Pack export (rich portable surface)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Union

from redibis import __version__ as REDIBIS_VERSION
from redibis.config import RedibisConfig
from redibis.pack.default_sections import (
    build_default_sections,
    catalog_validator_names,
    default_contents_flags,
)
from redibis.pack.models import (
    PackContents,
    PackManifest,
    PackMetadata,
    PackRegistries,
    PackRequires,
)
from redibis.pack.writer import build_pack_files, write_pack

PathLike = Union[str, Path]

DEFAULT_PACK_ID = "redibis-default"


def default_compat_range(version: str = REDIBIS_VERSION) -> str:
    """Engine compatibility range for the shipped default pack.

    ``0.5.6`` → ``>=0.5,<0.6`` so patch releases stay compatible while
    minor bumps require a deliberate golden refresh.
    """
    parts = [int(p) for p in version.split(".")[:2]]
    if len(parts) < 2:
        raise ValueError(f"cannot derive compat range from version {version!r}")
    major, minor = parts[0], parts[1]
    return f">={major}.{minor},<{major}.{minor + 1}"


def build_default_manifest(
    *,
    version: str = REDIBIS_VERSION,
    created: Optional[str] = None,
    sections: Mapping[str, Any] | None = None,
) -> PackManifest:
    # ``created`` defaults to None so the default-pack SHA is stable across
    # rebuilds (version + config content only). Callers may stamp a time.
    flags = default_contents_flags(dict(sections or {}))
    return PackManifest(
        apiVersion="redibis.io/pack/v1",
        kind="RedibisPack",
        metadata=PackMetadata(
            id=DEFAULT_PACK_ID,
            version=version,
            description=(
                "Shipped redibis defaults — portable config, regex catalog, "
                "Presidio context tokens, phone locale, NER labels/phrases, "
                "classification packs, masking regex library, and LLM prompt templates."
            ),
            author="redibis",
            created=created,
            base=None,
        ),
        requires=PackRequires(
            redibis=default_compat_range(version),
            registries=PackRegistries(
                validators=catalog_validator_names(),
                behavior_actions=["core.verdict.set_entity"],
                ner_backends=["gliner"],
                profilers=["great_expectations"],
            ),
        ),
        contents=PackContents(**flags),
        mode="overlay",
        checksum=None,
        signature=None,
    )


def _default_readme(version: str) -> str:
    return (
        f"# {DEFAULT_PACK_ID}@{version}\n\n"
        "Shipped redibis defaults as a portable Redibis Pack.\n\n"
        "Includes:\n"
        "- `config/redibis.yaml` — allow-listed portable config\n"
        "- `locale/regex.yaml` — full regex catalog (`replace_all`)\n"
        "- `locale/tokens.yaml` — Presidio/column context tokens\n"
        "- `locale/phone.yaml` — regions / MSISDN prefixes / geofence\n"
        "- `ner/models.yaml` — GLiNER labels, phrases, floors (no weights)\n"
        "- `classification/packs/` — builtin policy packs + edge rules\n"
        "- `assets/regex_patterns.json` — masking regex library\n"
        "- `assets/collisions.yaml` / `assets/coverage_gaps.yaml`\n"
        "- `assets/prompts/` — enrichment / refiner / tuning prompt templates\n\n"
        "Data only — no code, secrets, or masking keys.\n"
    )


def build_default_pack_files(
    config: Optional[RedibisConfig | Mapping[str, Any]] = None,
    *,
    version: str = REDIBIS_VERSION,
    created: Optional[str] = None,
) -> tuple[dict[str, bytes], str]:
    """Assemble the canonical default-pack file map and pack SHA."""
    if config is None:
        cfg: RedibisConfig | Mapping[str, Any] = RedibisConfig.default()
    else:
        cfg = config
    if isinstance(cfg, RedibisConfig):
        sections = build_default_sections(cfg)
    else:
        # Mapping path: still build rich sections from live defaults, but
        # override portable config with the provided allow-listed projection.
        from redibis.pack.config_allowlist import extract_portable_config

        sections = build_default_sections(RedibisConfig.default())
        sections["config/redibis.yaml"] = extract_portable_config(cfg)
    manifest = build_default_manifest(
        version=version, created=created, sections=sections
    )
    return build_pack_files(
        manifest,
        sections,
        readme=_default_readme(version),
        check_registries=True,
    )


def export_default_pack(
    path: PathLike,
    config: Optional[RedibisConfig | Mapping[str, Any]] = None,
    *,
    version: str = REDIBIS_VERSION,
    created: Optional[str] = None,
) -> str:
    """Write ``redibis-default@<version>`` to ``path``; return pack SHA-256."""
    files, pack_sha = build_default_pack_files(
        config, version=version, created=created
    )
    from redibis.pack.archive import write_zip_file

    write_zip_file(Path(path), files)
    return pack_sha
