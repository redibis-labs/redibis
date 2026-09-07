"""Open-core facade for optional enterprise module discovery.

Does **not** import vendor packages at module load. Discovery uses
``importlib.metadata`` entry points and optional ``find_spec`` probes only.
"""

from __future__ import annotations

from redibis.enterprise.status import discover_enterprise_modules, enterprise_status_payload

__all__ = [
    "discover_enterprise_modules",
    "enterprise_status_payload",
]
