# CLI help — continuous quality monitoring

Validate **approved active-contract rules** on a schedule or after ETL events — without
re-running discovery or overwriting the contract.

The canonical command is **`redibis quality-monitor`**. It monitors *quality
expectations only* — not PII, not business definitions, not contract lifecycle —
so the name says so explicitly. `redibis monitor …` remains a backward-compatible
alias for one deprecation cycle: it accepts the same arguments, returns the same
exit codes and the same JSON on stdout, and prints one migration notice to
stderr.

Discovery (profile → interactive review → approve → merge) and monitoring are separate:

| Phase | Tool | Writes contract? |
|-------|------|------------------|
| Discovery | `redibis scan` / web Quality workflow | Yes (when you merge) |
| Monitoring | `redibis quality-monitor run` / `batch` | **No** — validate-only |

Deep dive: [QUALITY_SCAN_ALL_WAYS.md](../QUALITY_SCAN_ALL_WAYS.md) §6 ·
workflow tutorial: [QUALITY_WORKFLOW_TUTORIAL.md](../tutorials/QUALITY_WORKFLOW_TUTORIAL.md).

## Prerequisites

```bash
pip install -e ".[ge,dev]" -c requirements/constraints.txt
export REDIBIS_CONFIG=redibis.yaml   # source + storage + catalog blocks
```

An **active contract** with quality rules must exist (`redibis show schema.table`).

## Run one table

```bash
redibis quality-monitor run golden.merchant_seller_registry \
  --sample tests/fixtures/golden/realistic_merchant_seller_registry.csv \
  --config redibis.yaml \
  --json
```

