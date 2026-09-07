"""Contract Synthesis — portable ODCS v3.1 from requirements + pipeline sources.

Public entry::

    from redibis.synthesis import ContractSynthesisRunner

    result = ContractSynthesisRunner(analysis_mode=\"deterministic\").run(
        base_contract=\"contract.v3.yaml\",
        requirement_paths=[\"01_pipeline_requirements.md\"],
        source_paths=[\"build_cafc_daily.py\", \"build.sql\"],
        output_dir=\"./synthesis_out\",
    )
"""

from redibis.synthesis.runner import ContractSynthesisRunner, SynthesisResult
from redibis.synthesis.ingest import InputManifest, ingest_paths, ingest_bytes_map
from redibis.synthesis.evidence import EvidenceBundle

__all__ = [
    "ContractSynthesisRunner",
    "SynthesisResult",
    "InputManifest",
    "ingest_paths",
    "ingest_bytes_map",
    "EvidenceBundle",
]
