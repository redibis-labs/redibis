"""Structural layout checks for Redibis Pack sections."""

from __future__ import annotations

from pathlib import Path

from redibis.pack.canonical import MANIFEST_NAME, README_NAME
from redibis.pack.errors import PackValidationError
from redibis.pack.models import PackManifest


def validate_layout(manifest: PackManifest, paths: set[str]) -> None:
    """Ensure declared ``contents`` match files present in the archive."""
    errors: list[str] = []
    contents = manifest.contents

    if MANIFEST_NAME not in paths:
        errors.append(f"missing {MANIFEST_NAME}")

    if contents.config and "config/redibis.yaml" not in paths:
        errors.append("contents.config=true but config/redibis.yaml is missing")

    if contents.locale:
        locale_files = [p for p in paths if p.startswith("locale/") and p.endswith((".yaml", ".yml"))]
        if not locale_files:
            errors.append("contents.locale=true but no locale/*.yaml files found")

    for policy_ref in contents.behavior:
        expected = f"behavior/{policy_ref}.yaml"
        alt = f"behavior/{policy_ref}.yml"
        if expected not in paths and alt not in paths:
            errors.append(f"contents.behavior references missing file: {expected}")

    for name in contents.quality:
        expected = f"quality/rulesets/{name}.yaml"
        alt = f"quality/rulesets/{name}.yml"
        if expected not in paths and alt not in paths:
            errors.append(f"contents.quality references missing file: {expected}")

    for name in contents.masking:
        expected = f"masking/plans/{name}.yaml"
        alt = f"masking/plans/{name}.yml"
        if expected not in paths and alt not in paths:
            errors.append(f"contents.masking references missing file: {expected}")

    if contents.ner and "ner/models.yaml" not in paths and "ner/models.yml" not in paths:
        errors.append("contents.ner=true but ner/models.yaml is missing")

    for name in contents.classification:
        expected = f"classification/packs/{name}.yaml"
        alt = f"classification/packs/{name}.yml"
        if expected not in paths and alt not in paths:
            errors.append(f"contents.classification references missing file: {expected}")

    if contents.ner_weights:
        weight_paths = [
            p for p in paths
            if p.startswith("ner/weights/") or p.startswith("assets/ner/")
        ]
        if not weight_paths:
            errors.append("contents.ner_weights=true but no ner weight assets found")

    # Behavior files must not collide on policy id (stem before @version).
    behavior_files = [
        p for p in paths if p.startswith("behavior/") and Path(p).suffix.lower() in {".yaml", ".yml"}
    ]
    seen_ids: dict[str, str] = {}
    for rel in behavior_files:
        stem = Path(rel).stem  # id@version
        policy_id = stem.split("@", 1)[0]
        if policy_id in seen_ids:
            errors.append(
                f"duplicate policy id {policy_id!r}: {seen_ids[policy_id]} and {rel}"
            )
        else:
            seen_ids[policy_id] = rel

    # Reject unexpected top-level directories / files beyond the known layout.
    allowed_roots = {
        MANIFEST_NAME,
        README_NAME,
        "CHECKSUMS.json",
        "config",
        "locale",
        "behavior",
        "quality",
        "masking",
        "ner",
        "assets",
        "classification",
    }
    for rel in sorted(paths):
        top = rel.split("/", 1)[0]
        if top not in allowed_roots and rel not in allowed_roots:
            errors.append(f"unexpected pack path: {rel}")

    if errors:
        raise PackValidationError("pack layout validation failed", errors=errors)
