"""
pii_detection.orchestrator.pipeline
=====================================
Workflow B — PII detection pipeline.

Programmatic usage
------------------
    from redibis.pii.runner import PIIDetectionRunner
    from redibis.pipeline_config import PipelineConfig
    from redibis.store.storage_backend    import LocalBackend
    from redibis.store.contract_store import ContractStore
    from redibis.pii.thresholds  import Thresholds

    backend = LocalBackend("./dev_storage")
    store   = ContractStore(backend, bucket="pii-contracts")
    config  = PipelineConfig(
        table      = "telecom.customers",
        equation   = "balanced",
        thresholds = Thresholds(presidio_min=0.7),
    )
    result = PIIDetectionRunner(config, backend, store).run()
    # result["status"] == "success"
    # result["pii_summary"]["detected"] == N

CLI usage (internal — prefer config-based API for library code)
-----------------------------------------------------------------
    PIIDetectionRunner.from_args(argparse_namespace, backend, store).run()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from redibis.models import PIIDetection, RunMetadata
from redibis.pii.thresholds import Thresholds
from redibis.pii.equations import decide_pii
from redibis.quality.sampling import PandasTableSampler, SamplingConfig
from redibis.pii.detector import detect_pii
from redibis.pii.llm_refiner import refine_detections
from redibis.pipeline_config import PipelineConfig
from redibis.runner_utils import utc_iso, timed_step, load_sample
from redibis.pii.run_outputs import write_pii_run_outputs
from redibis.store.storage_backend import StorageBackend, RunOutputWriter
from redibis.store.contract_store import ContractStore

logger = logging.getLogger("pii.pipeline")


class PIIDetectionRunner:
    """
    Orchestrates the full PII detection workflow (Workflow B).

    Steps
    -----
    1. Sample — load a pre-sampled parquet or live Spark sample.
    2. Profile — GE triage + Arabic detection.
    3. Detect  — Presidio + GLiNER produce raw scores.
    4. Refine  — optional LLM refinement (Milestone 2; stub in M1).
    5. Decide  — apply equation to set ``detected`` and ``confidence``.
    6. Write   — persist artefacts and upsert contract store.

    Parameters
    ----------
    config:
        Typed pipeline configuration.
    backend:
        Storage backend (``LocalBackend`` or ``S3Backend``).
    store:
        Contract store service — the single writer to ``pii-contracts``.
    """

    def __init__(
        self,
        config:  PipelineConfig,
        backend: StorageBackend,
        store:   ContractStore,
    ) -> None:
        self.config  = config
        self.backend = backend
        self.store   = store
        self.run_id  = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        self._run_writer: Optional[RunOutputWriter] = None

    # ── Factory for CLI compatibility ─────────────────────────────────────

    @classmethod
    def from_args(
        cls,
        args,
        backend: StorageBackend,
        store:   ContractStore,
    ) -> "PIIDetectionRunner":
        """
        Build a ``PIIDetectionRunner`` from an ``argparse.Namespace``.

        Kept for CLI backward compatibility.  Library consumers should
        construct ``PipelineConfig`` directly.
        """
        config = PipelineConfig(
            table           = args.table,
            output_dir      = Path(getattr(args, "output_dir", "./reports")),
            equation        = getattr(args, "equation",       "independent"),
            strategy        = getattr(args, "strategy",       "partition_picker"),
            thresholds      = Thresholds(
                presidio_min               = getattr(args, "presidio_min",               0.80),
                gliner_min                 = getattr(args, "gliner_min",                 0.70),
                llm_min                    = getattr(args, "llm_min",                    0.82),
                ge_triage_min              = getattr(args, "ge_triage_min",              0.0),
                very_high_confidence_floor = getattr(args, "very_high_confidence_floor", 0.90),
            ),
            enable_llm      = getattr(args, "enable_llm",     False),
            runs_bucket     = getattr(args, "s3_runs_bucket", "pii-reports"),
            contracts_bucket= getattr(args, "s3_contracts_bucket", "pii-contracts"),
        )
        return cls(config, backend, store)

    # ── Main entry point ──────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Execute the full PII pipeline and return the run manifest dict.

        The manifest contains ``status``, ``pii_summary``,
        ``layer_durations_ms``, and ``errors``.
        """
        manifest = self._init_manifest()
        try:
            logger.info(f"Starting PII Detection Scan for {self.config.table}")

            with timed_step() as t:
                logger.info("Step 1/5: Loading sampled data...")
                df = load_sample(
                    self.config.table,
                    self.config.output_dir,
                    self.config.strategy,
                )
            manifest["layer_durations_ms"]["sampling"] = t.elapsed_ms
            logger.info(f"Loaded {len(df)} rows.")

            with timed_step() as t:
                logger.info("Step 2/4: Running PII detection (Regex + NER Model)...")
                raw = detect_pii(df)
            manifest["layer_durations_ms"]["detection"] = t.elapsed_ms

            if self.config.enable_llm:
                with timed_step() as t:
                    logger.info("Step 3/4: Running LLM refinement...")
                    raw = refine_detections(raw, self.config.thresholds)
                manifest["layer_durations_ms"]["llm_refinement"] = t.elapsed_ms
            else:
                logger.info("Step 3/4: LLM refinement disabled. Skipping.")

            logger.info("Step 4/4: Applying decision equations and writing outputs...")
            detections = []
            for d in raw:
                verdict = decide_pii(d, self.config.equation, self.config.thresholds)
                if verdict.detected:
                    logger.info(f"  🚨 PII Detected in '{verdict.column}': {verdict.entity_type} (Confidence: {verdict.confidence:.2f})")
                else:
                    logger.info(f"  ✅ '{verdict.column}' is clean.")
                detections.append(verdict)

            # Apply edge rules post-verdict (convergence with modern pipeline path)
            edge_rules_enabled = getattr(self.config, "edge_rules_enabled", True)
            if edge_rules_enabled:
                try:
                    from redibis.classification.edge_rules import apply_edge_rules_to_detection
                    from redibis.classification.pack_store import load_pack

                    pack_name = getattr(self.config, "policy_pack", None) or "telecom"
                    policy = load_pack(pack_name)
                    refined = []
                    for d in detections:
                        updated, _ = apply_edge_rules_to_detection(d, policy, df=df)
                        refined.append(updated)
                    detections = refined
                except Exception as exc:
                    logger.warning(f"Edge rules skipped in legacy runner: {exc}")

            with timed_step() as t:
                output = self._write_outputs(df, detections)
            manifest["layer_durations_ms"]["writing"] = t.elapsed_ms

            manifest["pii_summary"] = {
                "scanned":  output.get("total_scanned", 0),
                "detected": output.get("detected_count", 0),
            }
            manifest["status"] = "success"
            logger.info(f"Scan complete! Detected PII in {output.get('detected_count', 0)}/{output.get('total_scanned', 0)} columns.")

        except Exception as exc:
            manifest["status"] = "failed"
            manifest["errors"].append(str(exc))
            logger.error(f"Scan failed: {exc}")
            raise
        finally:
            manifest["scan_completed_at"] = utc_iso()
            if self._run_writer:
                self._run_writer.write("run_manifest.json", manifest)

        return manifest

    # ── Private helpers ───────────────────────────────────────────────────

    def _init_manifest(self) -> dict:
        return {
            "workflow":            "pii",
            "table":               self.config.table,
            "run_id":              self.run_id,
            "equation":            self.config.equation,
            "status":              "pending",
            "scan_started_at":     utc_iso(),
            "scan_completed_at":   None,
            "layer_durations_ms":  {},
            "errors":              [],
        }

    def _write_outputs(
        self,
        df:         pd.DataFrame,
        detections: list[PIIDetection],
    ) -> dict:
        self._run_writer = RunOutputWriter(
            backend  = self.backend,
            bucket   = self.config.runs_bucket,
            workflow = "pii",
            table    = self.config.table,
            run_id   = self.run_id,
        )

        run_metadata = RunMetadata(
            run_id              = self.run_id,
            scan_date           = utc_iso(),
            equation_used       = self.config.equation,
            table_physical_name = self.config.table,
        )

        return write_pii_run_outputs(
            df,
            detections,
            run_metadata,
            table            = self.config.table,
            run_id           = self.run_id,
            equation_used    = self.config.equation,
            output_dir       = self.config.output_dir,
            backend          = self.backend,
            store            = self.store,
            runs_bucket      = self.config.runs_bucket,
            generate_ge_docs = self.config.generate_ge_docs,
            profiler         = None,
        )


# Backward-compat aliases (deprecated — will be removed in v2.0)
PIIPipeline = PIIDetectionRunner
