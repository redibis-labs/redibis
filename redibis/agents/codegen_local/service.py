"""Local codegen service — generates Ranger / target policy proposals in OSS."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from redibis.agents.codegen_local.models import (
    LocalCodeProposal,
    LocalJudgeVerdict,
    LocalVulnReport,
)
from redibis.agents.codegen_local.scanners import policy_judge, sast_scan
from redibis.agents.codegen_local.validators import validate_artifact

_LOCAL_PROVIDER_KINDS = frozenset({"ollama", "vllm", "sglang", "demo"})


def template_ranger_policy(
    *,
    table: str,
    intent: str,
    columns: Optional[list[dict[str, Any]]] = None,
    contract: Optional[dict[str, Any]] = None,
    note: str = "Proposed Ranger policy generated locally by Redibis open-core",
) -> str:
    """Deterministic, schema-valid Ranger security policy template.

    Accepts either an egress ``contract_summary``-style ``columns`` list (preferred
    for hosted/remote paths) or a local ``contract`` dict with ``columns`` map/list.
    Shared by OSS local codegen and the vendor hosted service.
    """
    db, tbl = table.split(".", 1) if "." in table else ("default", table)
    policies: list[dict[str, Any]] = []

    col_rows: list[dict[str, Any]] = []
    if columns:
        col_rows = [c for c in columns if isinstance(c, dict) and c.get("name")]
    elif contract:
        raw = contract.get("columns") or {}
        if isinstance(raw, list):
            col_rows = [c for c in raw if isinstance(c, dict) and c.get("name")]
        elif isinstance(raw, dict):
            for col_name, meta in raw.items():
                if isinstance(meta, dict):
                    col_rows.append({"name": col_name, **meta})
                else:
                    col_rows.append({"name": col_name})

    for col in col_rows:
        col_name = str(col["name"])
        masking = col.get("masking_policy")
        pii = col.get("pii") or col.get("pii_type") or col.get("entity_type")
        if not masking and not pii:
            continue
        if not masking and pii:
            masking = "HASH" if "id" in str(pii).lower() else "MASK_SHOW_LAST_4"
        safe = table.replace(".", "_")
        policy: dict[str, Any] = {
            "name": f"redibis_{safe}_{col_name}_mask",
            "resource": {"database": db, "table": tbl},
            "column": col_name,
            "masking_policy": masking,
            "effect": "mask",
        }
        entity = col.get("entity_type") or (str(pii) if pii else None)
        if entity:
            policy["entity_type"] = entity
        policies.append(policy)

    if not policies:
        policies.append({
            "name": f"{tbl}_default_policy",
            "resource": {"database": db, "table": tbl, "column": "*"},
            "column": "*",
            "masking_policy": "NONE",
            "effect": "allow",
        })

    payload = {
        "service": "ranger",
        "table": table,
        "intent": intent,
        "policies": policies,
        "note": note,
    }
    return json.dumps(payload, indent=2)


def _template_ranger_policy(
    table: str,
    contract: dict[str, Any],
    intent: str,
) -> str:
    """Backward-compatible wrapper for :func:`template_ranger_policy`."""
    return template_ranger_policy(table=table, intent=intent, contract=contract)


def _try_local_llm(
    *,
    provider_name: str,
    target: str,
    table: str,
    intent: str,
    contract: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Call a local provider through ``guarded_model_call``. Returns (code, meta)."""
    from redibis.enrich.providers import get_provider
    from redibis.telemetry.llm_evidence import (
        current_llm_evidence_recorder,
        llm_evidence_recorder,
        new_llm_run_id,
    )
    from redibis.telemetry.model_gateway import guarded_model_call

    provider = get_provider(provider_name)
    residency = str(getattr(provider, "residency", "") or "").strip().lower()
    if residency and residency not in {"local", "private"}:
        raise ValueError(
            f"codegen.generator refuses non-local residency {residency!r} "
            f"for provider {provider_name!r}"
        )

    system = (
        f"You are a data-governance codegen assistant. Emit ONLY a {target}-compatible "
        "policy artifact. Never include sample values or PII. Propose-only output."
    )
    user = (
        f"Generate a {target} policy for table '{table}' with intent: {intent}.\n"
        f"Contract summary: {json.dumps(contract)[:1000]}"
    )
    model_id = str(
        getattr(provider, "_effective_model", lambda: "")()
        or getattr(provider, "model", "")
        or provider_name
    )

    codegen_run_id = new_llm_run_id(f"codegen-{table or 'table'}")

    def _invoke() -> str:
        return provider.complete(system, user, json_mode=(target == "ranger"))

    def _call():
        return guarded_model_call(
            _invoke,
            model_id=model_id,
            provider=provider,
            contract=contract if isinstance(contract, dict) else None,
            table=table,
            user_prompt=user,
            system_prompt=system,
            residency=residency or "local",
            model_role="codegen.generator",
            run_id=codegen_run_id,
        )

    if current_llm_evidence_recorder() is None:
        with llm_evidence_recorder(
            run_id=codegen_run_id,
            table=table,
            execution_mode="agentic",
        ):
            raw, rai_report = _call()
    else:
        raw, rai_report = _call()
    text = (raw or "").strip()
    return text, {
        "model_id": model_id,
        "provider": provider_name,
        "model_role": "codegen.generator",
        "rai": rai_report,
    }


class LocalCodegenService:
    """OSS local codegen backend — template or local LLM (Ollama/vLLM/SGLang)."""

    def generate(
        self,
        *,
        intent: str,
        table: str,
        contract: dict[str, Any],
        target_system: str = "ranger",
        provider_name: str = "",
    ) -> dict[str, Any]:
        """Generate a proposed code artifact, scan with SAST, and validate with policy_judge."""
        target = (target_system or "ranger").strip().lower()
        generation_source = "template"
        source_code = ""
        llm_meta: dict[str, Any] = {}

        local_provider = (provider_name or os.environ.get("CODEGEN_LOCAL_PROVIDER", "")).strip()
        if local_provider in _LOCAL_PROVIDER_KINDS:
            try:
                source_code, llm_meta = _try_local_llm(
                    provider_name=local_provider,
                    target=target,
                    table=table,
                    intent=intent,
                    contract=contract,
                )
                if source_code:
                    generation_source = "local_llm"
            except Exception:
                source_code = ""
                llm_meta = {}

        if not source_code:
            if target == "ranger":
                source_code = _template_ranger_policy(table, contract, intent)
                generation_source = "template"
            else:
                source_code = f"# Proposed policy for {target} on {table}\n# Intent: {intent}\n"
                generation_source = "template"

        proposal = LocalCodeProposal(
            language="json" if target == "ranger" else "python",
            source=source_code,
            purpose=intent,
            table=table,
            generation_source=generation_source,
            metadata={"target_system": target, **({k: v for k, v in llm_meta.items() if k != "rai"})},
        )

        validation = validate_artifact(source_code, target)
        scan_report = sast_scan(source_code)
        judge = policy_judge(
            source=source_code,
            target_system=target,
            scan=scan_report,
            validation=validation,
        )

        out: dict[str, Any] = {
            "status": "proposed",
            "generation_source": generation_source,
            "proposal": proposal.to_dict(),
            "code": source_code,
            "validation": validation.to_dict(),
            "vuln_report": scan_report.to_dict(),
            "judge_verdict": judge.to_dict(),
            "model_role": "codegen.generator",
        }
        if llm_meta.get("provider"):
            out["provider"] = llm_meta["provider"]
            out["model_id"] = llm_meta.get("model_id", "")
        if llm_meta.get("rai") is not None:
            out["rai"] = llm_meta["rai"]
        return out
