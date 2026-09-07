"""E7 — OSS distribution must not ship the enterprise tree."""

from __future__ import annotations

from pathlib import Path

import pytest
from setuptools import find_packages

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_excludes_enterprise():
    text = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-exclude enterprise" in text


def test_setuptools_find_packages_is_oss_only():
    """Mirrors ``[tool.setuptools.packages.find]`` — no vendor trees."""
    packages = find_packages(
        where=str(ROOT),
        include=["redibis*"],
        exclude=[
            "enterprise*",
            "redibis_codegen_service*",
            "redibis_enterprise*",
            "tests*",
            "docs*",
        ],
    )
    assert packages, "expected redibis packages"
    assert all(p == "redibis" or p.startswith("redibis.") for p in packages)
    assert not any("codegen_service" in p for p in packages)
    assert not any(p.startswith("enterprise") for p in packages)
    # OSS facade is fine; vendor package names are not.
    assert "redibis.enterprise" in packages or any(
        p.startswith("redibis.enterprise") for p in packages
    )


def test_enterprise_tree_is_outside_oss_package_dir():
    """Commercial trees sit next to ``redibis/``, not inside it (when present)."""
    if not (ROOT / "enterprise").is_dir():
        pytest.skip("enterprise/ not present (OSS export)")
    assert (ROOT / "enterprise" / "addons" / "reports").is_dir()
    assert (ROOT / "enterprise" / "content").is_dir()
    assert not (ROOT / "redibis" / "addons").exists()
    assert not (ROOT / "redibis" / "content" / "packs").exists()
    assert not (ROOT / "redibis" / "redibis_codegen_service").exists()
