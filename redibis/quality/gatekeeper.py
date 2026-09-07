"""
redibis.quality.gatekeeper
==========================
QualityGatekeeper — runs GE expectation suites, generates Data Docs,
and exports quality-only ODCS contracts.

This is the QUALITY package's gatekeeper. It handles:
  - GE context lifecycle (in-memory or file-backed)
  - DataFrame attachment (Pandas or Spark)
  - Expectation ingestion, curation, and execution
  - GE Data Docs generation and folder copy (Option B)
  - Quality-only ODCS contract export
  - SQL validation (DuckDB for Pandas, native for Spark)

PII-specific methods (add_pii_expectation, register_pii_summary, etc.)
have been extracted to ``redibis.pii.quality_bridge.PIIQualityBridge``.
That class takes a QualityGatekeeper instance and adds PII expectations
onto it, preserving the one-way dependency direction.

Usage (quality-only workflow)
-----
    qa = QualityGatekeeper(
        suite_name       = "telecom_quality",
        in_memory        = False,
        context_root_dir = "./ge_project",
    )
    qa.attach_dataframe(df, dataset_name="telecom_customers")
    qa.merge_expectations(profiler.expectations)
    qa.add_table_quality_checks(min_rows=100)
    qa.add_column_quality_checks("phone", not_null=True)
    results = qa.run_tests(stage="quality_only", generate_docs=True)
    qa.copy_data_docs_to("./run_output/ge_report")
    contract = qa.export_quality_contract(
        database_name = "telecom",
        table_name    = "customers",
    )
    # contract is a dict — pass to ContractStore.upsert(workflow="quality")

Usage (with PII bridge — from the pii package)
-----
    from redibis.pii.quality_bridge import PIIQualityBridge
    bridge = PIIQualityBridge(qa)
    for det in detections:
        bridge.register(det)
    bridge.set_run_metadata(run_id="...", equation_used="balanced")
    results = qa.run_tests(generate_docs=True)
    full_contract = bridge.export_full_contract(
        database_name="telecom", table_name="customers",
        output_path="contract.yaml",
    )
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
import yaml

try:
    import great_expectations as gx
    from great_expectations.data_context.types.base import (
        DataContextConfig,
        InMemoryStoreBackendDefaults,
    )
    _GX_AVAILABLE = True
except ImportError:
    _GX_AVAILABLE = False


def _emit_quality_decisions(report: dict) -> None:
    """Emit structured pass/fail decisions for each GE expectation."""
    from redibis.obs import DecisionRecord, decision

    for row in report.get("results") or []:
        col = row.get("column") or "Table-Level"
        passed = bool(row.get("success"))
        decision(
            DecisionRecord(
                stage="quality",
                fn="quality.gatekeeper.run_tests",
                table="",
                column=col if col != "Table-Level" else "",
                verdict="pass" if passed else "fail",
                confidence=1.0 if passed else 0.0,
                rule=str(row.get("rule") or "unknown"),
                inputs={
                    "expectation_type": row.get("rule") or "",
                    "passed": passed,
                    "failed": not passed,
                },
            )
        )


class QualityGatekeeper:
    """
    GE expectation suite manager + quality runner.

    Two context modes:
      in_memory=True  (cluster default) — no filesystem writes, no Data Docs.
      in_memory=False (local / CLI)     — file-backed, Data Docs enabled.

    This class has NO PII coupling. It does not import PIIDetection, does
    not build pii blocks, and does not emit pii_summary. For PII features,
    use PIIQualityBridge from redibis.pii.quality_bridge.
    """

    def __init__(
        self,
        suite_name:       str       = "pipeline_quality_suite",
        in_memory:        bool      = True,
        context_root_dir: Union[str, Path] = "./great_expectations_project",
    ) -> None:
        if not _GX_AVAILABLE:
            raise ImportError(
                "great_expectations is required for QualityGatekeeper. "
                "Install with: pip install redibis[ge]"
            )

        self.suite_name       = suite_name
        self.in_memory        = in_memory
        self.context_root_dir = Path(context_root_dir)
        self.df_type:          Optional[str] = None
        self.df_reference:     Any           = None
        self.dataset_name:     str           = "pipeline_data"
        self.validator:        Any           = None
        self.suite:            Any           = None

        # ── GE context init ──────────────────────────────────────────────
        if in_memory:
            project_config = DataContextConfig(
                store_backend_defaults=InMemoryStoreBackendDefaults()
            )
            self.context = gx.get_context(project_config=project_config)
        else:
            self.context_root_dir.mkdir(parents=True, exist_ok=True)
            config_path = self.context_root_dir / "great_expectations.yml"
            if not config_path.exists():
                self.context = gx.data_context.FileDataContext.create(
                    project_root_dir=self.context_root_dir,
                    usage_statistics_enabled=False,
                )
            else:
                self.context = gx.DataContext(
                    context_root_dir=self.context_root_dir
                )

        self.suite = self.context.add_or_update_expectation_suite(
            expectation_suite_name=suite_name
        )

    # ── DataFrame attachment ──────────────────────────────────────────────

    def attach_dataframe(
        self,
        df:           Union[pd.DataFrame, Any],
        dataset_name: str = "pipeline_data",
    ) -> "QualityGatekeeper":
        """
        Wraps the incoming DataFrame in a GE datasource + validator.
        Stores dataset_name for ODCS export's physicalName.
        """
        self.df_reference = df
        self.dataset_name = dataset_name
        datasource_name   = f"{dataset_name}_source"
        asset_name        = f"{dataset_name}_asset"

        if isinstance(df, pd.DataFrame):
            self.df_type = "pandas"
            datasource   = self.context.sources.add_or_update_pandas(
                name=datasource_name
            )
        else:
            self.df_type = "spark"
            datasource   = self.context.sources.add_or_update_spark(
                name=datasource_name
            )

        try:
            data_asset = datasource.add_dataframe_asset(name=asset_name)
        except ValueError:
            data_asset = datasource.get_asset(asset_name)

        batch_request = data_asset.build_batch_request(dataframe=df)

        self.validator = self.context.get_validator(
            batch_request=batch_request,
            expectation_suite_name=self.suite_name,
        )
        return self

    # ── Expectation ingestion ─────────────────────────────────────────────

    def merge_expectations(
        self,
        new_expectations: List[Any],
    ) -> "QualityGatekeeper":
        """Ingests ExpectationConfiguration objects and merges into the suite."""
        if not new_expectations:
            return self

        for exp in new_expectations:
            self.suite.add_expectation(expectation_configuration=exp)

        self.context.add_or_update_expectation_suite(expectation_suite=self.suite)

        if self.validator:
            self.validator = self.context.get_validator(
                batch_request=self.validator.active_batch.batch_request,
                expectation_suite_name=self.suite_name,
            )
        return self

    # ── Quality check methods ─────────────────────────────────────────────

    def add_table_quality_checks(
        self,
        min_rows:         int                 = 1,
        max_rows:         Optional[int]       = None,
        expected_columns: Optional[List[str]] = None,
    ) -> "QualityGatekeeper":
        """Table-level: row count bounds and schema column set."""
        self._add_to_suite(
            "expect_table_row_count_to_be_between",
            min_value=min_rows, max_value=max_rows,
        )
        if expected_columns:
            self._add_to_suite(
                "expect_table_columns_to_match_set",
                column_set=expected_columns,
            )
        return self

    def add_column_quality_checks(
        self,
        column:    str,
        not_null:  bool                = True,
        unique:    bool                = False,
        type_list: Optional[List[str]] = None,
        mostly:    float               = 1.0,
    ) -> "QualityGatekeeper":
        """Column-level quality checks with tolerance."""
        if not_null:
            self._add_to_suite(
                "expect_column_values_to_not_be_null",
                column=column, mostly=mostly,
            )
        if unique:
            self._add_to_suite(
                "expect_column_values_to_be_unique",
                column=column, mostly=mostly,
            )
        if type_list:
            self._add_to_suite(
                "expect_column_values_to_be_of_type",
                column=column, type_list=type_list,
            )
        return self

    def add_percentage_quality_check(
        self,
        column:    str,
        value_set: List[Any],
        mostly:    float = 0.95,
    ) -> "QualityGatekeeper":
        """Categorical set membership check with tolerance."""
        self._add_to_suite(
            "expect_column_values_to_be_in_set",
            column=column, value_set=value_set, mostly=mostly,
        )
        return self

    def add_gx_expectation(
        self,
        expectation_name: str,
        **kwargs,
    ) -> "QualityGatekeeper":
        """
        Dynamic bridge to ANY built-in GX expectation by name.

        Deferred evaluation — the expectation is registered in the suite
        but NOT evaluated until you call run_tests(). This means:

          1. You can add 50 expectations without touching the data.
          2. run_tests() evaluates them all in a single pass.
          3. No wasted computation from double-evaluation.

        This is the recommended pattern for production pipelines.

        Usage:
            qa.add_gx_expectation(
                'expect_column_values_to_match_regex',
                column='phone',
                regex=r'^\\+20',
                mostly=0.8,
            )
            # Nothing evaluated yet — rule is just registered.
            results = qa.run_tests()  # single evaluation pass
        """
        self._add_to_suite(expectation_name, **kwargs)
        return self

    # ── Internal: deferred suite addition ─────────────────────────────────

    def _add_to_suite(
        self,
        expectation_name: str,
        **kwargs,
    ) -> None:
        """
        Add an expectation to the suite WITHOUT evaluating it.

        Supports both GE 0.17 (ExpectationConfiguration) and GE 1.x
        (Expectation class objects). Falls back to validator-based eager
        evaluation only if deferred addition fails entirely.
        """
        meta = kwargs.pop("meta", None)

        # ── Path 1: GE 0.17 — ExpectationConfiguration ───────────────
        try:
            from great_expectations.core.expectation_configuration import (
                ExpectationConfiguration,
            )
            ec = ExpectationConfiguration(
                expectation_type=expectation_name,
                kwargs=kwargs,
                meta=meta or {},
            )
            self.suite.add_expectation(expectation_configuration=ec)
            self.context.add_or_update_expectation_suite(
                expectation_suite=self.suite
            )
            return
        except (ImportError, AttributeError, TypeError):
            pass

        # ── Path 2: GE 1.x — Expectation class objects ───────────────
        try:
            import great_expectations.expectations as gx_exp
            # Convert snake_case name to PascalCase class name
            # e.g. "expect_column_values_to_not_be_null" -> "ExpectColumnValuesToNotBeNull"
            class_name = "".join(word.capitalize() for word in expectation_name.split("_"))
            exp_class = getattr(gx_exp, class_name, None)
            if exp_class is not None:
                exp_obj = exp_class(**kwargs)
                if meta:
                    exp_obj.meta = meta
                self.suite.add_expectation(exp_obj)
                return
        except (ImportError, AttributeError, TypeError) as e:
            pass

        # ── Path 3: fallback — validator eager evaluation ─────────────
        # This is the old behavior; used only if both deferred paths fail.
        if self.validator and hasattr(self.validator, expectation_name):
            if meta:
                kwargs["meta"] = meta
            getattr(self.validator, expectation_name)(**kwargs)
        else:
            raise AttributeError(
                f"Cannot add expectation '{expectation_name}'. "
                f"Not found as GE 0.17 ExpectationConfiguration, "
                f"GE 1.x Expectation class, or validator method."
            )

    def evaluate(
        self,
        expectation_name: str,
        **kwargs,
    ) -> dict:
        """
        Run a single expectation immediately and return the result.

        Unlike add_gx_expectation() which defers evaluation to run_tests(),
        this method evaluates NOW against the attached DataFrame and returns
        the result dict. The expectation is also added to the suite so it
        will be included in run_tests() later.

        Use this for:
          - Interactive exploration (try a rule, see if it works)
          - Quick one-off checks in a notebook
          - Debugging a rule's threshold before committing it

        Use add_gx_expectation() for:
          - Production pipelines (batch all rules, evaluate once)
          - Building a suite of 50+ rules efficiently

        Usage:
            result = qa.evaluate(
                'expect_column_values_to_match_regex',
                column='phone',
                regex=r'^\\+20',
                mostly=0.9,
            )
            print(result['success'])            # True/False
            print(result['unexpected_count'])    # how many failed
            print(result['unexpected_percent'])  # failure rate
            print(result['partial_unexpected'])  # sample bad values

        Returns:
            dict with keys: success, result, expectation_config, exception_info
        """
        if self.validator is None:
            raise RuntimeError(
                "No DataFrame attached. Call .attach_dataframe() first."
            )
        if not hasattr(self.validator, expectation_name):
            raise AttributeError(
                f"GX Validator has no expectation '{expectation_name}'. "
                "Check GX documentation for correct spelling."
            )

        # Run immediately on the validator (eager evaluation)
        raw_result = getattr(self.validator, expectation_name)(**kwargs)

        # Also register in the suite so run_tests() includes it
        self._add_to_suite(expectation_name, **kwargs)

        # Normalize the result into a clean dict
        if hasattr(raw_result, 'to_json_dict'):
            return raw_result.to_json_dict()
        if isinstance(raw_result, dict):
            return raw_result
        return {
            "success":            getattr(raw_result, 'success', None),
            "result":             getattr(raw_result, 'result', {}),
            "expectation_config": getattr(raw_result, 'expectation_config', {}),
        }

    # ── SQL validation ────────────────────────────────────────────────────

    def enforce_sql_rule(
        self,
        query: str,
        expected_violation_count: int = 0,
    ) -> "QualityGatekeeper":
        """
        Runs SQL against the attached DataFrame and asserts violation count.
        Spark: createOrReplaceTempView('pipeline_data').
        Pandas: DuckDB (pip install duckdb).
        """
        if self.df_type == "spark":
            self.df_reference.createOrReplaceTempView("pipeline_data")
            violations_df   = self.df_reference.sparkSession.sql(query)
            violation_count = violations_df.count()
        elif self.df_type == "pandas":
            try:
                import duckdb
            except ImportError:
                raise ImportError(
                    "duckdb is required for SQL validation on Pandas DataFrames. "
                    "Install with: pip install duckdb"
                )
            pipeline_data   = self.df_reference   # noqa: F841 — used by DuckDB
            violations_df   = duckdb.query(query).df()
            violation_count = len(violations_df)
        else:
            raise RuntimeError(
                "No DataFrame attached. Call .attach_dataframe() first."
            )

        if violation_count > expected_violation_count:
            raise ValueError(
                f"SQL Quality Check FAILED — "
                f"expected ≤{expected_violation_count} violations, "
                f"found {violation_count}. Query: {query}"
            )
        return self

    # ── GE Data Assistant ─────────────────────────────────────────────────

    def run_data_assistant(
        self,
        exclude_columns: Optional[List[str]] = None,
    ) -> "QualityGatekeeper":
        """Runs OnboardingDataAssistant directly inside the gatekeeper."""
        result = self.context.assistants.onboarding.run(
            batch_request=self.validator.active_batch.batch_request,
            exclude_column_names=exclude_columns or [],
        )
        self.suite = result.get_expectation_suite(self.suite_name)
        self.context.add_or_update_expectation_suite(
            expectation_suite=self.suite
        )
        self.validator = self.context.get_validator(
            batch_request=self.validator.active_batch.batch_request,
            expectation_suite_name=self.suite_name,
        )
        return self

    # ── Test execution ────────────────────────────────────────────────────

    def run_tests(
        self,
        stage:         str  = "after_pipeline",
        generate_docs: bool = True,
        open_browser:  bool = False,
    ) -> Any:
        """
        Runs the full expectation suite via a GE Checkpoint.
        Single evaluation pass — all expectations added via add_gx_expectation(),
        add_column_quality_checks(), merge_expectations(), etc. are evaluated
        together here, not before.

        generate_docs:
          in_memory=True  → silently ignored (no filesystem).
          in_memory=False → builds GX HTML Data Docs at context_root_dir.
        """
        # Sync suite to context and rebuild validator so it picks up
        # all deferred expectations added via _add_to_suite()
        self.context.add_or_update_expectation_suite(
            expectation_suite=self.suite
        )
        if self.validator:
            self.validator = self.context.get_validator(
                batch_request=self.validator.active_batch.batch_request,
                expectation_suite_name=self.suite_name,
            )

        checkpoint_name = f"checkpoint_{stage}"
        checkpoint = self.context.add_or_update_checkpoint(
            name=checkpoint_name,
            validator=self.validator,
        )

        run_name = (
            f"Stage: [{stage}] — "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')}"
        )
        results = checkpoint.run(
            result_format="SUMMARY",
            run_name=run_name,
        )

        if generate_docs and not self.in_memory:
            self.context.build_data_docs()

        _emit_quality_decisions(self._extract_report_data(results))
        return results

    # ── Data Docs folder copy (Option B) ──────────────────────────────────

    def copy_data_docs_to(
        self,
        target_dir: Union[str, Path],
        site_name:  str = "local_site",
    ) -> Path:
        """
        Copies the entire GE Data Docs site folder to target_dir.
        Preserves index.html + all CSS/JS/asset files so the report
        renders correctly when opened from the new location.

        Raises if in_memory=True or if the source site doesn't exist.
        """
        if self.in_memory:
            raise RuntimeError(
                "copy_data_docs_to() requires in_memory=False. "
                "Re-create the gatekeeper with in_memory=False to enable Data Docs."
            )

        source = self._data_docs_site_dir(site_name)
        if not source.exists():
            raise FileNotFoundError(
                f"GE Data Docs site not found at {source}. "
                "Did you call run_tests(generate_docs=True) first?"
            )

        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

        return target / "index.html"

    def _data_docs_site_dir(self, site_name: str = "local_site") -> Path:
        """Resolve GE Data Docs site path (layout differs across GE versions)."""
        candidates = [
            self.context_root_dir / "gx" / "uncommitted" / "data_docs" / site_name,
            self.context_root_dir / "uncommitted" / "data_docs" / site_name,
        ]
        for path in candidates:
            if path.is_dir():
                return path
        for index in self.context_root_dir.rglob("index.html"):
            if index.parent.name == site_name and "data_docs" in index.parts:
                return index.parent
        return candidates[0]

    # ── Custom quality report (works in-memory, no GE Data Docs needed) ───

    def export_quality_report(
        self,
        results:         Any,
        output_filename: Optional[str] = None,
        report_title:    str           = "Quality Suite Report",
        open_browser:    bool          = False,
    ) -> str:
        """
        Generate a self-contained HTML quality report from run_tests() results.

        Works in BOTH in_memory and file-backed modes — no GE Data Docs needed.
        The report is a single HTML file with embedded JS that shows:
          - Overall pass/fail stats
          - Results grouped by column (Table-Level + per-column categories)
          - Click to expand each category and see individual rule results
          - Pass/fail badges, unexpected counts, sample values

        Args:
            results         : The return value of run_tests()
            output_filename : File path to write. Defaults to
                              "result-{datetime}.html"
            report_title    : Title shown at the top of the report
            open_browser    : If True, opens the report in the default browser

        Returns:
            str : The file path that was written

        Usage:
            results = qa.run_tests(generate_docs=False)
            qa.export_quality_report(results, open_browser=True)
        """
        import webbrowser

        if output_filename is None:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
            output_filename = f"result-{ts}.html"

        # Extract validation data from GE results
        report_data = self._extract_report_data(results)
        report_json = json.dumps(report_data, ensure_ascii=False, default=str)

        html = _QUALITY_REPORT_TEMPLATE.replace("DATA_PLACEHOLDER", report_json)
        html = html.replace("TITLE_PLACEHOLDER", report_title)
        html = html.replace("SUITE_PLACEHOLDER", self.suite_name)
        html = html.replace("DATASET_PLACEHOLDER", self.dataset_name)

        Path(output_filename).parent.mkdir(parents=True, exist_ok=True)
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(html)

        if open_browser:
            webbrowser.open(f"file://{Path(output_filename).resolve()}")

        return output_filename

    def _extract_report_data(self, results: Any) -> dict:
        """Extract a normalized dict from GE checkpoint results (version-safe).

        Handles both:
          - GE 0.17: CheckpointResult.to_json_dict() with run_results
          - GE 1.x:  CheckpointResult.run_results → ValidationResult.to_json_dict()
        """
        overall_success = False
        statistics      = {}
        ge_results      = []

        # ── Path 1: try GE 1.x (run_results is a dict of ValidationResult objects) ──
        try:
            if hasattr(results, 'run_results') and isinstance(results.run_results, dict):
                overall_success = getattr(results, 'success', False)
                for _key, val in results.run_results.items():
                    # GE 1.x: val is ExpectationSuiteValidationResult
                    if hasattr(val, 'to_json_dict'):
                        vr = val.to_json_dict()
                    elif hasattr(val, 'results'):
                        # Direct attribute access
                        vr = {
                            "statistics": getattr(val, 'statistics', {}),
                            "results": [],
                        }
                        for r in val.results:
                            vr["results"].append(r if isinstance(r, dict)
                                                 else r.to_json_dict()
                                                 if hasattr(r, 'to_json_dict')
                                                 else {})
                    elif isinstance(val, dict):
                        vr = val.get("validation_result", val)
                    else:
                        continue

                    statistics = vr.get("statistics", statistics)
                    ge_results.extend(vr.get("results", []))
        except Exception:
            pass

        # ── Path 2: fallback to GE 0.17 (to_json_dict on the checkpoint result) ──
        if not ge_results:
            try:
                if hasattr(results, 'to_json_dict'):
                    raw = results.to_json_dict()
                elif isinstance(results, dict):
                    raw = results
                else:
                    raw = {}

                overall_success = raw.get("success", False)
                run_results = raw.get("run_results", {})
                if run_results:
                    first_key = next(iter(run_results))
                    rr = run_results[first_key]
                    vr = rr.get("validation_result", rr)
                    statistics = vr.get("statistics", {})
                    ge_results = vr.get("results", [])
                elif "results" in raw:
                    statistics = raw.get("statistics", {})
                    ge_results = raw.get("results", [])
            except Exception:
                pass

        # ── Normalize each result into a flat dict ──
        normalized = []
        for r in ge_results:
            if not isinstance(r, dict):
                try:
                    r = r.to_json_dict() if hasattr(r, 'to_json_dict') else {}
                except Exception:
                    continue

            exp_config  = r.get("expectation_config", {})
            # GE 1.x uses "type", GE 0.17 uses "expectation_type"
            exp_type    = (exp_config.get("type")
                          or exp_config.get("expectation_type")
                          or "unknown")
            kwargs      = exp_config.get("kwargs", {})
            column      = kwargs.get("column", "Table-Level")
            result_data = r.get("result", {})
            meta        = exp_config.get("meta", r.get("meta", {}))

            normalized.append({
                "column":           column,
                "rule":             exp_type,
                "success":          r.get("success", False),
                "kwargs":           {k: v for k, v in kwargs.items()
                                     if k not in ("column", "batch_id")},
                "element_count":    result_data.get("element_count"),
                "unexpected_count": result_data.get("unexpected_count", 0),
                "unexpected_pct":   result_data.get("unexpected_percent", 0),
                "partial_unexpected": result_data.get(
                    "partial_unexpected_list", [])[:5],
                "observed_value":   result_data.get("observed_value"),
                "meta":             meta,
            })

        return {
            "success":    overall_success,
            "statistics": statistics,
            "results":    normalized,
            "suite_name": self.suite_name,
            "dataset":    self.dataset_name,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }

    # ── Quality contract export ───────────────────────────────────────────

    def export_quality_contract(
        self,
        database_name:  str,
        table_name:     str,
        output_path:    Optional[str] = None,
        column_dtypes:  Optional[dict] = None,
    ) -> dict:
        """
        Exports a quality-only ODCS v3.0.1 partial contract from the suite.

        Delegates to QualityContractWriter which uses official ODCS Pydantic
        models (if installed) and the GE→ODCS mapper to produce native ODCS
        quality rules where possible.

        The output is:
          - Validated against the ODCS schema
          - Exportable via `redibis.contracts.exporter.export_contract()`
            to Soda, dbt, GE, SQL, HTML, and 20+ other formats
          - Mergeable via ContractStore.upsert()

        Args:
            database_name : database identifier for the contract
            table_name    : table identifier for the contract
            output_path   : optional file path to write the YAML

        Returns:
            dict : the partial contract (also written to output_path if given)
        """
        from redibis.quality.contract_writer import QualityContractWriter

        writer = QualityContractWriter(
            database_name       = database_name,
            table_name          = table_name,
            physical_table_name = f"{database_name}.{table_name}",
            column_dtypes       = column_dtypes,
        )
        writer.add_expectations(list(self.suite.expectations))
        contract = writer.build()

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w") as f:
                yaml.dump(
                    contract, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )

        return contract

    # ── Legacy export (with PII support for backward compat) ──────────────

    def export_to_odcs(
        self,
        model_name:  Optional[str] = None,
        output_path: str           = "generated_contract.yaml",
    ) -> str:
        """
        Legacy export that includes PII blocks if any were registered
        via the bridge. For new code, use export_quality_contract() for
        quality-only or PIIQualityBridge.export_full_contract() for
        the combined output.

        This method is kept for backward compatibility.
        """
        # Split dataset_name into db.table for the new method
        parts = (model_name or self.dataset_name).replace("_", ".", 1).split(".", 1)
        db_name = parts[0] if len(parts) > 1 else ""
        tbl_name = parts[1] if len(parts) > 1 else parts[0]

        contract = self.export_quality_contract(
            database_name=db_name,
            table_name=tbl_name,
        )

        # If PII detections were registered (via bridge writing to this instance),
        # check if the bridge attached pii_meta onto expectations via the meta block
        for expectation in self.suite.expectations:
            meta = expectation.meta or {}
            if "pii" in meta:
                column = expectation.kwargs.get("column")
                if column:
                    for prop in contract["schema"][0]["properties"]:
                        if prop["name"] == column:
                            prop["pii"] = meta["pii"]
                            if meta["pii"].get("detected"):
                                entity = meta["pii"].get("entity_type", "")
                                from redibis.pii.sensitivity import classify_sensitivity
                                pii_class = classify_sensitivity(entity)
                                prop["classification"] = pii_class
                                if pii_class == "security_sensitive":
                                    tags = ["security_sensitive"]
                                elif pii_class == "pii_indirect":
                                    tags = ["pii_indirect", "gdpr_personal_data"]
                                else:
                                    tags = ["pii", "gdpr_personal_data"]
                                if meta["pii"].get("arabic_aware"):
                                    tags.append("contains_arabic")
                                prop["tags"] = tags

        # Write YAML
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            yaml.dump(
                contract, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        return output_path

    # ── Column triage (lightweight inline path) ───────────────────────────

    def compute_column_profiles(
        self,
        threshold: float = 0.30,
    ) -> list:
        """
        Computes structural column profiles for triage, directly from the
        attached DataFrame. Returns list[ColumnProfile].

        This is a convenience method for quick inline usage. For full
        profiling with GE expectations, use QualityProfiler separately.
        """
        if self.df_reference is None:
            return []

        if self.df_type == "pandas":
            pdf = self.df_reference
        else:
            try:
                pdf = self.df_reference.limit(5000).toPandas()
            except Exception:
                return []

        from redibis.quality.profiler import QualityProfiler
        _p = QualityProfiler.__new__(QualityProfiler)
        _p._df_pandas      = pdf
        _p.arabic_columns  = {}
        _p._triage_signals = []
        _p.expectations    = []
        _p.profile_arabic_presence()
        return _p.compute_column_profiles(threshold=threshold)

    # ── Introspection helpers ─────────────────────────────────────────────

    def list_expectations(self) -> List[dict]:
        """List all expectations currently in the suite as dicts."""
        return [
            {
                "expectation_type": exp.expectation_type,
                "kwargs": dict(exp.kwargs),
                "meta": exp.meta or {},
            }
            for exp in self.suite.expectations
        ]

    def get_quality_results(self) -> List[dict]:
        """
        Returns the last run's validation results as a list of dicts.
        Call after run_tests().
        """
        # This is a placeholder — actual implementation needs to extract
        # from the GE checkpoint results object.
        return []


_QUALITY_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>TITLE_PLACEHOLDER</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root { --bg:#f8fafc; --surface:#fff; --border:#e2e8f0; --text:#1e293b;
          --muted:#64748b; --green:#22c55e; --red:#ef4444; --amber:#f59e0b;
          --accent:#4f46e5; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text); }
  .mono { font-family:'SF Mono','Cascadia Code','Consolas',monospace; }
  .header { background:#0f172a; color:#fff; padding:1.5rem 2rem; }
  .header h1 { font-size:1.4rem; font-weight:800; }
  .header .sub { color:#94a3b8; font-size:.8rem; margin-top:.3rem; }
  .hero { display:flex; gap:1px; background:var(--border); border-bottom:1px solid var(--border); }
  .hero-cell { flex:1; background:var(--surface); padding:1rem; text-align:center; }
  .hero-cell .label { font-size:.6rem; text-transform:uppercase; letter-spacing:.08em;
                      color:var(--muted); font-weight:700; }
  .hero-cell .val { font-size:2rem; font-weight:800; margin-top:.15rem; }
  .hero-cell .val.pass { color:var(--green); }
  .hero-cell .val.fail { color:var(--red); }
  .hero-cell .val.pct { color:var(--accent); }

  .overall { margin:1rem 1.5rem; padding:.75rem 1.25rem; border-radius:8px; font-weight:700;
             font-size:.9rem; display:flex; align-items:center; gap:.6rem; }
  .overall.pass { background:#f0fdf4; color:#166534; border:1px solid #bbf7d0; }
  .overall.fail { background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }
  .overall .dot { width:12px; height:12px; border-radius:50%; }
  .overall.pass .dot { background:var(--green); }
  .overall.fail .dot { background:var(--red); }

  .cat { border:1px solid var(--border); border-radius:10px; margin:.75rem 1.5rem;
         background:var(--surface); overflow:hidden; }
  .cat-hdr { display:flex; align-items:center; justify-content:space-between;
             padding:.75rem 1.25rem; cursor:pointer; user-select:none;
             background:#f8fafc; border-bottom:1px solid transparent; transition:all .15s; }
  .cat-hdr:hover { background:#f1f5f9; }
  .cat-hdr.open { border-bottom-color:var(--border); }
  .cat-hdr .arrow { transition:transform .2s; font-size:.7rem; color:var(--muted); margin-right:.6rem; }
  .cat-hdr.open .arrow { transform:rotate(90deg); }
  .cat-name { font-weight:700; font-size:.85rem; }
  .cat-stats { display:flex; gap:.5rem; font-size:.7rem; font-weight:600; }
  .badge { padding:1px 7px; border-radius:8px; font-size:.65rem; font-weight:700; }
  .badge.p { background:#dcfce7; color:#166534; }
  .badge.f { background:#fee2e2; color:#991b1b; }
  .badge.cnt { background:#e0e7ff; color:#4338ca; }
  .cat-body { display:none; }
  .cat-body.open { display:block; }

  .rule { display:flex; align-items:flex-start; gap:.75rem; padding:.6rem 1.25rem;
          border-bottom:1px solid #f1f5f9; font-size:.82rem; }
  .rule:last-child { border-bottom:none; }
  .rule:hover { background:#fafbfe; }
  .rule .icon { font-size:.9rem; margin-top:.1rem; flex-shrink:0; }
  .rule .body { flex:1; min-width:0; }
  .rule .name { font-weight:600; }
  .rule .detail { font-size:.72rem; color:var(--muted); margin-top:.15rem; font-family:'SF Mono',monospace; }
  .rule .unexpected { font-size:.7rem; color:var(--red); margin-top:.15rem; }
  .rule .unexpected .samples { color:var(--muted); font-style:italic; }
</style>
</head>
<body>

<div class="header">
  <h1>TITLE_PLACEHOLDER</h1>
  <div class="sub">Suite: <strong>SUITE_PLACEHOLDER</strong> &middot;
    Dataset: <strong>DATASET_PLACEHOLDER</strong> &middot;
    <span id="ts"></span></div>
</div>
<div class="hero" id="hero"></div>
<div id="overall"></div>
<div id="categories"></div>

<script>
const data = DATA_PLACEHOLDER;

document.getElementById('ts').textContent = data.timestamp
  ? new Date(data.timestamp).toLocaleString() : '';

// ── Hero stats ─────────────────────────────────────────────────────
const stats = data.statistics || {};
const total  = stats.evaluated_expectations || data.results.length;
const passed = stats.successful_expectations
  || data.results.filter(r => r.success).length;
const failed = total - passed;
const pct    = total > 0 ? Math.round((passed / total) * 100) : 0;

document.getElementById('hero').innerHTML = `
  <div class="hero-cell"><div class="label">Total Checks</div><div class="val">${total}</div></div>
  <div class="hero-cell"><div class="label">Passed</div><div class="val pass">${passed}</div></div>
  <div class="hero-cell"><div class="label">Failed</div><div class="val fail">${failed}</div></div>
  <div class="hero-cell"><div class="label">Pass Rate</div><div class="val pct">${pct}%</div></div>`;

document.getElementById('overall').innerHTML = `
  <div class="overall ${data.success ? 'pass' : 'fail'}">
    <div class="dot"></div>
    ${data.success ? 'All checks passed' : failed + ' check(s) failed'}
  </div>`;

// ── Group by column ────────────────────────────────────────────────
const groups = {};
data.results.forEach(r => {
  const k = r.column || 'Table-Level';
  if (!groups[k]) groups[k] = [];
  groups[k].push(r);
});

const expanded = new Set();

function renderCategories() {
  const container = document.getElementById('categories');
  container.innerHTML = '';
  const sortedKeys = Object.keys(groups).sort((a, b) => {
    if (a === 'Table-Level') return -1;
    if (b === 'Table-Level') return 1;
    return a.localeCompare(b);
  });

  sortedKeys.forEach(cat => {
    const rules = groups[cat];
    const catPassed = rules.filter(r => r.success).length;
    const catFailed = rules.length - catPassed;
    const isOpen = expanded.has(cat);
    const isTable = cat === 'Table-Level';

    const section = document.createElement('div');
    section.className = 'cat';

    const hdr = document.createElement('div');
    hdr.className = 'cat-hdr' + (isOpen ? ' open' : '');
    hdr.innerHTML = `
      <div style="display:flex;align-items:center">
        <span class="arrow">&#9654;</span>
        <span class="cat-name">${isTable ? 'Table-Level' : cat}</span>
      </div>
      <div class="cat-stats">
        <span class="badge cnt">${rules.length}</span>
        <span class="badge p">${catPassed} pass</span>
        ${catFailed > 0 ? '<span class="badge f">' + catFailed + ' fail</span>' : ''}
      </div>`;
    hdr.onclick = () => {
      if (expanded.has(cat)) expanded.delete(cat); else expanded.add(cat);
      renderCategories();
    };
    section.appendChild(hdr);

    const body = document.createElement('div');
    body.className = 'cat-body' + (isOpen ? ' open' : '');

    rules.forEach(r => {
      const row = document.createElement('div');
      row.className = 'rule';

      const icon = r.success ? '&#10004;' : '&#10008;';
      const iconColor = r.success ? 'var(--green)' : 'var(--red)';

      let detailParts = [];
      if (r.kwargs && Object.keys(r.kwargs).length > 0) {
        detailParts.push(Object.entries(r.kwargs)
          .map(([k,v]) => k + '=' + JSON.stringify(v)).join(', '));
      }
      if (r.element_count != null) detailParts.push('rows: ' + r.element_count);

      let unexpectedHtml = '';
      if (!r.success && r.unexpected_count > 0) {
        unexpectedHtml = '<div class="unexpected">' +
          r.unexpected_count + ' unexpected (' +
          (r.unexpected_pct != null ? r.unexpected_pct.toFixed(2) : '?') + '%)';
        if (r.partial_unexpected && r.partial_unexpected.length > 0) {
          unexpectedHtml += ' <span class="samples">samples: ' +
            r.partial_unexpected.slice(0, 3).map(v => JSON.stringify(v)).join(', ') +
            '</span>';
        }
        unexpectedHtml += '</div>';
      } else if (!r.success && r.observed_value != null) {
        unexpectedHtml = '<div class="unexpected">observed: ' +
          JSON.stringify(r.observed_value) + '</div>';
      }

      row.innerHTML = `
        <div class="icon" style="color:${iconColor}">${icon}</div>
        <div class="body">
          <div class="name">${r.rule}</div>
          ${detailParts.length > 0 ? '<div class="detail">' + detailParts.join(' &middot; ') + '</div>' : ''}
          ${unexpectedHtml}
        </div>`;
      body.appendChild(row);
    });

    section.appendChild(body);
    container.appendChild(section);
  });
}

renderCategories();
</script>
</body>
</html>"""


# ── Backward-compat aliases (deprecated — will be removed in v2.0) ────────
DataQualityGatekeeper = QualityGatekeeper