Rules run are the **effective** rule set: the active contract's quality rules with
any `quality_decisions` overlay applied first (suppressed rules removed, approved
manual rules included) — see [Quality decision overlay](#quality-decision-overlay).

| Flag | Purpose |
|------|---------|
| `--sample PATH` | CSV/Parquet sample (required when `source.engine` is local/none). For `batch`, may contain `{table}` / `{table_safe}` placeholders expanded per table, e.g. `/data/{table_safe}.csv` |
| `--rows N` | Max rows to load (default 5000) |
| `--rules-file PATH` | Override contract rules with a `QualityRuleSet` YAML |
| `--no-schema-drift` | Skip column add/drop vs contract |
| `--no-ge-docs` | Skip GE Data Docs folder |
| `--no-publish` | Skip all configured result sinks (`quality.publish.sinks` / legacy `catalog.push.quality`) |
| `--use-s3` | Persist artifacts to MinIO (same flags as `scan`) |

Exit code: `0` on success/skipped, `1` when checks fail.

## Batch (all contracted tables)

`quality-monitor batch` and `quality-monitor airflow generate` require an **explicit table
selector** — `--tables`, `--tables-file`, `--database`, or `--all-contracts`.
There is no implicit "monitor everything"; omitting all four exits `2` with an
error.

```bash
redibis quality-monitor batch --all-contracts --config redibis.yaml --json
redibis quality-monitor batch --tables 'golden.*' --database golden --json
redibis quality-monitor batch --tables-file ./tables.txt --sample /data/samples/{table_safe}.csv
```

`--tables` entries containing `*`, `?`, or `[` are matched as glob patterns
against tables that currently have an active contract (`fnmatch`); plain
entries are used literally without a contract-existence check. `--database`
filters contracted tables by schema/database prefix.

Each table is isolated — one failure does not stop the batch.

## Export deployment package

Generates a versionable folder (identical layout to the zip served by the web
UI) with:

- `{table}_quality.py` — the **authoritative entry point**: a complete, executable
  Python program (imports, an inline `RULES = [...]` literal, `load_data()`,
  `validate(df)`, `main()`). Spark-first by default, so it runs standalone with
  `redibis[ge,spark]` installed — paste it into Jupyter or any IDE, or invoke it
  from any scheduler. Nothing in the package is read at runtime: the rules live
  in the file, so editing the program *is* editing the rules
- `quality/rule_set.json` — the canonical `redibis.io/quality/v1alpha1` rule set
  (see [Canonical schema](#canonical-schema-and-result-sinks))
- `quality/rules.yaml` — the same rules in the legacy editable GE shape
- `ge_paste.py` — rules-only AST-safe fragment; a compatibility artifact for the
  Quality UI paste panel (never executed). The full program above is what you
  deploy or open in a notebook
- `schemas/quality_ruleset.v1alpha1.schema.json`, `schemas/quality_run.v1alpha1.schema.json`
- `requirements.txt` — `redibis[ge,spark]`, `great_expectations`, `pyspark`
  (pinned to the installed versions when known)
- `dags/redibis_quality_<table>.py` — daily Airflow DAG, `spark-submit`ting the
  program above so the scheduled run validates the same full partition you would
- `dags/redibis_quality_<table>_event.py` — event-triggered DAG (`schedule=None`)
- `manifest.yaml` — `rule_source` (`active_contract` | `session_draft` |
  `session_run` | `pasted_code` | `python_file` | `none`), `engine`,
  `rule_set_id`, `rule_set_digest`, `source_digest` (for `--python-file`),
  dropped rule ids, runtime versions
- `CHECKSUMS.json` — real SHA-256 of every other file in the package

Without `--python-file`, CLI export uses the **effective active-contract rule
set** (decision overlay applied); it never reads a web session's in-progress
curation.

```bash
redibis quality-monitor export golden.merchant_seller_registry \
  -o ./packages \
  --schedule "0 6 * * *" \
  --config redibis.yaml
```

### Spark-first by default

A quality verdict is only meaningful over a real unit of data — typically a whole
business day of transactions. The generated program therefore:

- imports `SparkSession` and validates a **Spark DataFrame in place**; Great
  Expectations attaches to it directly through `QualityGatekeeper.attach_dataframe`
- never calls `toPandas()`, never applies the monitor sampler's 5,000-row cap, and
  never collects rows to the driver
- accepts a catalog table (`--table`, default `schema.table`) or a
  CSV/Parquet/Delta path (`--path` + `--format`)
- accepts an optional Spark predicate (`--partition-filter`, e.g.
  `"txn_date = '2026-08-30'"`) so you can validate one complete partition without
  editing the program

```bash
spark-submit golden_merchant_seller_registry_quality.py \
  --table golden.merchant_seller_registry \
  --partition-filter "txn_date = '2026-08-30'" \
  --out results.json
```

`--engine pandas` renders a small-file local-development variant instead
(`load_sample()` + `--rows`). Every UI action and every package defaults to Spark.

### Build a package from Python you edited

Copy the program out of the UI (or a package), extend it in Jupyter — add rules,
retune thresholds, delete noisy ones — then turn it back into a full package:

```bash
redibis quality-monitor export golden.merchant_seller_registry \
  --python-file ./edited_quality.py \
  -o ./packages
```

The file is parsed with the same AST/literal-only parser the UI uses
([`redibis/contracts/rule_code_parser.py`](../../redibis/contracts/rule_code_parser.py)):
Redibis reads the `RULES = [...]` literal (and any `add_gx_expectation(...)` /
`expect_*(...)` calls) and **never imports, compiles, or executes the file**. The
package records `rule_source: python_file` plus the file's SHA-256 as
`source_digest`.

Unreadable files, syntax errors, non-literal rule definitions, and files with
zero extractable rules are hard errors (exit `2`). There is deliberately no
fallback to the contract — silently shipping rules the operator did not write
would defeat the point of editing the file.

## Airflow DAG generation

```bash
redibis quality-monitor airflow generate \
  --tables golden.merchant_seller_registry telecom.customers \
  --all-contracts \
  -o ./dags \
  --schedule "0 6 * * *"
```

Copy `./dags/*.py` to `$AIRFLOW_HOME/dags/`. Set Airflow Variables:

- `REDIBIS_CONFIG` — path to `redibis.yaml`
- `REDIBIS_SAMPLE_ROOT` — directory of per-table CSV samples
- `S3_ENDPOINT_URL` — MinIO endpoint when using `--use-s3`

Daily DAGs run on cron; `*_event.py` DAGs are trigger-only (REST API after upstream ETL).

Standalone `quality-monitor airflow generate` DAGs call
`redibis quality-monitor run` against a bounded sample — a smoke check, and the
generated header says so. The DAGs *inside a deployment package* instead
`spark-submit` that package's program (with `REDIBIS_PACKAGE_DIR` and an optional
`REDIBIS_PARTITION_FILTER`), so the scheduled run validates the complete
partition.

Reference DAG module: `redibis/cli/airflow_dags.py` (`redibis_monitor_daily`).

## Artifacts (MinIO / local)

Written under workflow `monitor/` via `RunOutputWriter`:

```
monitor/{table_safe}/{run_id}/
  quality_results.json      # legacy per-rule results + schema drift (unchanged reader contract)
  quality_run.json          # canonical redibis.io/quality/v1alpha1 QualityRun envelope
  monitor_summary.json      # pass/fail rollup + completed_at (used to pick the "latest" run)
  quality-report-{run_id}.html
  ge_report/                # when GE docs enabled
  scan_coverage.json        # QUALITY_TEST facet for governance
```

`redibis catalog push` / replay selects the **latest** run per table by comparing
`completed_at` across each run's `monitor_summary.json` — not by run-id ordering.

## Quality decision overlay

Manual rule suppress/approve decisions recorded via `redibis contract quality-decision`
(or the web Quality UI) apply to `quality-monitor run` / `batch` / `export`
before validation: suppressed rules never run, and approved manual rules run
alongside the contract's own rules. This is the same overlay applied when serving
`redibis contract quality-view`; see [`redibis/store/quality_decisions.py`](../../redibis/store/quality_decisions.py)
(`effective_contract_quality`).

## Canonical schema and result sinks

Two authorities, on purpose:

- **ODCS v3** stays the contract / rule-*intent* authority — a rule's presence and
  its Great Expectations binding live on the contract's `quality` blocks.
- **`redibis.io/quality/v1alpha1`** (`redibis/quality/schema.py`) is Redibis' own
  versioned interchange for a *complete* rule set (`QualityRuleSet`, JSON Schema at
  [`schemas/quality/quality_ruleset.v1alpha1.schema.json`](../../schemas/quality/quality_ruleset.v1alpha1.schema.json))
  and a *complete* run result (`QualityRun`, JSON Schema at
  [`schemas/quality/quality_run.v1alpha1.schema.json`](../../schemas/quality/quality_run.v1alpha1.schema.json)).
  No single open standard covers both a cross-engine rule definition and its
  execution result, so this is Redibis-native rather than a repackaged external
  spec. `quality_run.json` is written alongside the legacy `quality_results.json`
  on every run; nothing that reads the legacy file needs to change.

Publishing a run to a dashboard is a pluggable **result sink** — a
`QualityResultSink` implementation resolved from `redibis/quality/sinks/registry.py`
(built-ins: `openmetadata`, `console`; more can be added as `redibis.quality_sinks`
entry points without touching the monitor service):

```yaml
quality:
  publish:
    sinks: [openmetadata]   # authoritative when set; try [console] to preview locally
```

Without `quality.publish.sinks`, the legacy toggle keeps working unchanged:

```yaml
catalog:
  push:
    quality: true   # default: true -> sinks: [openmetadata]
```

Each monitor run (unless `--no-publish`) dispatches the canonical `QualityRun` to
every configured sink. The OpenMetadata sink preserves existing behavior: stable
test identities, ledger ownership, and graceful degradation on transient failures.

Configure OM **Observability → Alerts** (Slack, email, generic webhook) on the table's
test suite — redibis does not embed a separate alert UI.

Tutorial: [CATALOG_OPENMETADATA_TUTORIAL.md](../tutorials/CATALOG_OPENMETADATA_TUTORIAL.md#data-quality-test-results).

## Web UI

On **Review Quality Rules** (and on the **Data quality** results page):

- **Generate & Copy Jupyter Code** / **Copy Jupyter Code** — one complete,
  self-contained `.py` program for exactly the rules you kept: imports, the
  inline `RULES = [...]` block, `build_rule_set()`, optional `load_data()`, and
  `validate(df)`. Paste it into Jupyter and call `validate(existing_dataframe)`;
  no Redibis-generated JSON or YAML is loaded at runtime
- **Download monitor package** — zip with the same layout as
  `quality-monitor export` (the same program, canonical rule set, DAGs, checksums)

Both actions go through the same renderer as the CLI, so the code you copy and
the program in the package are identical apart from the generated-at timestamp.

The rules-only `ge_paste.py` fragment still ships inside the package as a
compatibility artifact for the paste panel; it is no longer what the UI copies.

Web export uses an explicit, no-silent-fallback rule origin, resolved once in
[`redibis/services/quality_code.py`](../../redibis/services/quality_code.py):

1. pasted/uploaded Python (`pasted_code`), parsed only — never executed
2. structured curated rules supplied by the caller
3. the session's curated draft rule set, minus rows dropped on the review page
   (`dropped_indices`)
4. the session's last evaluated quality run (`session_run`)
5. the effective active contract (decision overlay applied) for the session's table

The exported package's `manifest.yaml` records which one was used as `rule_source`.

### Bringing edited Python back in

The paste panel accepts both a rules-only fragment and a full generated program,
and **📄 load .py file** loads an edited program straight from disk. Either way
the text is parsed with AST + literals only, yielding *proposed* rules; evaluate
them against the sample, approve, then merge. Import is strictly one-way:
uploaded code is never executed, and contract writes still require the existing
approval/merge flow.

API:

- `POST /api/sessions/{sid}/quality/full-code` — body
  `{"rules": [...] | "code": "...", "dropped_indices": [...], "engine": "spark"}`;
  returns `text/x-python` plus `X-Redibis-Rule-Source` / `X-Redibis-Rule-Count`
- `POST /api/sessions/{sid}/quality/export-package` — body
  `{"dropped_indices": [...], "rules": [...], "code": "...", "schedule": "..."}`
- `POST /api/quality/parse-rules` — stateless safe parse of a fragment or a full
  program

## Hive / JDBC sources

When `source.engine` is `hive`, `postgres`, `oracle`, or `jdbc`, omit `--sample` and
configure `source` in `redibis.yaml` (same spine as agent external sampling).

## Related commands

```bash
redibis rules list schema.table
redibis rules export schema.table --target ge
redibis contract quality-view schema.table
redibis catalog push schema.table    # metadata + quality test replay
```
