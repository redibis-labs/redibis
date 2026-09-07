"""Codegen safety chain — proposed-not-executed seam (commercial implementations plug in)."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.agents.codegen_egress import (
    EgressValidation,
    prepare_codegen_request,
)
from redibis.config import RedibisConfig


class CommercialFeatureError(RuntimeError):
    """Raised when a commercial-only agent capability is invoked without a plugin."""


@dataclass
class CodeProposal:
    """Generated code artifact — never executed by the open core."""

    language: str
    source: str
    purpose: str
    table: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VulnScanResult:
    passed: bool
    findings: list[dict[str, Any]] = field(default_factory=list)
    scanner: str = ""


@dataclass
class LLMJudgeResult:
    approved: bool
    rationale: str = ""
    model_id: str = ""


@dataclass
class CodegenSafetyChain:
    """Open-core stub — commercial package provides concrete scanners/judges."""

    def propose(self, prompt: str, *, context: dict[str, Any]) -> CodeProposal:
        raise CommercialFeatureError(
            "Dynamic codegen is a commercial feature — proposed code is never "
            "executed by the open core. Install the commercial agent-tools package "
            "or use the manual Ranger/Trino policy workflow."
        )

    def scan(self, proposal: CodeProposal) -> VulnScanResult:
        raise CommercialFeatureError("SAST/vuln scan requires commercial agent-tools")

    def judge(self, proposal: CodeProposal, *, scan: VulnScanResult) -> LLMJudgeResult:
        raise CommercialFeatureError("LLM-as-judge for codegen requires commercial agent-tools")

    def await_human(self, proposal: CodeProposal, *, judge: LLMJudgeResult) -> bool:
        """Human approval gate — always False in open core (nothing to approve)."""
        return False


class CodegenBackend(ABC):
    """Commercial implementations register via entry point ``redibis.codegen``."""

    @abstractmethod
    def propose(self, prompt: str, *, context: dict[str, Any]) -> CodeProposal:
        ...

    @abstractmethod
    def scan(self, proposal: CodeProposal) -> VulnScanResult:
        ...

    @abstractmethod
    def judge(self, proposal: CodeProposal, *, scan: VulnScanResult) -> LLMJudgeResult:
        ...


def get_codegen_backend() -> CodegenSafetyChain | CodegenBackend:
    """Return a commercial backend if registered, else the open-core stub."""
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group = (
            eps.select(group="redibis.codegen")
            if hasattr(eps, "select")
            else eps.get("redibis.codegen", [])
        )
        for ep in group:
            backend = ep.load()()
            return backend
    except Exception:
        pass
    return CodegenSafetyChain()


def _service_response_payload(
    validation: EgressValidation,
    resp: Any,
) -> dict[str, Any]:
    return {
        "status": "proposed",
        "request": validation.request.to_dict(),
        "proposal": {
            "language": resp.language,
            "purpose": resp.purpose,
            "table": resp.manifest.table,
            "metadata": {
                "manifest": resp.manifest.to_dict(),
                "provenance": resp.provenance,
            },
        },
        "code": resp.code,
        "vuln_report": resp.vuln_report.to_dict(),
        "judge_verdict": resp.judge_verdict.to_dict(),
        "manifest": resp.manifest.to_dict(),
        "provenance": resp.provenance,
        "egress": validation.request.egress_audit,
    }


def submit_codegen(
    *,
    intent: str,
    table: str,
    contract: dict[str, Any],
    target_system: str = "ranger",
    residency: str = "local",
    provider: str = "",
    hard_block: bool = True,
    redibis_config: Optional[RedibisConfig] = None,
) -> dict[str, Any]:
    """
    Build egress-safe ``CodegenRequest`` and submit to local or remote codegen engine.

    Open core never executes returned code. Logs egress audit on the request.
    """
    cfg = redibis_config or RedibisConfig.default()
    validation = prepare_codegen_request(
        intent=intent,
        table=table,
        contract=contract,
        target_system=target_system,
        residency=residency,
        provider=provider,
        hard_block=hard_block,
    )
    if not validation.allowed:
        raise PermissionError("; ".join(validation.violations))

    mode = (os.environ.get("REDIBIS_CODEGEN_MODE") or getattr(cfg.agents, "codegen_mode", "local") or "local").strip().lower()
    service_url = (cfg.agents.codegen_service_url or os.environ.get("REDIBIS_CODEGEN_SERVICE_URL") or "").strip()
    token = os.environ.get("REDIBIS_CODEGEN_TOKEN", "")

    if mode == "remote" or (service_url and token):
        if not service_url or not token:
            from redibis.config import ConfigError
            raise ConfigError(
                "Codegen mode is set to 'remote' but missing required configuration. "
                "Set agents.codegen_service_url and REDIBIS_CODEGEN_TOKEN."
            )
        from redibis.agents.codegen_remote_client import RemoteCodegenClient

        client = RemoteCodegenClient(service_url=service_url, token=token)
        remote_resp = client.submit(validation.request.to_dict())
        remote_resp["egress"] = validation.request.egress_audit
        remote_resp.setdefault("method", "remote")
        return remote_resp

    # Default: OSS Local Codegen Engine (Template + optional local LLM)
    from redibis.agents.codegen_local.service import LocalCodegenService

    local_svc = LocalCodegenService()
    result = local_svc.generate(
        intent=intent,
        table=table,
        contract=contract,
        target_system=target_system,
        provider_name=provider,
    )
    result["request"] = validation.request.to_dict()
    result["egress"] = validation.request.egress_audit
    result.setdefault("method", "local")
    return result
