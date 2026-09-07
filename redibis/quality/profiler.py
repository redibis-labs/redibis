"""
GE-backed column profiler (legacy Workflow A engine).

Note: ``Profiler`` in ``redibis.profiling`` is the strategy ABC; this
``QualityProfiler`` class is the Great Expectations implementation only.
A future rename to ``GEProfilerBackend`` is planned when direct callers migrate.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import webbrowser
from typing import Any, Dict, List, Optional, Union

import pandas as pd

try:
    import great_expectations as gx
    from great_expectations.data_context.types.base import (
        DataContextConfig,
        InMemoryStoreBackendDefaults,
    )
    from great_expectations.render.renderer import ExpectationSuitePageRenderer
    from great_expectations.render.view import DefaultJinjaPageView
    _GX_AVAILABLE = True
except ImportError:
    gx = None  # type: ignore[assignment]
    DataContextConfig = None  # type: ignore[assignment,misc]
    InMemoryStoreBackendDefaults = None  # type: ignore[assignment,misc]
    ExpectationSuitePageRenderer = None  # type: ignore[assignment,misc]
    DefaultJinjaPageView = None  # type: ignore[assignment,misc]
    _GX_AVAILABLE = False

_GX_IMPORT_ERROR = (
    "great_expectations is required for QualityProfiler. "
    "Install with: pip install redibis[ge]"
)

from redibis.models import (
    ColumnProfile,
    PII_NAME_HINTS,
    arabic_regex_for_ge,
)
# Legacy private alias used by profiler internals
_PII_NAME_HINTS = PII_NAME_HINTS

logger = logging.getLogger("pii.profiler")


# ─────────────────────────────────────────────────────────────────────────────
# QualityProfiler
# ─────────────────────────────────────────────────────────────────────────────

class QualityProfiler:
    """
    Ephemeral profiling sandbox for engineers.
    Runs entirely in memory — no GE filesystem.
    Produces draft suite + Arabic map + triage signals for Layer 3.
    """

    def __init__(
        self,
        df: Union[pd.DataFrame, "pyspark.sql.DataFrame"],
        dataset_name: str = "profiling_sample",
    ):
        if not _GX_AVAILABLE:
            raise ImportError(_GX_IMPORT_ERROR)

        # ── In-memory context ────────────────────────────────────────────
        project_config = DataContextConfig(
            store_backend_defaults=InMemoryStoreBackendDefaults()
        )
        self.context      = gx.get_context(project_config=project_config)
        self.dataset_name = dataset_name

        # ── State ────────────────────────────────────────────────────────
        self.expectations:    List[Any]              = []
        self.arabic_columns:  Dict[str, float]       = {}
        self._triage_signals: List[ColumnProfile] = []
        self._df_pandas:      Optional[pd.DataFrame] = None

        # ── Datasource setup ─────────────────────────────────────────────
        datasource_name = f"{dataset_name}_source"
        asset_name      = f"{dataset_name}_asset"

        if isinstance(df, pd.DataFrame):
            self._df_pandas = df
            datasource = self.context.sources.add_or_update_pandas(
                name=datasource_name
            )
        else:
            try:
                self._df_pandas = df.limit(5000).toPandas()
            except Exception:
                self._df_pandas = None
            datasource = self.context.sources.add_or_update_spark(
                name=datasource_name
            )

        try:
            data_asset = datasource.add_dataframe_asset(name=asset_name)
        except ValueError:
            data_asset = datasource.get_asset(asset_name)

        self.batch_request = data_asset.build_batch_request(dataframe=df)

        self.suite_name = "temp_profiling_suite"
        self.context.add_or_update_expectation_suite(
            expectation_suite_name=self.suite_name
        )
        self.validator = self.context.get_validator(
            batch_request=self.batch_request,
            expectation_suite_name=self.suite_name,
        )

    @property
    def triage_signals(self) -> List[ColumnProfile]:
        """
        PII triage signals computed by ``compute_column_profiles()``.
        Returns an empty list if ``compute_column_profiles()`` has not been called yet.
        """
        return self._triage_signals

    # ── 1. Profiling ──────────────────────────────────────────────────────

    def run_assistant(
        self,
        exclude_columns: Optional[List[str]] = None,
    ) -> "QualityProfiler":
        """
        Runs GE OnboardingDataAssistant and stores draft expectations in memory.
        Auto-generates ~50 structural expectations covering all columns.
        """
        logger.info("Profiling data distributions and inferring rules...")
        result = self.context.assistants.onboarding.run(
            batch_request=self.batch_request,
            exclude_column_names=exclude_columns or [],
        )
        draft_suite       = result.get_expectation_suite("draft")
        self.expectations = draft_suite.expectations
        logger.info(
            "Found %d potential rules.",
            len(self.expectations),
        )
        return self

    def _append_expectation(
        self,
        expectation_type: str,
        *,
        meta: Optional[dict] = None,
        **kwargs,
    ) -> None:
        """Register a deferred expectation on the suite and ``self.expectations``."""
        from great_expectations.core.expectation_configuration import (
            ExpectationConfiguration,
        )

        ec = ExpectationConfiguration(
            expectation_type=expectation_type,
            kwargs=kwargs,
            meta=meta or {},
        )
        self.expectations.append(ec)
        suite = self.context.get_expectation_suite(
            expectation_suite_name=self.suite_name,
        )
        suite.add_expectation(expectation_configuration=ec)
        self.context.add_or_update_expectation_suite(expectation_suite=suite)

    # ── 2. Arabic presence detection ──────────────────────────────────────

    def profile_arabic_presence(
        self,
        columns: Optional[List[str]] = None,
        threshold: float = 0.05,
    ) -> "QualityProfiler":
        """
        Detects which columns contain Arabic-script values.
        Populates self.arabic_columns: dict[column → arabic_fraction].
        """
        if self._df_pandas is None:
            logger.warning("No Pandas DataFrame available for Arabic detection.")
            return self

        from redibis.profiling import triage as profiling_triage

        self.arabic_columns = profiling_triage.profile_arabic_presence(
            self._df_pandas,
            columns=columns,
        )

        ge_regex = arabic_regex_for_ge()
        for col, fraction in self.arabic_columns.items():
            if fraction >= threshold:
                try:
                    self._append_expectation(
                        "expect_column_values_to_match_regex",
                        column=col,
                        regex=ge_regex,
                        mostly=threshold,
                        meta={
                            "notes": {
                                "format": "markdown",
                                "content": (
                                    f"🔹 **Arabic presence**: {fraction:.1%} of values "
                                    "contain Arabic characters. "
                                    "Activate Arabic-aware recognizers in Layer 3."
                                ),
                            }
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to register Arabic expectation for %s: %s",
                        col, exc,
                    )

        flagged = [c for c, f in self.arabic_columns.items() if f >= threshold]
        logger.info(
            "Arabic detection complete. %d column(s) flagged (>= %.0f%%): %s",
            len(flagged), threshold * 100, flagged or "none",
        )
        return self

    # ── 3. PII triage signals ─────────────────────────────────────────────

    def compute_column_profiles(
        self,
        threshold: float = 0.30,
        sample_size: int = 5,
    ) -> List[ColumnProfile]:
        """
        Computes a ColumnProfile for every string column.
        Weights: name=0.40  card=0.20  len=0.20  null=0.10  arabic=0.10
        Returns list sorted by triage_score descending.
        """
        if self._df_pandas is None:
            logger.warning("No Pandas DataFrame available for triage signals.")
            return []

        from redibis.profiling import triage as profiling_triage

        profiles = profiling_triage.compute_column_profiles(
            self._df_pandas,
            arabic_columns=self.arabic_columns,
            threshold=threshold,
            sample_size=sample_size,
        )
        self._triage_signals = profiles
        return profiles

    # ── 4. Review & curation ──────────────────────────────────────────────

    def review(self) -> "QualityProfiler":
        """Logs a detailed list of draft expectations with parameters."""
        if not self.expectations:
            logger.warning("No expectations to review. Call run_assistant() first.")
            return self

        lines = ["\n" + "=" * 80, "DRAFT EXPECTATIONS REVIEW", "=" * 80]
        for idx, exp in enumerate(self.expectations):
            col    = exp.kwargs.get("column", "[Table-Level]")
            rule   = exp.expectation_type
            params = {k: v for k, v in exp.kwargs.items() if k not in ("column", "batch_id")}
            lines.append(f"[{idx:>3}]  {col:<25} {rule}")
            lines.append(f"         -> {params or 'No extra parameters'}")
        lines.append("=" * 80)
        logger.info("\n".join(lines))
        return self

    def remove_rules(
        self,
        columns: Optional[List[str]] = None,
        rule_types: Optional[List[str]] = None,
    ) -> "QualityProfiler":
        """Remove rules by column name or expectation type."""
        columns    = columns    or []
        rule_types = rule_types or []
        before     = len(self.expectations)
        self.expectations = [
            e for e in self.expectations
            if e.kwargs.get("column") not in columns
            and e.expectation_type not in rule_types
        ]
        logger.info(
            "Removed %d rules; %d remain.",
            before - len(self.expectations), len(self.expectations),
        )
        return self

    def drop_rules_by_index(self, indices_to_drop: List[int]) -> "QualityProfiler":
        """Remove specific rules by their zero-based index."""
        if not indices_to_drop:
            return self
        before = len(self.expectations)
        for idx in sorted(set(indices_to_drop), reverse=True):
            if 0 <= idx < len(self.expectations):
                removed  = self.expectations.pop(idx)
                col_name = removed.kwargs.get("column", "[Table-Level]")
                logger.debug(
                    "Dropped [%d]: %s (column: %s)",
                    idx, removed.expectation_type, col_name,
                )
        logger.info(
            "%d rules removed; %d remain.",
            before - len(self.expectations), len(self.expectations),
        )
        return self

    # Backward-compat alias for the old camelCase name
    drop_rules_byIndex = drop_rules_by_index

    # ── 5. HTML reports ───────────────────────────────────────────────────

    def export_html_review(
        self,
        output_filename: str = "profiler_draft_review.html",
        open_browser: bool   = False,
    ) -> "QualityProfiler":
        """Generates the native GX HTML report with Draft Index in notes."""
        if not self.expectations:
            logger.warning('No draft rules to render. Call run_assistant() first.')
            return self

        logger.info("Generating GX HTML report with index numbers...")
        temp_suite_name = "html_render_temp_suite"
        temp_suite = self.context.add_or_update_expectation_suite(temp_suite_name)

        for idx, exp in enumerate(self.expectations):
            exp_copy = copy.deepcopy(exp)
            exp_copy.meta["notes"] = {
                "format": "markdown",
                "content": (
                    f"DRAFT INDEX: [{idx}] — "
                    f"Use `.drop_rules_by_index([{idx}])` to remove."
                ),
            }
            temp_suite.add_expectation(expectation_configuration=exp_copy)

        try:
            document     = ExpectationSuitePageRenderer().render(temp_suite)
            html_content = DefaultJinjaPageView().render(document)
            output_path  = os.path.abspath(output_filename)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info("HTML report saved: %s", output_path)
            if open_browser:
                webbrowser.open(f"file://{output_path}")
        finally:
            self.context.delete_expectation_suite(temp_suite_name)

        return self

    def export_interactive_review(
        self,
        output_filename: str = "interactive_review.html",
        open_browser: bool   = False,
    ) -> "QualityProfiler":
        """Generates a custom interactive HTML dashboard for rule curation."""
        if not self.expectations:
            logger.warning('No draft rules to render. Call run_assistant() first.')
            return self

        logger.info("Generating interactive dashboard...")

        from redibis.quality.ge_codegen import render_gx_expectation_line

        rules_data = []
        for idx, exp in enumerate(self.expectations):
            col       = exp.kwargs.get("column", "Table-Level")
            rule_type = exp.expectation_type
            kwargs    = {
                k: v for k, v in exp.kwargs.items()
                if k not in ("column", "batch_id")
            }
            py_code = render_gx_expectation_line(
                rule_type,
                column=None if col == "Table-Level" else col,
                kwargs=kwargs,
            )
            rules_data.append({
                "id":          idx,
                "column":      col,
                "rule":        rule_type,
                "kwargs":      str(kwargs) if kwargs else "No additional parameters",
                # Structured params (as opposed to the display string above) so
                # the embedding page can send the kept rules to the backend
                # renderer instead of re-parsing the display text.
                "params":      kwargs,
                "python_code": py_code,
            })

        rules_json    = json.dumps(rules_data, default=str)
        final_html    = _INTERACTIVE_HTML_TEMPLATE.replace(
            "DATA_PLACEHOLDER", rules_json
        )

        output_path = os.path.abspath(output_filename)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_html)
        logger.info("Interactive dashboard saved: %s", output_path)
        if open_browser:
            webbrowser.open(f"file://{output_path}")
        return self

    def export_triage_report(
        self,
        output_filename: str = "pii_triage_report.html",
        open_browser: bool   = False,
    ) -> "QualityProfiler":
        """HTML table of triage scores for visual review."""
        if not self._triage_signals:
            logger.warning("No triage signals. Call compute_column_profiles() first.")
            return self

        from redibis.profiling.column_types import format_type_cell

        rows = ""
        for p in self._triage_signals:
            flag_bg  = "#d1fae5" if p.send_to_detector else "#f1f5f9"
            flag_txt = "SCAN"    if p.send_to_detector else "skip"
            flag_col = "#065f46" if p.send_to_detector else "#64748b"
            ar_badge = (
                f'<span style="background:#fef3c7;color:#92400e;'
                f'padding:2px 6px;border-radius:3px;font-size:11px;">'
                f"AR {p.arabic_fraction:.0%}</span>"
                if p.arabic_fraction >= 0.05 else ""
            )
            samples = " · ".join(
                f"<em>{s[:30]}</em>" for s in p.sample_values[:3]
            )
            rows += (
                f"<tr style='background:{flag_bg}'>"
                f"<td style='font-weight:600'>{p.column}</td>"
                f"{format_type_cell(p)}"
                f"<td style='color:{flag_col};font-weight:700'>{flag_txt}</td>"
                f"<td>{p.triage_score:.2f}</td>"
                f"<td>{p.cardinality_ratio:.3f}</td>"
                f"<td>{p.avg_value_length:.1f}</td>"
                f"<td>{p.null_rate:.1%}</td>"
                f"<td>{p.name_hint_score:.2f}</td>"
                f"<td>{ar_badge}</td>"
                f"<td style='font-size:11px;color:#64748b'>{samples}</td>"
                f"</tr>\n"
            )

        html = _TRIAGE_REPORT_TEMPLATE.replace("ROWS_PLACEHOLDER", rows)
        output_path = os.path.abspath(output_filename)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Triage report saved: %s", output_path)
        if open_browser:
            webbrowser.open(f"file://{output_path}")
        return self


# ─────────────────────────────────────────────────────────────────────────────
# HTML Templates
# ─────────────────────────────────────────────────────────────────────────────

_INTERACTIVE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Quality Rules Review</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  :root {
    --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --accent: #4f46e5;
    --red: #ef4444; --green: #22c55e; --amber: #f59e0b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', system-ui, -apple-system, sans-serif; background: var(--bg);
         color: var(--text); padding-bottom: 100px; }
  .mono { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; }
  .dropped { opacity: 0.35; }
  .dropped .rule-name { text-decoration: line-through; }
  .header { background: #0f172a; color: #fff; padding: 1.5rem 2rem; }
  .header h1 { font-size: 1.4rem; font-weight: 800; letter-spacing: -.02em; }
  .header p { color: #94a3b8; font-size: .85rem; margin-top: .35rem; }
  .stats-bar { display: flex; gap: 1px; background: var(--border); border-bottom: 1px solid var(--border); }
  .stat-cell { flex: 1; background: var(--surface); padding: .75rem 1rem; text-align: center; }
  .stat-cell .label { font-size: .65rem; text-transform: uppercase; letter-spacing: .08em;
                      color: var(--muted); font-weight: 700; }
  .stat-cell .value { font-size: 1.4rem; font-weight: 800; margin-top: .15rem; }
  .stat-cell .value.accent { color: var(--accent); }
  .stat-cell .value.red { color: var(--red); }
  .stat-cell .value.green { color: var(--green); }
  .category { border: 1px solid var(--border); border-radius: 10px; margin: 1rem 1.5rem;
              background: var(--surface); overflow: hidden; }
  .category-header { display: flex; align-items: center; justify-content: space-between;
                     padding: .85rem 1.25rem; cursor: pointer; user-select: none;
                     background: #f8fafc; border-bottom: 1px solid transparent;
                     transition: all .15s; }
  .category-header:hover { background: #f1f5f9; }
  .category-header.open { border-bottom-color: var(--border); }
  .category-header .left { display: flex; align-items: center; gap: .75rem; }
  .category-header .arrow { transition: transform .2s; font-size: .75rem; color: var(--muted); }
  .category-header.open .arrow { transform: rotate(90deg); }
  .cat-name { font-weight: 700; font-size: .9rem; }
  .cat-badge { font-size: .7rem; font-weight: 700; padding: 2px 8px; border-radius: 10px;
               background: #e0e7ff; color: #4338ca; }
  .cat-badge.table { background: #fef3c7; color: #92400e; }
  .cat-dropped { font-size: .7rem; color: var(--red); font-weight: 600; }
  .category-body { display: none; }
  .category-body.open { display: block; }
  .rule-row { display: flex; align-items: flex-start; gap: 1rem; padding: .75rem 1.25rem;
              border-bottom: 1px solid #f1f5f9; transition: all .15s; }
  .rule-row:last-child { border-bottom: none; }
  .rule-row:hover { background: #fafbfe; }
  .rule-id { font-size: .7rem; font-weight: 700; color: var(--muted); min-width: 2.5rem;
             padding-top: .15rem; }
  .rule-content { flex: 1; min-width: 0; }
  .rule-name { font-weight: 600; font-size: .82rem; word-break: break-all; }
  .rule-params { font-size: .72rem; color: var(--muted); margin-top: .2rem;
                 font-family: 'SF Mono', monospace; }
  .rule-actions { display: flex; gap: .4rem; flex-shrink: 0; }
  .btn { padding: .3rem .65rem; border-radius: 5px; font-size: .7rem; font-weight: 700;
         border: 1px solid; cursor: pointer; transition: all .15s; white-space: nowrap; }
  .btn-drop { background: #fef2f2; color: #dc2626; border-color: #fecaca; }
  .btn-drop:hover { background: #fee2e2; }
  .btn-drop.active { background: #e2e8f0; color: #64748b; border-color: #cbd5e1; }
  .btn-code { background: #f8fafc; color: #475569; border-color: #e2e8f0; }
  .btn-code:hover { background: #f1f5f9; }
  .code-panel { display: none; margin: .25rem 0 .5rem 3.5rem; padding: .65rem .85rem;
                background: #0f172a; color: #86efac; border-radius: 6px;
                font-family: 'SF Mono', monospace; font-size: .72rem;
                white-space: pre-wrap; word-break: break-all; }
  .code-panel.open { display: block; }
  .action-bar { display: flex; gap: .5rem; padding: .75rem 1.5rem; border-bottom: 1px solid var(--border);
                background: var(--surface); flex-wrap: wrap; align-items: center; }
  .action-bar .pill { padding: .35rem .85rem; border-radius: 6px; font-size: .75rem; font-weight: 600;
                      cursor: pointer; border: 1px solid var(--border); transition: all .15s;
                      background: var(--surface); color: var(--text); }
  .action-bar .pill:hover { background: #f1f5f9; }
  .action-bar .pill.active { background: #4f46e5; color: #fff; border-color: #4f46e5; }
  .footer { position: fixed; bottom: 0; left: 0; right: 0; background: var(--surface);
            border-top: 1px solid var(--border);
            box-shadow: 0 -4px 12px rgba(0,0,0,.06); padding: .75rem 1.5rem;
            display: flex; justify-content: space-between; align-items: center; z-index: 50; }
  .footer code { background: #f1f5f9; color: #be185d; padding: .4rem .75rem; border-radius: 6px;
                 font-size: .78rem; border: 1px solid var(--border); display: block;
                 max-width: 60vw; overflow-x: auto; white-space: nowrap; }
  .footer button { background: #4f46e5; color: #fff; font-weight: 700; font-size: .8rem;
                   padding: .55rem 1.5rem; border-radius: 6px; border: none; cursor: pointer;
                   transition: background .15s; }
  .footer button:hover { background: #4338ca; }
  .toast { position: fixed; top: 1.5rem; right: 1.5rem; background: #065f46; color: #fff;
           padding: .6rem 1.2rem; border-radius: 8px; font-size: .8rem; font-weight: 600;
           z-index: 100; opacity: 0; transition: opacity .25s; pointer-events: none; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>

<div class="header">
  <h1>Quality Rules Review</h1>
  <p>Click a category to expand rules. Drop noisy rules, then copy one complete Jupyter-ready program for what you kept.</p>
</div>
<div class="stats-bar" id="stats-bar"></div>
<div class="action-bar" id="action-bar"></div>
<div id="categories"></div>
<div class="footer">
  <div style="flex:1;min-width:0">
    <div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:#64748b;
                font-weight:700;margin-bottom:.25rem">Generated Code
      <span id="code-summary" style="text-transform:none;letter-spacing:0;font-weight:600;color:#4f46e5"></span>
    </div>
    <code id="script-output" style="display:block;max-height:3.5rem;overflow-y:auto">Review rules and click Generate Code.</code>
  </div>
  <button id="copy-btn" onclick="generateAndCopy()">Generate &amp; Copy Jupyter Code</button>
</div>
<div class="toast" id="toast">Copied to clipboard!</div>

<script>
const rules = DATA_PLACEHOLDER;

// ── Persistent state (survives re-renders) ───────────────────────────
const droppedIndices      = new Set();
const expandedCategories  = new Set();
const openCodePanels      = new Set();

// Exposed so the parent dashboard page (same-origin iframe embed) can read
// this review page's curated drop state before exporting a monitoring
// package — otherwise "Download monitor package" would silently ignore
// whatever the user dropped here. Top-level `const` isn't a window property.
window.getDroppedIndices = function() { return Array.from(droppedIndices); };

// The kept rules in structured form, so the embedding page can render one
// complete Jupyter program through the authoritative backend renderer rather
// than re-parsing this page's display text.
window.getKeptRules = function() {
  return rules.filter(r => !droppedIndices.has(r.id)).map(r => ({
    rule: r.rule,
    column: r.column === 'Table-Level' ? null : r.column,
    kwargs: r.params || {},
  }));
};

function getGroups() {
  const groups = {};
  rules.forEach(r => {
    const key = r.column;
    if (!groups[key]) groups[key] = [];
    groups[key].push(r);
  });
  const sorted = {};
  if (groups['Table-Level']) sorted['Table-Level'] = groups['Table-Level'];
  Object.keys(groups).sort().forEach(k => {
    if (k !== 'Table-Level') sorted[k] = groups[k];
  });
  return sorted;
}

// Expand all by default on first load
const initialGroups = getGroups();
Object.keys(initialGroups).forEach(k => expandedCategories.add(k));

function renderStats() {
  const groups = getGroups();
  const totalDropped = droppedIndices.size;
  const columnCount = Object.keys(groups).filter(k => k !== 'Table-Level').length;
  const tableRules = (groups['Table-Level'] || []).length;
  document.getElementById('stats-bar').innerHTML = `
    <div class="stat-cell"><div class="label">Total Rules</div><div class="value accent">${rules.length}</div></div>
    <div class="stat-cell"><div class="label">Kept</div><div class="value green">${rules.length - totalDropped}</div></div>
    <div class="stat-cell"><div class="label">Dropped</div><div class="value red">${totalDropped}</div></div>
    <div class="stat-cell"><div class="label">Columns</div><div class="value">${columnCount}</div></div>
    <div class="stat-cell"><div class="label">Table Rules</div><div class="value">${tableRules}</div></div>`;
}

function renderActionBar() {
  const groups = getGroups();
  const bar = document.getElementById('action-bar');
  bar.innerHTML = '<span style="font-size:.75rem;font-weight:600;color:#64748b;margin-right:.25rem">View:</span>';

  const addPill = (label, onClick, isActive) => {
    const p = document.createElement('span');
    p.className = 'pill' + (isActive ? ' active' : '');
    p.innerHTML = label;
    p.onclick = onClick;
    bar.appendChild(p);
  };

  // Collapse All
  addPill('Collapse All', () => { expandedCategories.clear(); render(); },
          expandedCategories.size === 0);

  // Expand All
  const allKeys = Object.keys(groups);
  addPill(`Expand All (${rules.length})`,
          () => { allKeys.forEach(k => expandedCategories.add(k)); render(); },
          expandedCategories.size === allKeys.length);

  // Per-category pills — clicking TOGGLES that category without clearing others
  allKeys.forEach(cat => {
    const count = groups[cat].length;
    const droppedCount = groups[cat].filter(r => droppedIndices.has(r.id)).length;
    let label = (cat === 'Table-Level' ? 'Table' : cat) + ` <span style="opacity:.6">(${count})</span>`;
    if (droppedCount > 0) label += ` <span style="color:#ef4444;font-size:.65rem">-${droppedCount}</span>`;
    addPill(label,
            () => {
              if (expandedCategories.has(cat)) expandedCategories.delete(cat);
              else expandedCategories.add(cat);
              render();
            },
            expandedCategories.has(cat));
  });
}

function renderCategories() {
  const groups = getGroups();
  const container = document.getElementById('categories');
  container.innerHTML = '';

  Object.entries(groups).forEach(([cat, catRules]) => {
    const isOpen = expandedCategories.has(cat);
    const droppedCount = catRules.filter(r => droppedIndices.has(r.id)).length;
    const keptCount = catRules.length - droppedCount;
    const isTable = cat === 'Table-Level';

    const section = document.createElement('div');
    section.className = 'category';

    const header = document.createElement('div');
    header.className = 'category-header' + (isOpen ? ' open' : '');
    header.innerHTML = `
      <div class="left">
        <span class="arrow">&#9654;</span>
        <span class="cat-name">${isTable ? 'Table-Level Rules' : cat}</span>
        <span class="cat-badge ${isTable ? 'table' : ''}">${catRules.length} rule${catRules.length !== 1 ? 's' : ''}</span>
        ${droppedCount > 0 ? '<span class="cat-dropped">' + droppedCount + ' dropped</span>' : ''}
      </div>
      <div style="font-size:.7rem;color:#64748b;font-weight:600">${keptCount} kept</div>`;
    header.onclick = () => {
      if (expandedCategories.has(cat)) expandedCategories.delete(cat);
      else expandedCategories.add(cat);
      render();
    };
    section.appendChild(header);

    const body = document.createElement('div');
    body.className = 'category-body' + (isOpen ? ' open' : '');

    catRules.forEach(rule => {
      const isDropped = droppedIndices.has(rule.id);
      const isCodeOpen = openCodePanels.has(rule.id);

      const row = document.createElement('div');
      row.className = 'rule-row' + (isDropped ? ' dropped' : '');
      row.innerHTML = `
        <div class="rule-id">[${rule.id}]</div>
        <div class="rule-content">
          <div class="rule-name">${rule.rule}</div>
          <div class="rule-params">${rule.kwargs}</div>
        </div>
        <div class="rule-actions">
          <button class="btn btn-drop ${isDropped ? 'active' : ''}"
                  onclick="event.stopPropagation();toggleDrop(${rule.id})">
            ${isDropped ? 'Undo' : 'Drop'}
          </button>
          <button class="btn btn-code"
                  onclick="event.stopPropagation();toggleCode(${rule.id})">
            Code
          </button>
        </div>`;
      body.appendChild(row);

      const codePanel = document.createElement('div');
      codePanel.className = 'code-panel' + (isCodeOpen ? ' open' : '');
      codePanel.id = 'code-' + rule.id;
      if (isCodeOpen) codePanel.textContent = rule.python_code;
      body.appendChild(codePanel);
    });

    section.appendChild(body);
    container.appendChild(section);
  });
}

// ── Actions (never clear state unnecessarily) ────────────────────────
function toggleDrop(id) {
  if (droppedIndices.has(id)) droppedIndices.delete(id);
  else droppedIndices.add(id);
  render();
}

function toggleCode(id) {
  if (openCodePanels.has(id)) openCodePanels.delete(id);
  else openCodePanels.add(id);
  render();
}

function generateFullCode() {
  const lines = [];
  const dropped = new Set(droppedIndices);
  const kept = rules.filter(r => !dropped.has(r.id));

  if (kept.length === 0 && dropped.size === 0) return '';

  lines.push('# ── Quality rules generated by redibis interactive review ──');
  lines.push('');

  // Group kept rules by column
  const groups = {};
  kept.forEach(r => {
    const key = r.column;
    if (!groups[key]) groups[key] = [];
    groups[key].push(r);
  });

  // Emit table-level rules first
  if (groups['Table-Level']) {
    lines.push('# Table-level rules');
    groups['Table-Level'].forEach(r => {
      lines.push(r.python_code);
      lines.push('');
    });
    delete groups['Table-Level'];
  }

  // Emit per-column rules
  Object.keys(groups).sort().forEach(col => {
    lines.push('# Column: ' + col);
    groups[col].forEach(r => {
      lines.push(r.python_code);
      lines.push('');
    });
  });

  // Emit drop metadata comment (not executable — curation recorded in package manifest)
  if (dropped.size > 0) {
    const sortedDropped = Array.from(dropped).sort((a, b) => a - b);
    lines.push('# Dropped rule indices (manifest only): ' + sortedDropped.join(', '));
    lines.push('');
  }

  return lines.join('\\n');
}

function updateScript() {
  const out = document.getElementById('script-output');
  const summary = document.getElementById('code-summary');
  const kept = rules.length - droppedIndices.size;

  if (droppedIndices.size === 0 && rules.length > 0) {
    out.textContent = 'All ' + rules.length + ' rules kept. Click Generate & Copy Jupyter Code to export.';
    summary.textContent = '(' + rules.length + ' rules)';
  } else if (droppedIndices.size > 0) {
    out.textContent = kept + ' rules kept, ' + droppedIndices.size + ' dropped. Click Generate & Copy Jupyter Code.';
    summary.textContent = '(' + kept + ' kept, ' + droppedIndices.size + ' dropped)';
  } else {
    out.textContent = 'Review rules and click Generate & Copy Jupyter Code.';
    summary.textContent = '';
  }
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px;opacity:0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand('copy');
    showToast('Copied to clipboard!');
  } catch (e) {
    // Last resort: show in a prompt
    prompt('Copy this code manually (Ctrl+A, Ctrl+C):', text);
  }
  document.body.removeChild(ta);
}

function doCopy(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => showToast('Copied to clipboard!'),
      () => fallbackCopy(text)
    );
  } else {
    fallbackCopy(text);
  }
}

// Prefer the embedding dashboard's backend renderer: it returns one complete,
// self-contained Jupyter program (imports + inline RULES + validate(df)) for
// exactly the rules kept here. When this page is opened as a standalone file
// there is no backend, so fall back to the rules-only fragment.
async function generateAndCopy() {
  const out = document.getElementById('script-output');
  let renderer = null;
  try {
    if (window.parent && window.parent !== window) {
      renderer = window.parent.redibisRenderJupyterCode;
    }
  } catch (e) { renderer = null; }

  if (typeof renderer === 'function') {
    const kept = window.getKeptRules();
    if (!kept.length) { showToast('No rules kept — nothing to export.'); return; }
    showToast('Generating Jupyter code…');
    try {
      const code = await renderer(kept, Array.from(droppedIndices));
      out.textContent = code;
      out.style.maxHeight = '8rem';
      return;
    } catch (e) {
      showToast('Backend codegen failed — copied rules-only fragment instead.');
    }
  }

  const code = generateFullCode();
  if (!code) {
    showToast('No rules to export.');
    return;
  }
  out.textContent = code;
  out.style.maxHeight = '8rem';
  doCopy(code);
}

function render() {
  renderStats();
  renderActionBar();
  renderCategories();
  updateScript();
}

render();
</script>
</body>
</html>"""

