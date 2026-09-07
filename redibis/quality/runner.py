"""
pii_detection.orchestrator.ge_pipeline
========================================
Workflow A — GE-only quality scan.

Programmatic usage
------------------
    from redibis.quality.runner import QualityRunner
    from redibis.pipeline_config import GEPipelineConfig
    from redibis.store.storage_backend       import LocalBackend
    from redibis.store.contract_store   import ContractStore

    backend = LocalBackend("./dev_storage")
    store   = ContractStore(backend, bucket="pii-contracts")
    config  = GEPipelineConfig(table="telecom.customers")
    result  = QualityRunner(config, backend, store).run()

CLI usage (internal)
--------------------
    QualityRunner.from_args(argparse_namespace, backend, store).run()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from redibis.pipeline_config import GEPipelineConfig
from redibis.profiling.base import ProfileResult
from redibis.runner_utils import utc_iso, timed_step, load_sample
from redibis.quality.run_outputs import write_ge_run_outputs
from redibis.store.storage_backend import StorageBackend, RunOutputWriter
from redibis.store.contract_store import ContractStore

logger = logging.getLogger("pii.ge_pipeline")


class QualityRunner:
    """
    Orchestrates the GE-only quality scan (Workflow A).

    Steps
    -----
    1. Sample  — load pre-sampled parquet.
    2. Profile — GE OnboardingDataAssistant + Arabic detection.
    3. Write   — GE Data Docs + quality-only ODCS partial + upsert.

    Parameters
    ----------
    config:
        Typed GE pipeline configuration.
    backend:
        Storage backend.
    store:
        Contract store service.
    """

    def __init__(
        self,
        config:  GEPipelineConfig,
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
    ) -> "QualityRunner":
        """Build a ``QualityRunner`` from an ``argparse.Namespace``."""
        config = GEPipelineConfig(
            table            = args.table,
            output_dir       = Path(getattr(args, "output_dir",       "./reports")),
            strategy         = getattr(args, "strategy",              "partition_picker"),
            runs_bucket      = getattr(args, "s3_runs_bucket",        "pii-reports"),
            contracts_bucket = getattr(args, "s3_contracts_bucket",   "pii-contracts"),
        )
        return cls(config, backend, store)

    # ── Main entry point ──────────────────────────────────────────────────

    def run(self) -> dict:
        """Execute the GE pipeline and return the run manifest dict."""
        manifest = self._init_manifest()
        try:
            logger.info(f"Starting Quality Scan for {self.config.table}")

            with timed_step() as t:
                logger.info("Step 1/3: Loading sampled data...")
                df = load_sample(
                    self.config.table,
                    self.config.output_dir,
                    self.config.strategy,
                )
            manifest["layer_durations_ms"]["sampling"] = t.elapsed_ms
            logger.info(f"Loaded {len(df)} rows.")

            with timed_step() as t:
                logger.info("Step 2/3: Running quality profiler...")
                profiler = self._profile(df)
            manifest["layer_durations_ms"]["profiling"] = t.elapsed_ms

            with timed_step() as t:
                logger.info("Step 3/3: Writing GE Data Docs and contracts...")
                output = write_ge_run_outputs(
                    df,
                    profiler,
                    table       = self.config.table,
                    run_id      = self.run_id,
                    output_dir  = self.config.output_dir,
                    backend     = self.backend,
                    store       = self.store,
                    runs_bucket = self.config.runs_bucket,
                )
            manifest["layer_durations_ms"]["writing"] = t.elapsed_ms

            manifest["quality_summary"] = output.get("quality_summary", {})
            manifest["status"] = "success"
            logger.info("Quality scan complete!")

        except Exception as exc:
            manifest["status"] = "failed"
            manifest["errors"].append(str(exc))
            logger.error(f"Quality scan failed: {exc}")
            raise
        finally:
            manifest["scan_completed_at"] = utc_iso()
            self._run_writer = RunOutputWriter(
                backend  = self.backend,
                bucket   = self.config.runs_bucket,
                workflow = "ge",
                table    = self.config.table,
                run_id   = self.run_id,
            )
            self._run_writer.write("run_manifest.json", manifest)

        return manifest

    # ── Private helpers ───────────────────────────────────────────────────

    def _init_manifest(self) -> dict:
        return {
            "workflow":           "ge",
            "table":              self.config.table,
            "run_id":             self.run_id,
            "status":             "pending",
            "scan_started_at":    utc_iso(),
            "scan_completed_at":  None,
            "layer_durations_ms": {},
            "errors":             [],
        }

    def _profile(self, df: pd.DataFrame) -> ProfileResult:
        from redibis.services import pipeline
        return pipeline.profile_dataframe(
            df,
            self.config.table.replace(".", "_"),
            triage_threshold=0.0,
        )


# Backward-compat aliases (deprecated — will be removed in v2.0)
GEPipeline = QualityRunner
