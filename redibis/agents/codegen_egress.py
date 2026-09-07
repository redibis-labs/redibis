"""Codegen egress gate — metadata-only outbound requests (Phase 8 T8.1)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from redibis.classification.ensemble import _schema_properties
from redibis.telemetry.pii_scope import infer_contains_raw_pii, payload_may_contain_raw_pii

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w{2,}", re.IGNORECASE)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CodegenRequest:
    """Structured outbound codegen payload — intent + contract metadata only."""

    request_id: str = field(default_factory=lambda: uuid4().hex[:16])
    intent: str = ""
    target_system: str = "ranger"
    table: str = ""
    contract_summary: dict[str, Any] = field(default_factory=dict)
    residency: str = "local"
    provider: str = ""
    created_at: str = field(default_factory=_utc_iso)
    egress_audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "intent": self.intent,
            "target_system": self.target_system,
            "table": self.table,
            "contract_summary": dict(self.contract_summary),
            "residency": self.residency,
            "provider": self.provider,
            "created_at": self.created_at,
            "egress_audit": dict(self.egress_audit),
        }

    def payload_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def payload_sha256(self) -> str:
        return hashlib.sha256(self.payload_json().encode("utf-8")).hexdigest()


@dataclass
class EgressValidation:
    allowed: bool
    request: CodegenRequest
    violations: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)


def slim_contract_summary(contract: dict[str, Any], table: str) -> dict[str, Any]:
    """Strip contract to governance metadata safe for external codegen."""
    columns: list[dict[str, Any]] = []
    for prop in _schema_properties(contract, table):
        name = prop.get("name")
        if not name:
            continue
        privacy = prop.get("privacy") or {}
        columns.append({
            "name": name,
            "logicalType": prop.get("logicalType"),
            "physicalType": prop.get("physicalType"),
            "tags": list(prop.get("tags") or []),
            "classification": prop.get("classification"),
            "masking_policy": (privacy.get("masking_policy") or privacy.get("maskingPolicy")),
            "entity_type": (privacy.get("classification_engine") or {}).get("entity_type"),
        })
    return {
        "table": table,
        "contract_uuid": contract.get("contract_uuid"),
        "name": contract.get("name"),
        "version": contract.get("version"),
        "column_count": len(columns),
        "columns": columns,
    }


def _scrub_intent_text(text: str) -> tuple[str, list[str]]:
    redactions: list[str] = []
    scrubbed = text
    for match in _EMAIL_RE.finditer(text):
        redactions.append(f"email@{match.start()}")
        scrubbed = scrubbed.replace(match.group(0), "[REDACTED_EMAIL]")
    if payload_may_contain_raw_pii(scrubbed):
        redactions.append("payload_heuristic_pii")
        scrubbed = "[REDACTED_INTENT_PII]"
    return scrubbed, redactions


class EgressGate:
    """Ensure only metadata/intent leaves the trust boundary."""

    def __init__(self, *, hard_block: bool = True):
        self.hard_block = hard_block

    def validate(
        self,
        request: CodegenRequest,
        *,
        contract: Optional[dict[str, Any]] = None,
    ) -> EgressValidation:
        violations: list[str] = []
        redactions: list[str] = []

        intent, intent_redactions = _scrub_intent_text(request.intent or "")
        redactions.extend(intent_redactions)

        contains_pii, pii_cols = infer_contains_raw_pii(
            contract=contract,
            table=request.table,
            user_prompt=request.intent,
        )
        if contains_pii and request.residency not in ("local", "private"):
            msg = f"egress blocked: raw PII signals in request (columns={pii_cols}) with residency={request.residency!r}"
            if self.hard_block:
                violations.append(msg)
            else:
                redactions.append(msg)

        # Contract summary must not carry sample values or free-text descriptions
        for col in request.contract_summary.get("columns") or []:
            for key in ("sample", "samples", "values", "description", "definition"):
                if col.get(key):
                    violations.append(f"column {col.get('name')!r} carries forbidden field {key!r}")

        safe = CodegenRequest(
            request_id=request.request_id,
            intent=intent,
            target_system=request.target_system,
            table=request.table,
            contract_summary=request.contract_summary,
            residency=request.residency,
            provider=request.provider,
            created_at=request.created_at,
            egress_audit={
                "validated_at": _utc_iso(),
                "payload_sha256": "",
                "redactions": redactions,
                "violations": violations,
                "contains_raw_pii": contains_pii,
                "pii_columns": pii_cols,
            },
        )
        safe.egress_audit["payload_sha256"] = safe.payload_sha256()

        allowed = not violations
        return EgressValidation(allowed=allowed, request=safe, violations=violations, redactions=redactions)


def build_codegen_request(
    *,
    intent: str,
    table: str,
    contract: dict[str, Any],
    target_system: str = "ranger",
    residency: str = "local",
    provider: str = "",
) -> CodegenRequest:
    """Assemble a codegen request from contract metadata (no raw data)."""
    return CodegenRequest(
        intent=intent.strip(),
        target_system=target_system,
        table=table,
        contract_summary=slim_contract_summary(contract, table),
        residency=residency,
        provider=provider,
    )


def prepare_codegen_request(
    *,
    intent: str,
    table: str,
    contract: dict[str, Any],
    target_system: str = "ranger",
    residency: str = "local",
    provider: str = "",
    hard_block: bool = True,
) -> EgressValidation:
    """Build + egress-validate a codegen request; log-ready for OTel/audit."""
    req = build_codegen_request(
        intent=intent,
        table=table,
        contract=contract,
        target_system=target_system,
        residency=residency,
        provider=provider,
    )
    return EgressGate(hard_block=hard_block).validate(req, contract=contract)
