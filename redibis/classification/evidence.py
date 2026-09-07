"""Evidence-bundle projection for the classification policy pack used in a scan.

Builds the ``header.provenance.pack_stack`` block (PLAN §3 /
``docs/internal/EVIDENCE_BUNDLE_PLAN.md``) for the single classification policy
pack resolved during PII detection, and exposes a contextvar so the impure scan
pipeline (``redibis.services.pipeline``) can hand the resolved stack to
``redibis.scan.report_bundle`` without threading a new parameter through every
scanner layer (``PIIScanner`` → ``Scan`` → ``ScanRunResult`` → ``ReportBundle``).

This module is intentionally impure (imports ``redibis.pack.identity`` for
UUID/hash helpers) — ``redibis.scan.evidence_bundle`` stays a pure leaf and only
ever receives an already-built ``pack_stack`` dict.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from redibis.classification.policy_pack import ClassificationPolicy

_current_pack_stack: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "redibis_current_pack_stack", default=None
)


def set_current_pack_stack(stack: Optional[dict]) -> None:
    """Record the pack stack resolved for the run currently in flight."""
    _current_pack_stack.set(stack)


def current_pack_stack() -> Optional[dict]:
    """Return the most recently resolved pack stack (or ``None``)."""
    return _current_pack_stack.get()


def clear_current_pack_stack() -> None:
    _current_pack_stack.set(None)


def classification_pack_stack_for_evidence(
    policy: "ClassificationPolicy",
    *,
    pack_name: str,
    pack_text: str = "",
) -> dict[str, Any]:
    """Build a ``header.provenance.pack_stack``-shaped dict for one policy pack.

    ``pack_text`` is the raw YAML the pack was loaded from (``get_pack_text``);
    its SHA-256 is the pack's content digest. Without it we still return
    ``rulesets``/``custom_rules`` (the part ``rules_fired`` resolves against)
    but leave identity fields empty rather than mint a false-confidence hash.
    """
    from redibis.pack.identity import backfill_uuid, compute_stack_sha256, compute_stack_uuid

    text = pack_text or ""
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
    family_id = f"redibis.classification.{pack_name}"
    pack_uuid = backfill_uuid(family_id, policy.version, sha) if sha else ""

    ruleset_id = f"rs.{policy.name}"
    rulesets: list[dict[str, Any]] = [
        {
            "id": ruleset_id,
            "name": policy.name,
            "version": policy.version,
            "source": f"pack:classification/{pack_name}",
            "patterns": len(policy.edge_rules),
        }
    ]

    custom_rules: list[dict[str, Any]] = []
    for rule in policy.edge_rules:
        rid = str(rule.get("id") or "")
        if not rid:
            continue
        custom_rules.append(
            {
                "id": f"cr.{policy.name}.{rid}",
                "ruleset": ruleset_id,
                "condition": json.dumps(rule.get("when") or {}, sort_keys=True),
                "effect": json.dumps(rule.get("then") or {}, sort_keys=True),
            }
        )

    pack_entry = {
        "uuid": pack_uuid,
        "family_id": family_id,
        "kind": "policy",
        "id": pack_name,
        "version": policy.version,
        "parent_uuid": None,
        "sha256": sha,
        "size_bytes": len(text.encode("utf-8")) if text else 0,
        "published_at": "",
        "author": "",
        "signature": None,
        "contents": {"custom_rules": len(custom_rules)},
        "available_locally": True,
    }

    stack_uuid = ""
    stack_sha256 = ""
    if pack_uuid and sha:
        @dataclass
        class _StackItem:
            uuid: str
            sha256: str

        item = _StackItem(uuid=pack_uuid, sha256=sha)
        stack_uuid = compute_stack_uuid([item], mode="overlay")
        stack_sha256 = compute_stack_sha256([item], mode="overlay")

    return {
        "stack_uuid": stack_uuid,
        "stack_sha256": stack_sha256,
        "mode": "overlay",
        "packs": [pack_entry],
        "rulesets": rulesets,
        "custom_rules": custom_rules,
    }


def resolve_and_record_pack_stack(policy: "ClassificationPolicy", *, pack_name: str) -> Optional[dict]:
    """Load the pack's raw text, build its evidence stack, and record it as current.

    Best-effort: swallows lookup errors (custom in-memory/overlay-only policies
    have no on-disk text) so classification never fails a scan for evidence
    bookkeeping.
    """
    pack_text = ""
    try:
        from redibis.classification.pack_store import get_pack_text

        pack_text = get_pack_text(pack_name)
    except Exception:
        pack_text = ""
    try:
        stack = classification_pack_stack_for_evidence(policy, pack_name=pack_name, pack_text=pack_text)
    except Exception:
        return None
    set_current_pack_stack(stack)
    return stack
