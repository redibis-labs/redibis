"""Optional LLM-assisted gap filling for Contract Synthesis.

Deterministic mode is default. Assisted mode only proposes typed patches for
unresolved/conflict evidence; it never auto-writes active contracts.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from redibis.synthesis.evidence import EvidenceBundle, EvidenceKind, EvidenceRecord

log = logging.getLogger(__name__)


def build_assisted_prompt(
    bundle: EvidenceBundle,
    *,
    max_items: int = 40,
    max_chars: int = 12000,
) -> tuple[str, str]:
    """Build system/user prompts for unresolved and conflict evidence only."""
    items = []
    for rec in (bundle.conflicts + bundle.unresolved)[:max_items]:
        items.append({
            "id": rec.id,
            "kind": rec.kind.value if hasattr(rec.kind, "value") else str(rec.kind),
            "summary": rec.summary[:400],
            "requirement_ids": rec.requirement_ids,
            "payload": rec.payload,
        })
    for rec in bundle.by_kind(EvidenceKind.SCHEMA_FIELD)[:20]:
        items.append({
            "id": rec.id,
            "kind": "schema_field_context",
            "summary": rec.summary[:200],
            "payload": {
                "name": (rec.payload or {}).get("name"),
                "logicalType": (rec.payload or {}).get("logicalType"),
            },
            "requirement_ids": rec.requirement_ids,
        })

    system = (
        "You are assisting ODCS Contract Synthesis. "
        "Return ONLY JSON with keys proposals[] and foreign_keys[]. "
        "Propose fills for unresolved gaps and conflicts. "
        "Do not invent schema columns that lack evidence. "
        "foreign_keys are true foreign-key relationships only, not transforms. "
        "Never include secrets, raw PII samples, or full contract YAML."
    )
    user = json.dumps({"evidence": items}, indent=2)[:max_chars]
    return system, user


def parse_assisted_response(raw: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, [f"assisted JSON parse failed: {exc}"]
    if not isinstance(data, dict):
        return {}, ["assisted response must be a JSON object"]
    if "proposals" in data and not isinstance(data["proposals"], list):
        errors.append("proposals must be a list")
        data["proposals"] = []
    if "foreign_keys" in data and not isinstance(data["foreign_keys"], list):
        errors.append("foreign_keys must be a list")
        data["foreign_keys"] = []
    return data, errors


def apply_assisted_proposals(
    candidate: dict[str, Any],
    bundle: EvidenceBundle,
    proposals: dict[str, Any],
) -> list[str]:
    """Apply bounded assisted proposals onto the candidate; return findings."""
    findings: list[str] = []
    for item in proposals.get("proposals") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("target_path") or "")
        value = item.get("value")
        if not path:
            continue
        if path.startswith("description."):
            key = path.split(".", 1)[1]
            if key in ("purpose", "usage", "limitations"):
                desc = candidate.setdefault("description", {})
                if isinstance(desc, dict) and key not in desc:
                    desc[key] = value
                    findings.append(f"assisted filled {path}")
        elif path.startswith("customProperties."):
            prop = path.split(".", 1)[1]
            cps = list(candidate.get("customProperties") or [])
            if not any(isinstance(c, dict) and c.get("property") == prop for c in cps):
                cps.append({"property": prop, "value": value})
                candidate["customProperties"] = cps
                findings.append(f"assisted customProperty {prop}")

    for fk in proposals.get("foreign_keys") or []:
        if not isinstance(fk, dict) or not fk.get("to"):
            continue
        bundle.add(EvidenceRecord(
            id=f"assisted:fk:{fk.get('from', '')}:{fk.get('to')}",
            kind=EvidenceKind.FOREIGN_KEY,
            summary=str(fk.get("rationale") or "assisted foreign key"),
            confidence=float(fk.get("confidence") or 0.6),
            payload={
                "from": fk.get("from"),
                "to": fk.get("to"),
                "type": fk.get("type") or "foreignKey",
            },
            source="assisted",
        ))
        findings.append(f"assisted FK → {fk.get('to')}")
    return findings


def _call_provider(provider: Any, system: str, user: str) -> str:
    if hasattr(provider, "complete"):
        try:
            return str(provider.complete(system_prompt=system, user_prompt=user))
        except TypeError:
            return str(provider.complete(system=system, user=user))
    if hasattr(provider, "chat"):
        return str(provider.chat(system, user))
    raise TypeError(f"provider {type(provider)!r} has no complete/chat method")


def run_assisted(
    candidate: dict[str, Any],
    bundle: EvidenceBundle,
    *,
    provider: Any = None,
    redibis_config: Any = None,
) -> dict[str, Any]:
    """
    Call the LLM (optional) and apply proposals.

    Returns metadata: ``{used, findings, errors, raw_preview, proposals}``.
    """
    meta: dict[str, Any] = {
        "used": False,
        "findings": [],
        "errors": [],
        "raw_preview": "",
        "proposals": {},
    }
    if provider is None:
        meta["errors"].append("assisted mode requested but no provider configured")
        return meta

    system, user = build_assisted_prompt(bundle)
    try:
        from redibis.telemetry.model_gateway import guarded_model_call

        raw, rai = guarded_model_call(
            lambda: _call_provider(provider, system, user),
            model_id=str(getattr(provider, "model", "") or "synthesis"),
            provider=provider,
            system_prompt=system,
            user_prompt=user,
            redibis_config=redibis_config,
            model_role="contract.synthesis",
            node_kind="contract_synthesis",
        )
        meta["rai"] = rai
    except Exception as exc:
        meta["errors"].append(str(exc))
        return meta

    raw_text = raw if isinstance(raw, str) else str(raw)
    meta["raw_preview"] = raw_text[:2000]
    proposals, errors = parse_assisted_response(raw_text)
    meta["errors"].extend(errors)
    meta["proposals"] = proposals
    if errors and not proposals:
        return meta
    findings = apply_assisted_proposals(candidate, bundle, proposals)
    meta["findings"] = findings
    meta["used"] = True
    return meta