_TRIAGE_REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>PII Triage Report</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f8fafc; padding: 2rem; }
    h1 { color: #1e293b; font-size: 1.5rem; margin-bottom: .25rem; }
    p.sub { color: #64748b; font-size: .85rem; margin-bottom: 1.5rem; }
    table { border-collapse: collapse; width: 100%; background: #fff;
            border-radius: 8px; overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,.1); }
    th { background: #1e293b; color: #fff; padding: .6rem .8rem;
         text-align: left; font-size: .75rem; letter-spacing: .05em;
         text-transform: uppercase; white-space: nowrap; }
    td { padding: .5rem .8rem; font-size: .82rem;
         border-bottom: 1px solid #f1f5f9; }
    tr:last-child td { border-bottom: none; }
  </style>
</head>
<body>
  <h1>🔍 PII Column Triage Report</h1>
  <p class="sub">Columns scored by structural signals.
     <strong style="color:#065f46">✓ SCAN</strong> columns are forwarded to Presidio + GLiNER.
  </p>
  <table>
    <thead>
      <tr>
        <th>Column</th><th>Type</th><th>Decision</th><th>Score</th>
        <th>Cardinality</th><th>Avg Len</th><th>Null %</th>
        <th>Name Hint</th><th>Arabic</th><th>Sample Values</th>
      </tr>
    </thead>
    <tbody>
ROWS_PLACEHOLDER
    </tbody>
  </table>
</body>
</html>"""


# Backward-compat aliases (deprecated — will be removed in v2.0)
DataProfiler = QualityProfiler