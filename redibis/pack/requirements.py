"""Resolve ``requires`` against the live Redibis version and registries."""

from __future__ import annotations

from packaging.requirements import InvalidRequirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from redibis import __version__ as REDIBIS_VERSION
from redibis.pack.errors import PackCompatibilityError
from redibis.pack.models import PackManifest, PackRequires


def _check_version_range(spec: str, *, current: str = REDIBIS_VERSION) -> None:
    text = (spec or "").strip()
    if not text:
        raise PackCompatibilityError(
            "requires.redibis must be a non-empty version range",
            errors=["requires.redibis is empty"],
        )
    try:
        spec_set = SpecifierSet(text)
    except InvalidSpecifier as exc:
        raise PackCompatibilityError(
            f"invalid requires.redibis range: {text!r}",
            errors=[str(exc)],
        ) from exc
    if current not in spec_set:
        raise PackCompatibilityError(
            f"pack requires redibis {text}, running {current}",
            errors=[f"requires.redibis {text} not satisfied by {current}"],
        )


def _resolve_validators(names: list[str]) -> list[str]:
    from redibis.pii.regex_catalog import catalog_validators

    missing = [n for n in names if n not in catalog_validators]
    return missing


def _resolve_behavior_actions(names: list[str]) -> list[str]:
    from redibis.behavior.registry import get_builtin_registry

    reg = get_builtin_registry()
    missing: list[str] = []
    for name in names:
        if reg.get_action(name) is None and not reg.has_effect(name):
            missing.append(name)
    return missing


def _resolve_ner_backends(names: list[str]) -> list[str]:
    from redibis.pii.ner_registry import NERModelRegistry

    backends = set(NERModelRegistry._backend_classes())
    return [n for n in names if n not in backends]


def _resolve_profilers(names: list[str]) -> list[str]:
    from redibis.profiling import PROFILER_REGISTRY

    return [n for n in names if n not in PROFILER_REGISTRY]


def validate_requires(
    requires: PackRequires,
    *,
    current_version: str = REDIBIS_VERSION,
    check_registries: bool = True,
) -> None:
    """Fail closed when version or registry references cannot resolve."""
    _check_version_range(requires.redibis, current=current_version)
    if not check_registries:
        return
    errors: list[str] = []
    regs = requires.registries
    for name in _resolve_validators(regs.validators):
        errors.append(f"unresolved validator: {name}")
    for name in _resolve_behavior_actions(regs.behavior_actions):
        errors.append(f"unresolved behavior_action: {name}")
    for name in _resolve_ner_backends(regs.ner_backends):
        errors.append(f"unresolved ner_backend: {name}")
    for name in _resolve_profilers(regs.profilers):
        errors.append(f"unresolved profiler: {name}")
    if errors:
        raise PackCompatibilityError(
            "pack requires unresolved registry entries",
            errors=errors,
        )


def validate_manifest_requires(
    manifest: PackManifest,
    *,
    current_version: str = REDIBIS_VERSION,
    check_registries: bool = True,
) -> None:
    validate_requires(
        manifest.requires,
        current_version=current_version,
        check_registries=check_registries,
    )


def safe_parse_requirement_hint(text: str) -> bool:
    """Return True if ``text`` looks like a packaging specifier (best-effort)."""
    try:
        SpecifierSet(text)
        return True
    except (InvalidSpecifier, InvalidRequirement, TypeError):
        return False
