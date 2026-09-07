"""
redibis.behavior.plugins
========================
Trusted fact/action plug-in SDK — entry-point discovery and registration.

Entry-point groups
------------------
``redibis.behavior_facts``
    Each entry point must return a list of ``(FactDescriptor, provider_callable)``
    tuples where *provider_callable* may be ``None`` for pure metadata facts.

``redibis.behavior_actions``
    Each entry point must return a list of ``(EffectDescriptor, BehaviorAction)``
    tuples.

Security model (v1 in-process)
-------------------------------
- Only packages listed in ``BehaviorConfig.plugin_allowlist`` are loaded.
- Built-in IDs (registered with ``builtin=True``) cannot be replaced.
- ``side_effect`` classes are restricted to ``{"none", "review"}``.
- Every loaded entry point must return a non-empty list of descriptors.
- A manifest record is emitted for every registered ID.

Enable a plugin
---------------
Add the distribution name to ``BehaviorConfig.plugin_allowlist``::

    behavior:
      enabled: true
      plugin_allowlist:
        - redibis_telecom_normalize

Then call ``apply_plugins_to_registry(config, registry)`` before building
the ``BehaviorPolicySet``.

Load report fields
------------------
Each item returned by ``load_plugins`` is a dict with:
  ``distribution``, ``version``, ``ep_name``, ``ep_group``,
  ``registered_ids``, ``digest``, ``status`` ("ok" | "rejected"),
  ``reject_reason`` (set when status == "rejected").
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import sys
from copy import deepcopy
from typing import Any

from redibis.behavior.config import BehaviorConfig
from redibis.behavior.models import HookStage
from redibis.behavior.registry import (
    BehaviorAction,
    BehaviorRegistry,
    EffectDescriptor,
    FactDescriptor,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GROUP_FACTS   = "redibis.behavior_facts"
_GROUP_ACTIONS = "redibis.behavior_actions"

# Side-effect classes that third-party plugins may declare.
_ALLOWED_SIDE_EFFECTS = frozenset({"none", "review"})

# Autonomy values allowed for third-party plugins (write/external are blocked).
_ALLOWED_AUTONOMY = frozenset({"auto", "suggest-only", "gate"})


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _dist_digest(dist: importlib.metadata.Distribution) -> str:
    """
    Return a hex digest of the distribution's RECORD or METADATA file,
    whichever is available first.  Returns empty string when neither is
    accessible (editable installs, VCS sources, etc.).
    """
    for resource_name in ("RECORD", "METADATA"):
        try:
            text = dist.read_text(resource_name)
            if text:
                return hashlib.sha256(text.encode()).hexdigest()
        except Exception:  # noqa: BLE001
            continue
    return ""


def _ep_callable_digest(fn: Any) -> str:
    """Return a stable digest of the entry-point callable's source, if available."""
    try:
        src = inspect.getsource(fn)
        return hashlib.sha256(src.encode()).hexdigest()
    except (OSError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Rejection helpers
# ---------------------------------------------------------------------------

class PluginRejected(Exception):
    """Raised when a plug-in entry point fails validation."""


def _reject(ep_name: str, reason: str) -> None:
    raise PluginRejected(f"[{ep_name}] {reason}")


# ---------------------------------------------------------------------------
# Fact entry-point loading
# ---------------------------------------------------------------------------

def _load_fact_ep(
    ep: importlib.metadata.EntryPoint,
    dist_name: str,
    builtin_fact_ids: frozenset[str],
) -> list[tuple[FactDescriptor, Any]]:
    """Load and validate a ``redibis.behavior_facts`` entry point."""
    fn = ep.load()
    if not callable(fn):
        _reject(ep.name, "entry point is not callable")

    result = fn()
    if not isinstance(result, (list, tuple)):
        _reject(ep.name, "entry point must return a list of (FactDescriptor, provider) tuples")

    validated: list[tuple[FactDescriptor, Any]] = []
    for i, item in enumerate(result):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            _reject(ep.name, f"item[{i}] must be a (FactDescriptor, provider) tuple")
        desc, provider = item

        if not isinstance(desc, FactDescriptor):
            _reject(ep.name, f"item[{i}][0] must be a FactDescriptor, got {type(desc).__name__}")

        if "." not in desc.id:
            _reject(ep.name,
                    f"third-party fact ID {desc.id!r} must be namespaced (contain '.')")

        if desc.id in builtin_fact_ids:
            _reject(ep.name,
                    f"cannot replace built-in fact {desc.id!r}")

        if desc.privacy_class not in {"public", "metadata", "sensitive_aggregate"}:
            _reject(ep.name,
                    f"unsupported privacy_class {desc.privacy_class!r} on fact {desc.id!r}")

        validated.append((desc, provider))

    if not validated:
        _reject(ep.name, "entry point returned no descriptors")

    return validated


# ---------------------------------------------------------------------------
# Action entry-point loading
# ---------------------------------------------------------------------------

def _load_action_ep(
    ep: importlib.metadata.EntryPoint,
    dist_name: str,
    builtin_effect_ids: frozenset[str],
) -> list[tuple[EffectDescriptor, BehaviorAction]]:
    """Load and validate a ``redibis.behavior_actions`` entry point."""
    fn = ep.load()
    if not callable(fn):
        _reject(ep.name, "entry point is not callable")

    result = fn()
    if not isinstance(result, (list, tuple)):
        _reject(ep.name,
                "entry point must return a list of (EffectDescriptor, BehaviorAction) tuples")

    validated: list[tuple[EffectDescriptor, BehaviorAction]] = []
    for i, item in enumerate(result):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            _reject(ep.name, f"item[{i}] must be a (EffectDescriptor, handler) tuple")
        desc, handler = item

        if not isinstance(desc, EffectDescriptor):
            _reject(ep.name,
                    f"item[{i}][0] must be an EffectDescriptor, got {type(desc).__name__}")

        if "." not in desc.id:
            _reject(ep.name,
                    f"third-party effect ID {desc.id!r} must be namespaced (contain '.')")

        if desc.id in builtin_effect_ids:
            _reject(ep.name,
                    f"cannot replace built-in effect {desc.id!r}")

        if desc.side_effect not in _ALLOWED_SIDE_EFFECTS:
            _reject(ep.name,
                    f"unsupported side_effect class {desc.side_effect!r} on effect "
                    f"{desc.id!r}; allowed: {sorted(_ALLOWED_SIDE_EFFECTS)}")

        if desc.autonomy not in _ALLOWED_AUTONOMY:
            _reject(ep.name,
                    f"unsupported autonomy {desc.autonomy!r} on effect {desc.id!r}; "
                    f"allowed: {sorted(_ALLOWED_AUTONOMY)}")

        if handler is None:
            _reject(ep.name, f"effect {desc.id!r} has no handler (handler must not be None)")

        if not callable(getattr(handler, "apply", None)):
            _reject(ep.name,
                    f"handler for effect {desc.id!r} does not implement apply(context, params)")

        validated.append((desc, handler))

    if not validated:
        _reject(ep.name, "entry point returned no descriptors")

    return validated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_plugins(
    config: BehaviorConfig,
    registry: BehaviorRegistry,
) -> list[dict]:
    """
    Discover and validate all entry points in the two plug-in groups.

    Only packages listed in ``config.plugin_allowlist`` are loaded.
    Built-in IDs are never replaced.

    Returns a list of load-report dicts (one per entry point processed).
    Reports include both successful registrations and rejections.
    """
    allowlist: frozenset[str] = frozenset(config.plugin_allowlist or [])
    reports: list[dict] = []

    if not allowlist:
        return reports

    builtin_fact_ids:   frozenset[str] = registry._builtin_facts
    builtin_effect_ids: frozenset[str] = registry._builtin_effects

    # Collect all entry points for both groups.
    all_eps: list[tuple[str, importlib.metadata.EntryPoint]] = []
    for group in (_GROUP_FACTS, _GROUP_ACTIONS):
        for ep in importlib.metadata.entry_points(group=group):
            all_eps.append((group, ep))

    for group, ep in all_eps:
        dist_name = _resolve_dist_name(ep)
        report: dict = {
            "distribution": dist_name,
            "version":      _resolve_dist_version(ep),
            "ep_name":      ep.name,
            "ep_group":     group,
            "registered_ids": [],
            "digest":       "",
            "status":       "ok",
            "reject_reason": None,
        }

        if dist_name not in allowlist:
            report["status"]        = "rejected"
            report["reject_reason"] = (
                f"distribution {dist_name!r} is not in plugin_allowlist"
            )
            _log.warning(
                "behavior plugin rejected (not allow-listed): dist=%s ep=%s",
                dist_name, ep.name,
            )
            reports.append(report)
            continue

        try:
            if group == _GROUP_FACTS:
                items = _load_fact_ep(ep, dist_name, builtin_fact_ids)
                for desc, provider in items:
                    registry.register_fact(desc, provider=provider)
                    report["registered_ids"].append(desc.id)
            else:
                items = _load_action_ep(ep, dist_name, builtin_effect_ids)
                for desc, handler in items:
                    registry.register_effect(desc, handler=handler)
                    report["registered_ids"].append(desc.id)

            report["digest"] = _compute_report_digest(ep, dist_name)

        except PluginRejected as exc:
            report["status"]        = "rejected"
            report["reject_reason"] = str(exc)
            _log.warning("behavior plugin rejected: %s", exc)

        except Exception as exc:  # noqa: BLE001
            report["status"]        = "rejected"
            report["reject_reason"] = f"unexpected error: {exc!r}"
            _log.exception("behavior plugin load error: ep=%s dist=%s", ep.name, dist_name)

        reports.append(report)

    return reports


def apply_plugins_to_registry(
    config: BehaviorConfig,
    registry: BehaviorRegistry,
) -> BehaviorRegistry:
    """
    Load all allow-listed plugins into *registry* and return it.

    The same registry instance is mutated and returned (registration is
    idempotent for identical descriptors).  Call this once at startup before
    building the ``BehaviorPolicySet``.
    """
    reports = load_plugins(config, registry)
    ok = sum(1 for r in reports if r["status"] == "ok")
    rejected = sum(1 for r in reports if r["status"] == "rejected")
    if ok or rejected:
        _log.info(
            "behavior plugins applied: ok=%d rejected=%d", ok, rejected
        )
    return registry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_dist_name(ep: importlib.metadata.EntryPoint) -> str:
    """Return the distribution name for an entry point, or its value string."""
    try:
        # Python 3.9+: ep.dist is the Distribution object
        dist = ep.dist  # type: ignore[attr-defined]
        if dist is not None:
            return dist.metadata["Name"] or dist.name
    except AttributeError:
        pass
    # Fallback: parse from the entry point value (module path prefix)
    return ep.value.split(":")[0].split(".")[0]


def _resolve_dist_version(ep: importlib.metadata.EntryPoint) -> str:
    """Return the version string for an entry point's distribution."""
    try:
        dist = ep.dist  # type: ignore[attr-defined]
        if dist is not None:
            return dist.metadata.get("Version", "")
    except AttributeError:
        pass
    try:
        name = _resolve_dist_name(ep)
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def _compute_report_digest(
    ep: importlib.metadata.EntryPoint,
    dist_name: str,
) -> str:
    """
    Compute a stable digest for a successfully loaded entry point.

    Tries distribution RECORD/METADATA first, then source digest of the
    entry-point callable.
    """
    try:
        dist = ep.dist  # type: ignore[attr-defined]
        if dist is not None:
            d = _dist_digest(dist)
            if d:
                return d
    except AttributeError:
        pass

    try:
        fn = ep.load()
        return _ep_callable_digest(fn)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Manifest summary
# ---------------------------------------------------------------------------

def plugin_manifest(reports: list[dict]) -> dict:
    """
    Return a JSON-safe summary of all load reports, suitable for audit logging.

    Fields: ``loaded`` (count of ok reports), ``rejected`` (count),
    ``entries`` (full report list), ``manifest_sha256`` (digest of entries).
    """
    entries = [
        {k: v for k, v in r.items() if k != "reject_reason" or r["status"] == "rejected"}
        for r in reports
    ]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return {
        "loaded":         sum(1 for r in reports if r["status"] == "ok"),
        "rejected":       sum(1 for r in reports if r["status"] == "rejected"),
        "entries":        entries,
        "manifest_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
