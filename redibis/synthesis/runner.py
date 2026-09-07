"""Contract Synthesis runner — portable ODCS v3.1 from a v3 base + docs/code.

Does **not** call ``ContractStore.upsert``. Existing Redibis scan/enrich/store
workflows remain unchanged. Output is a validated portable candidate plus
lineage / traceability artifacts for import into other applications.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from redibis.contracts.convert import convert_to_v31, propose_semantic_version_bump, normalize_portable_contract
from redibis.contracts.validate_odcs import assert_synthesis_contract, validate_odcs_contract
from redibis.contracts.versions import SYNTHESIS_API_VERSION
from redibis.synthesis.analyzers import run_analyzers
from redibis.synthesis.artifacts import write_synthesis_artifacts
from redibis.synthesis.assisted import run_assisted
from redibis.synthesis.evidence import EvidenceBundle
from redibis.synthesis.ingest import InputManifest, ingest_bytes_map, ingest_paths
from redibis.synthesis.reconcile import reconcile
from redibis.synthesis.stages import run_stages

log = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_contract(source: Union[str, Path, dict]) -> dict:
    if isinstance(source, dict):
        return copy.deepcopy(source)
    path = Path(source)
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"contract is not a mapping: {path}")
    return data


@dataclass
class SynthesisResult:
    candidate: dict
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    lineage: dict = field(default_factory=dict)
    lineage_react_flow: dict = field(default_factory=dict)
    traceability: dict = field(default_factory=dict)
    stage_results: list[dict] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    comparison: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "candidate_api_version": (self.candidate or {}).get("apiVersion"),
            "artifacts": dict(self.artifacts),
            "traceability": self.traceability,
            "meta": self.meta,
            "stage_results": self.stage_results,
            "comparison": self.comparison,
        }


@dataclass
class ContractSynthesisRunner:
    """Orchestrate ingest → analyze → reconcile → stages → optional assisted → export."""

    analysis_mode: str = "deterministic"  # deterministic | assisted
    odcs_version: str = SYNTHESIS_API_VERSION
    bump_kind: str = "minor"
    provider: Any = None
    redibis_config: Any = None

    def run(
        self,
        *,
        base_contract: Union[str, Path, dict],
        requirement_paths: Optional[list[Union[str, Path]]] = None,
        source_paths: Optional[list[Union[str, Path]]] = None,
        uploaded: Optional[dict[str, bytes]] = None,
        output_dir: Optional[Union[str, Path]] = None,
        also_run_assisted_compare: bool = False,
    ) -> SynthesisResult:
        warnings: list[str] = []
        base = load_contract(base_contract)
        api = str(base.get("apiVersion") or "")
        if api and not api.startswith("v3."):
            raise ValueError(
                f"Contract Synthesis requires an ODCS v3 base contract, got apiVersion={api!r}"
            )

        # Ingest inputs
        manifest = InputManifest()
        paths = list(requirement_paths or []) + list(source_paths or [])
        if paths:
            m = ingest_paths(paths)
            manifest.files.extend(m.files)
            manifest.errors.extend(m.errors)
            manifest.warnings.extend(m.warnings)
            manifest.total_bytes += m.total_bytes
        if uploaded:
            m2 = ingest_bytes_map(uploaded)
            manifest.files.extend(m2.files)
            manifest.errors.extend(m2.errors)
            manifest.warnings.extend(m2.warnings)
            manifest.total_bytes += m2.total_bytes
        if manifest.errors:
            # Hard fail on ingest security/type errors.
            raise ValueError("ingest failed:\n" + "\n".join(manifest.errors))
        warnings.extend(manifest.warnings)

        bundle = EvidenceBundle(meta={
            "analysis_mode": self.analysis_mode,
            "started_at": _utc_iso(),
            "input_files": [f.to_dict() for f in manifest.files],
        })
        run_analyzers(manifest.files, bundle)
        reconcile(bundle, base_contract=base)

        # Convert base → v3.1 candidate skeleton (portable; strips redibis extensions).
        candidate = convert_to_v31(base, target_api_version=self.odcs_version)
        candidate["version"] = propose_semantic_version_bump(
            str(base.get("version") or "1.0.0"),
            kind=self.bump_kind,
        )
        # Keep status draft for portable export until steward activates elsewhere.
        if candidate.get("status") == "active":
            candidate["status"] = "draft"
            warnings.append("portable candidate status set to draft (was active on base)")

        ctx: dict[str, Any] = {"analysis_mode": self.analysis_mode}
        candidate, stage_results, ctx = run_stages(candidate, bundle, context=ctx)

        assisted_meta: dict[str, Any] = {}
        comparison: dict[str, Any] = {}
        deterministic_snapshot = copy.deepcopy(candidate)

        def _rerun_tail(target: dict) -> None:
            from redibis.synthesis.stages import STAGE_REGISTRY

            for sid in ("lineage_relationships", "traceability", "validate"):
                spec = STAGE_REGISTRY.get(sid)
                if spec:
                    stage_results.append(spec.run(target, bundle, ctx))

        if self.analysis_mode == "assisted":
            assisted_meta = run_assisted(
                candidate,
                bundle,
                provider=self.provider,
                redibis_config=self.redibis_config,
            )
            if assisted_meta.get("used"):
                _rerun_tail(candidate)

        normalize_portable_contract(candidate)
        normalize_portable_contract(deterministic_snapshot)

        if also_run_assisted_compare and self.provider is not None:
            assisted_candidate = copy.deepcopy(deterministic_snapshot)
            compare_meta = run_assisted(
                assisted_candidate,
                bundle,
                provider=self.provider,
                redibis_config=self.redibis_config,
            )
            if compare_meta.get("used"):
                from redibis.synthesis.stages import STAGE_REGISTRY

                for sid in ("lineage_relationships", "traceability", "validate"):
                    spec = STAGE_REGISTRY.get(sid)
                    if spec:
                        spec.run(assisted_candidate, bundle, dict(ctx))
                normalize_portable_contract(assisted_candidate)
            comparison = {
                "deterministic_valid": not assert_synthesis_contract(deterministic_snapshot),
                "assisted_meta": compare_meta,
                "assisted_valid": (
                    not assert_synthesis_contract(assisted_candidate)
                    if compare_meta.get("used") else False
                ),
            }

        stage_dicts = [
            s.to_dict() if hasattr(s, "to_dict") else (s if isinstance(s, dict) else {"stage_id": str(s)})
            for s in stage_results
        ]

        errors = assert_synthesis_contract(candidate)
        # If validate stage already recorded errors, prefer those.
        for s in stage_dicts:
            if s.get("stage_id") == "validate" and s.get("errors"):
                errors = list(s["errors"])

        meta = {
            "tool": "redibis.synthesis",
            "analysis_mode": self.analysis_mode,
            "odcs_version": self.odcs_version,
            "base_api_version": api or base.get("apiVersion"),
            "finished_at": _utc_iso(),
            "assisted": assisted_meta,
            "writes_active_contract": False,
            "note": (
                "Portable ODCS v3.1 candidate — out of Redibis merger; "
                "does not call ContractStore.upsert()"
            ),
        }
        artifacts: dict[str, str] = {}
        if output_dir:
            artifacts = write_synthesis_artifacts(
                output_dir,
                candidate=candidate,
                base_contract=base,
                evidence=bundle.to_dict(),
                lineage=ctx.get("lineage_graph") or {},
                lineage_react_flow=ctx.get("lineage_react_flow") or {},
                traceability=ctx.get("traceability") or {},
                stage_results=stage_dicts,
                meta=meta,
                comparison=comparison or None,
            )

        return SynthesisResult(
            candidate=candidate,
            valid=not errors,
            errors=errors,
            warnings=warnings,
            evidence=bundle.to_dict(),
            lineage=ctx.get("lineage_graph") or {},
            lineage_react_flow=ctx.get("lineage_react_flow") or {},
            traceability=ctx.get("traceability") or {},
            stage_results=stage_dicts,
            artifacts=artifacts,
            meta=meta,
            comparison=comparison,
        )
