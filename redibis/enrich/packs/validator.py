"""Semantic validation for loaded enrichment packs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from redibis.enrich.delta_schema import parse_enrichment_delta
from redibis.enrich.packs.errors import PackCompatibilityError, PackValidationError
from redibis.enrich.packs.models import LoadedEnrichmentPack

_OUTPUT_CONTRACT = "redibis.enrichment-delta/v1"
_PUBLISHER_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+)+$", re.IGNORECASE)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$", re.IGNORECASE)


@dataclass
class PackValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    signature_status: str = "unsigned"
    identity: str = ""
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "signature_status": self.signature_status,
            "identity": self.identity,
            "sha256": self.sha256,
        }


def _current_redibis_version() -> str:
    try:
        from importlib.metadata import version

        return version("redibis")
    except Exception:
        return "0.5.6"


def check_compatibility(
    pack: LoadedEnrichmentPack,
    *,
    redibis_version: Optional[str] = None,
) -> None:
    """Raise PackCompatibilityError if the pack cannot run on this Redibis."""
    running = redibis_version or _current_redibis_version()
    spec_text = pack.manifest.compatibility.redibis
    try:
        spec = SpecifierSet(spec_text)
        ver = Version(running)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise PackCompatibilityError(
            f"invalid compatibility/version: {exc}"
        ) from exc
    if ver not in spec:
        raise PackCompatibilityError(
            f"pack requires redibis {spec_text}; running {running}"
        )
    out = pack.manifest.compatibility.outputContract
    if out != _OUTPUT_CONTRACT:
        raise PackCompatibilityError(
            f"unsupported outputContract {out!r}; expected {_OUTPUT_CONTRACT!r}"
        )


def _validate_glossary(pack: LoadedEnrichmentPack, errors: list[str]) -> None:
    seen_names: set[str] = set()
    alias_owners: dict[str, str] = {}
    for entry in pack.glossary:
        key = entry.canonicalName.strip().lower()
        if key in seen_names:
            errors.append(f"duplicate glossary canonicalName: {entry.canonicalName}")
        seen_names.add(key)
        for alias in entry.aliases:
            akey = alias.strip().lower()
            if not akey:
                errors.append(f"empty alias for {entry.canonicalName}")
                continue
            owner = alias_owners.get(akey)
            if owner and owner != key:
                errors.append(
                    f"conflicting alias {alias!r}: owned by {owner} and {key}"
                )
            alias_owners[akey] = key


def _validate_goldens(pack: LoadedEnrichmentPack, errors: list[str]) -> None:
    for example in pack.manifest.examples:
        contract = pack.golden_inputs.get(example.id)
        delta = pack.golden_deltas.get(example.id)
        if contract is None:
            errors.append(f"missing golden input for example {example.id!r}")
            continue
        if not isinstance(contract.get("schema"), list) and "schema" in contract:
            errors.append(f"golden input {example.id!r}: schema must be a list")
        # Soft ODCS check — require apiVersion v3.x when present.
        api = str(contract.get("apiVersion") or "")
        if api and not re.match(r"^v3\.\d+", api):
            errors.append(
                f"golden input {example.id!r}: apiVersion must be ODCS v3.x, got {api!r}"
            )
        if delta is None:
            errors.append(f"missing golden delta for example {example.id!r}")
            continue
        _, delta_errors = parse_enrichment_delta(delta)
        for err in delta_errors:
            errors.append(f"golden delta {example.id!r}: {err}")


def _validate_eval_cases(pack: LoadedEnrichmentPack, errors: list[str]) -> None:
    if not pack.manifest.evals:
        return
    if not pack.eval_cases:
        errors.append("evals.cases is empty")
        return
    seen: set[str] = set()
    for case in pack.eval_cases:
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            errors.append("evaluation case missing id")
            continue
        if case_id in seen:
            errors.append(f"duplicate evaluation case id: {case_id}")
        seen.add(case_id)
        input_ref = case.get("input")
        if not input_ref:
            errors.append(f"evaluation case {case_id!r} missing input")
        assertions = case.get("assertions")
        if assertions is not None and not isinstance(assertions, dict):
            errors.append(f"evaluation case {case_id!r}: assertions must be a mapping")
        scoring = case.get("scoring") or {}
        if scoring and not isinstance(scoring, dict):
            errors.append(f"evaluation case {case_id!r}: scoring must be a mapping")


def validate_loaded_pack(
    pack: LoadedEnrichmentPack,
    *,
    redibis_version: Optional[str] = None,
    raise_on_error: bool = False,
) -> PackValidationReport:
    """Validate a loaded pack; returns a structured report."""
    errors: list[str] = []
    warnings = list(pack.warnings)

    md = pack.manifest.metadata
    if not _PUBLISHER_RE.match(md.publisherId):
        errors.append(
            f"publisherId must look like reverse DNS (e.g. com.example), got {md.publisherId!r}"
        )
    if not _NAME_RE.match(md.name):
        errors.append(f"invalid pack name: {md.name!r}")
    if not md.license.strip():
        errors.append("metadata.license must be non-empty")

    try:
        check_compatibility(pack, redibis_version=redibis_version)
    except PackCompatibilityError as exc:
        errors.append(str(exc))

    _validate_glossary(pack, errors)
    _validate_goldens(pack, errors)
    _validate_eval_cases(pack, errors)

    if pack.signature_status == "unsupported":
        warnings.append(
            "signature present but enrichment pack v1 does not cryptographically verify it"
        )

    report = PackValidationReport(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        signature_status=pack.signature_status,
        identity=pack.manifest.release_identity,
        sha256=pack.sha256,
    )
    if raise_on_error and not report.ok:
        raise PackValidationError(
            "pack validation failed",
            errors=report.errors,
        )
    return report


def load_and_validate_pack(
    path: str,
    *,
    redibis_version: Optional[str] = None,
) -> tuple[LoadedEnrichmentPack, PackValidationReport]:
    from redibis.enrich.packs.loader import load_enrichment_pack

    pack = load_enrichment_pack(path)
    report = validate_loaded_pack(pack, redibis_version=redibis_version, raise_on_error=True)
    return pack, report
