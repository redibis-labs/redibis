"""Discover installed commercial / vendor modules without loading secrets."""

from __future__ import annotations

from importlib import metadata
from importlib.util import find_spec
from typing import Any


def _ep_names(group: str) -> list[str]:
    try:
        eps = metadata.entry_points()
        selected = (
            eps.select(group=group)
            if hasattr(eps, "select")
            else eps.get(group, [])  # type: ignore[arg-type]
        )
        return sorted({ep.name for ep in selected})
    except Exception:
        return []


def _dist_version(dist_name: str) -> str:
    try:
        return metadata.version(dist_name)
    except Exception:
        return ""


def discover_enterprise_modules() -> list[dict[str, Any]]:
    """Return installed Tier-B/C modules visible to open-core.

    Never includes tokens, secrets, private paths, or license file contents.
    """
    modules: list[dict[str, Any]] = []

    reporting = _ep_names("redibis.reporting")
    modules.append({
        "id": "reports",
        "kind": "addon",
        "distribution": "redibis-reports",
        "installed": bool(reporting),
        "entry_points": reporting,
        "version": _dist_version("redibis-reports") if reporting else "",
    })

    codegen_svc = find_spec("redibis_codegen_service") is not None
    modules.append({
        "id": "codegen_service",
        "kind": "vendor_service",
        "distribution": "redibis-codegen-service",
        "installed": codegen_svc,
        "entry_points": [],
        "version": _dist_version("redibis-codegen-service") if codegen_svc else "",
        "note": "Vendor-internal; customers use remote URL + token, not this package",
    })

    codegen_plugins = _ep_names("redibis.codegen")
    modules.append({
        "id": "codegen_plugin",
        "kind": "plugin",
        "distribution": "",
        "installed": bool(codegen_plugins),
        "entry_points": codegen_plugins,
        "version": "",
    })

    modules.append({
        "id": "commercial_content",
        "kind": "content",
        "distribution": "",
        "installed": False,
        "entry_points": [],
        "version": "",
        "note": "Content packs are data under enterprise/content/; not a Python install",
    })

    modules.append({
        "id": "airgap_deploy",
        "kind": "vendor_deploy",
        "distribution": "",
        "installed": False,
        "entry_points": [],
        "version": "",
        "note": (
            "Bare-metal / Docker airgap ship lives under enterprise/deploy/airgap/; "
            "not a Python install"
        ),
    })

    return modules


def enterprise_status_payload() -> dict[str, Any]:
    """Public envelope for ``GET /api/enterprise/status``."""
    modules = discover_enterprise_modules()
    return {
        "modules": modules,
        "installed_count": sum(1 for m in modules if m.get("installed")),
        "docs": {
            "enterprise_workspace": "enterprise/README.md",
            "reports": "pip install redibis-reports",
            "content_packs": "enterprise/docs/customer/CONTENT_PACKS.md",
            "airgap": "enterprise/deploy/airgap/README.md",
            "codegen": "docs/CODEGEN_SERVICE.md",
        },
    }
