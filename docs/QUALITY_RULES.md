# Quality rules in redibis

_Last updated: 2026-09-27_

The reference for data-quality rules: what they are, where they come from, how
they get into a data contract, and every Python, CLI and HTTP entry point.
For a hands-on walkthrough, start with the tutorial:
**[tutorials/QUALITY_AUTHORING.md](tutorials/QUALITY_AUTHORING.md)**.

---

## 1. What a quality rule is

A quality rule is a check a table must pass: *no nulls in `txn_id`*, *`currency`
is one of EGP/USD/EUR*, *`amount` between 0 and 5 500*. redibis runs rules with
[Great Expectations](https://greatexpectations.io/) and stores them in the
table's **data contract** (ODCS v3), next to the column's type, PII findings and
business definition:

```yaml
schema:
  - name: transactions
    quality:                                   # table-level rules
      - rule: rowCount
        mustBeBetween: [900000, 1100000]
    properties:
      - name: currency
        logicalType: string
        required: true
        quality:                               # column-level rules
          - rule: missingCount                 # portable ODCS rule
            mustBe: 0
          - rule: validValues
            arguments: {validValues: [EGP, EUR, USD]}
          - engine: greatExpectations          # engine-specific rule
            implementation:
              expectation_type: expect_column_value_lengths_to_be_between
              kwargs: {column: currency, min_value: 3, max_value: 3}
```

Rules with a portable ODCS form are stored that way; everything else is stored
as a Great Expectations block. Both run the same way.

### Rule vocabulary

`redibis quality-run show` prints the canonical type; `--drop-rule` and
`draft.drop(rule_types=…)` accept any name in the row. The first six are stored
in portable ODCS form; every other Great Expectations rule is stored as an
`engine: greatExpectations` block and runs the same way.

| Canonical type | ODCS rule | Great Expectations expectation | Also accepted |
|---|---|---|---|
| `not_null` | `missingCount` | `expect_column_values_to_not_be_null` (`missingCount` 100 % = `expect_column_values_to_be_null`) | `missing`, `null`, `required` |
| `unique` | `duplicateCount` | `expect_column_values_to_be_unique` | `duplicates` |
| `set` | `validValues` | `expect_column_values_to_be_in_set` | `in_set`, `values`, `valid_values` |
| `regex` | `regex` (pattern in `arguments.pattern`) | `expect_column_values_to_match_regex` | `pattern`, `format` |
| `unique_count` | `uniqueCount` | `expect_column_unique_value_count_to_be_between` | — |
| `row_count` | `rowCount` | `expect_table_row_count_to_be_between` / `_to_equal` | `rows`, `rowcount` |
| `engine` | — | any other `expect_…` rule — see the catalogue below | the `expect_…` name |
| `sql` | `type: sql` + `query` + a threshold | custom SQL, run by redibis — see [Custom SQL rules](#custom-sql-rules) | — |

### Every Great Expectations rule

redibis runs Great Expectations **0.17.23** (the pinned version). All 53 core
expectations can be written as rules, are stored in the contract, and come
back unchanged. The table shows where each one runs; every ✓ was checked on
both engines through redibis. The check is `tests/test_ge_expectation_catalog.py`,
which fails if Great Expectations adds or drops an expectation.

Write any of them in a program's `RULES`, or paste them on the Quality page:

```python
{"rule": "expect_column_values_to_be_between", "column": "amount",
 "kwargs": {"min_value": 0, "max_value": 5500, "mostly": 0.99}}
{"rule": "expect_column_pair_values_a_to_be_greater_than_b", "column": None,
 "kwargs": {"column_A": "total", "column_B": "amount", "or_equal": True}}
```

Rules on two or more columns use `column: None` and name their columns in
`kwargs`; they are stored with the table-level rules. Every column rule also
takes `mostly` (the share of rows that must pass, e.g. `0.99`).

**Missing values and uniqueness**

| Expectation | Key arguments | pandas | Spark |
|---|---|:-:|:-:|
| `expect_column_values_to_not_be_null` | — | ✓ | ✓ |
| `expect_column_values_to_be_null` | — | ✓ | ✓ |
| `expect_column_values_to_be_unique` | — | ✓ | ✓ |
| `expect_compound_columns_to_be_unique` | `column_list` | ✓ | ✓ |
| `expect_select_column_values_to_be_unique_within_record` | `column_list` | ✓ | ✓ |

**Sets and categories**

| Expectation | Key arguments | pandas | Spark |
|---|---|:-:|:-:|
| `expect_column_values_to_be_in_set` | `value_set` | ✓ | ✓ |
| `expect_column_values_to_not_be_in_set` | `value_set` | ✓ | ✓ |
| `expect_column_distinct_values_to_be_in_set` | `value_set` | ✓ | ✓ |
| `expect_column_distinct_values_to_contain_set` | `value_set` | ✓ | ✓ |
| `expect_column_distinct_values_to_equal_set` | `value_set` | ✓ | ✓ |
| `expect_column_most_common_value_to_be_in_set` | `value_set` | ✓ | ✓ |
| `expect_column_pair_values_to_be_in_set` | `column_A`, `column_B`, `value_pairs_set` | ✓ | ✓ |

**Formats and text**

| Expectation | Key arguments | pandas | Spark |
|---|---|:-:|:-:|
| `expect_column_values_to_match_regex` | `regex` | ✓ | ✓ |
| `expect_column_values_to_not_match_regex` | `regex` | ✓ | ✓ |
| `expect_column_values_to_match_regex_list` | `regex_list`, `match_on` (`any`/`all`) | ✓ | ✓ ¹ |
| `expect_column_values_to_not_match_regex_list` | `regex_list` | ✓ | ✓ |
| `expect_column_values_to_match_strftime_format` | `strftime_format` | ✓ | ✓ ² |
| `expect_column_values_to_be_dateutil_parseable` | — | ✓ | ✗ ³ |
| `expect_column_values_to_be_json_parseable` | — | ✓ | ✓ ² |
| `expect_column_values_to_match_json_schema` | `json_schema` | ✓ ⁷ | ✓ ² ⁷ |
| `expect_column_value_lengths_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_value_lengths_to_equal` | `value` | ✓ | ✓ |
| `expect_column_values_to_match_like_pattern` | `like_pattern` | ✗ ⁴ | ✗ ⁴ |
| `expect_column_values_to_match_like_pattern_list` | `like_pattern_list` | ✗ ⁴ | ✗ ⁴ |
| `expect_column_values_to_not_match_like_pattern` | `like_pattern` | ✗ ⁴ | ✗ ⁴ |
| `expect_column_values_to_not_match_like_pattern_list` | `like_pattern_list` | ✗ ⁴ | ✗ ⁴ |

**Types**

| Expectation | Key arguments | pandas | Spark |
|---|---|:-:|:-:|
| `expect_column_values_to_be_of_type` | `type_` (pandas: `str`, `int64`…; Spark: `StringType`…) | ✓ | ✓ |
| `expect_column_values_to_be_in_type_list` | `type_list` | ✗ ⁵ | ✓ |

**Numbers and statistics** (most are *snapshot* rules when discovered; see §4)

| Expectation | Key arguments | pandas | Spark |
|---|---|:-:|:-:|
| `expect_column_values_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_values_to_be_increasing` | `strictly` | ✓ | ✓ |
| `expect_column_values_to_be_decreasing` | `strictly` | ✓ | ✓ |
| `expect_column_min_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_max_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_mean_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_median_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_stdev_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_sum_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_quantile_values_to_be_between` | `quantile_ranges` `{quantiles, value_ranges}` | ✓ | ✓ |
| `expect_column_value_z_scores_to_be_less_than` | `threshold`, `double_sided` | ✓ | ✓ |
| `expect_column_proportion_of_unique_values_to_be_between` | `min_value`, `max_value` (0–1) | ✓ | ✓ |
| `expect_column_unique_value_count_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_column_kl_divergence_to_be_less_than` | `partition_object` `{values, weights}` or `{bins, weights}`, `threshold` | ✓ | ✓ |
| `expect_column_pair_values_a_to_be_greater_than_b` | `column_A`, `column_B`, `or_equal` | ✓ | ✓ |
| `expect_column_pair_values_to_be_equal` | `column_A`, `column_B` | ✓ | ✓ |
| `expect_multicolumn_sum_to_equal` | `column_list`, `sum_total` | ✓ | ✓ |

**Table shape**

| Expectation | Key arguments | pandas | Spark |
|---|---|:-:|:-:|
| `expect_column_to_exist` | `column` | ✓ | ✓ |
| `expect_table_columns_to_match_set` | `column_set`, `exact_match` | ✓ | ✓ |
| `expect_table_columns_to_match_ordered_list` | `column_list` | ✓ | ✓ |
| `expect_table_column_count_to_equal` | `value` | ✓ | ✓ |
| `expect_table_column_count_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_table_row_count_to_equal` | `value` | ✓ | ✓ |
| `expect_table_row_count_to_be_between` | `min_value`, `max_value` | ✓ | ✓ |
| `expect_table_row_count_to_equal_other_table` | `other_table_name` | ✗ ⁶ | ✗ ⁶ |

1. On Spark, `match_on: "all"` reports every row as failing in Great Expectations
   0.17; use `match_on: "any"`, or one `match_regex` rule per pattern.
2. Runs as a Python function on the Spark workers: they need the same Python
   environment as the driver (Great Expectations installed; set `PYSPARK_PYTHON`).
3. No Spark implementation in Great Expectations 0.17; use
   `expect_column_values_to_match_strftime_format`.
4. `LIKE` patterns are implemented for SQL databases only, not for pandas or
   Spark DataFrames; use the `regex` variants, or a [SQL rule](#custom-sql-rules)
   with `LIKE`.
5. Fails on pandas text columns in Great Expectations 0.17; use
   `expect_column_values_to_be_of_type` with `type_: "str"`.
6. Needs a second table from a SQL data source; compare row counts with a
   [SQL rule](#custom-sql-rules) instead.
7. Raises on a value that is not JSON at all. The rule then reports *could not
   run*; the other rules are unaffected (see [What validation reports](#what-validation-reports)).
   Keep `expect_column_values_to_be_json_parseable` next to it for a clear message.

A rule that cannot run fails with the engine's message in the report, so an
unsupported rule never passes silently.

### Custom SQL rules

For checks that are easier to say in SQL — business rules, several columns,
conditional logic — add a SQL rule. redibis runs it on the same DataFrame as the
Great Expectations rules: on pandas with an in-process DuckDB (file and network
access switched off), on Spark in the DataFrame's own session.

- **Name the table** `${object}` (the ODCS placeholder; recommended), `data` (as on
  the Quality page), or its own name (`shop.orders`). `${property}` is the
  rule's column.
- **What is compared:**
  - A query whose final `SELECT` starts with `COUNT(`, `SUM(`, `AVG(`, `MIN(`
    or `MAX(` and returns one value: that value.
  - Any other query: the rows it returns are the rows that break the rule,
    and their count is compared.
- **The threshold** uses the ODCS keys: `mustBe`, `mustNotBe`, `mustBeGreaterThan`,
  `mustBeGreaterOrEqualTo`, `mustBeLessThan`, `mustBeLessOrEqualTo`,
  `mustBeBetween`, `mustNotBeBetween`. The default is `mustBeLessThan: 1`, meaning
  no bad rows; `max_failures=n` means `mustBeLessThan: n + 1`.
- **Only one `SELECT`** (or `WITH … SELECT`) statement is accepted. A query that
  fails to run fails its rule, with the database's message.

In a program's `RULES` a SQL rule is written with `sql_rule(...)`, or as the
equivalent dict:

```python
sql_rule("SELECT * FROM ${object} WHERE amount < 0", description="no negative amounts")
# same as
{"rule": "sql", "column": None,
 "kwargs": {"sql": "SELECT * FROM ${object} WHERE amount < 0", "mustBeLessThan": 1},
 "description": "no negative amounts"}
```

In the contract it is stored at table level (or on its column):

```yaml
quality:
  - type: sql
    query: SELECT * FROM ${object} WHERE amount < 0
    mustBeLessThan: 1
    description: no negative amounts
```

SQL rules run everywhere rules run: the Quality page, `validate(df)` and
`save_quality_run(df)` in the program, `draft.validate()`, `quality-monitor run` /
`batch` and the monitoring package. `quality-run show` / `diff` / `edit` / `merge`
list, change and merge them like any other rule.

`rules export --target sodacl` exports them. Soda compares the query's
**value**, so for rules you export, write `SELECT COUNT(*) FROM ${object} WHERE
<bad rows>`: it gives the same answer in redibis and in SodaCL. The Great
Expectations and dbt exports leave SQL rules out.

---

## 2. Where rules come from

| Source | How | Best for |
|---|---|---|
| **Quality scan** | Scan page with quality on, or `redibis quality FILE TABLE` | A first set of rules from a file |
| **Quality page** | Review, drop and add rules on a scan's results; **Copy Jupyter Code** | Curating by hand |
| **Code, quick** | `scan(df)`, or `QualityDraft.new()` plus `expect_…` calls (below) | A few rules on any DataFrame; editing discovered rules |
| **Code (pandas or Spark)** | `QualityAuthor(table).author(df)` | Accurate rules from a full partition |
| **Generated program in Jupyter** | Paste the Quality page's program, run `validate(df)`, edit `RULES`, `save_quality_run(df)` | Starting in the UI, finishing on big data |
| **Manual rule** | Contract → quality view → add a rule (`/quality-decisions/manual`) | One-off business checks |

Discovery uses Great Expectations' onboarding assistant over the **whole**
DataFrame (on Spark: in the cluster, nothing collected except a 5 000-row
sample for column triage). redibis adds three things on top:

- **Coverage** — columns the assistant skips (often the keys) get *not null*,
  *unique* and *length* rules computed in one aggregation.
- **Regex patterns are kept** — stored under `arguments.pattern` (a top-level
  `pattern` is dropped by the ODCS model).
- **Stable output** — value lists are sorted so the same set in another order is
  not a change.

### Rules in a few lines

Three ways to get rules into Python, all returning a `QualityDraft` you can edit:

```python
from redibis.quality import scan, QualityDraft

# 1. One small rule on any DataFrame — no table, no contract
QualityDraft.new().expect_column_values_to_not_be_null("customer_id").validate(df).to_frame()

# 2. Scan a DataFrame: rules discovered for you, then add or remove any of them
rules = scan(df, "shop.customers")        # drops one-sample rules, widens ranges ±10 %
rules.remove("regex")                                            # every rule of a kind
rules.remove("expect_column_value_lengths_to_be_between", column="website")  # both must match
rules.expect_column_values_to_be_between("loyalty_points", min_value=0, severity="P1")
rules.validate(next_partition).to_frame(only_failed=True)

# 3. Write them yourself — paste the lines the Quality page generates as they are
qa = QualityDraft.new("shop.customers")
qa.add_gx_expectation(expectation_name='expect_column_values_to_not_be_null', column='account_created_at')
qa.expect_column_values_to_be_unique("customer_id", severity="P1")   # the same, called by name; calls chain
qa.add_sql("SELECT * FROM ${object} WHERE loyalty_points < 0", description="no negative points")
```

- Any Great Expectations expectation works, by name, with its usual keyword
  arguments plus `severity=`. A misspelt name fails at once with a suggestion
  (`did you mean 'expect_column_values_to_be_unique'?`).
- `validate(df)` runs on pandas or Spark and writes nothing; `.to_frame()` gives
  one row per rule (expectation, column, severity, passed, observed, unexpected
  count, sample of failing values, message).
- A draft started with `QualityDraft.new()` names only the columns it checks, so
  extra columns in the DataFrame are not reported as schema drift. A scanned
  draft covers every column and does report drift.
- `print(rules.to_code())` writes any draft — scanned, edited or loaded from a run —
  as the short Python above: one `expect_…` call per rule, `add_sql` for SQL rules,
  severity when it is not `P1`. Paste it in a notebook or a pipeline, change it,
  run it, `validate(df)`. `to_code(style="gx")` writes the Quality page's
  `add_gx_expectation(expectation_name=…)` form instead.

  ```python
  print(scan(df, "shop.customers").to_code())
  ```

  ```text
  from redibis.quality import QualityDraft

  qa = QualityDraft.new('shop.customers')
  qa.expect_column_values_to_not_be_null('customer_id')
  qa.expect_column_values_to_be_unique('customer_id')
  qa.expect_column_value_lengths_to_be_between('customer_id', min_value=8, max_value=8)
  qa.expect_column_values_to_be_in_set('country', value_set=['AE', 'EG', 'SA'])
  …
  ```
- **Combine drafts** — scanned rules plus your curated ones — with `merge`. When
  both have a rule on the same column with the same check (same expectation, or
  same SQL query) the draft merged in wins; identical rules are not duplicated:

  ```python
  auto = scan(df, "shop.customers")
  curated = QualityDraft.new("shop.customers").expect_column_values_to_be_between(
      "loyalty_points", min_value=0, max_value=100_000, severity="P1")
  auto.merge(curated)                     # in place; curated's range replaces the scanned one
  rules = merge(auto, curated, extra)     # from redibis.quality import merge: a new draft
  auto.merge(curated, on_conflict="keep") # or "both"
  ```
- To keep the rules, save the draft as a quality run and merge it
  (`QualityAuthor(table).save(draft)`, §3).

---

## 3. The lifecycle: quality runs

No rule reaches a contract directly. Every source produces a **quality run** —
a proposed set of rules for one table — that you review and then merge.

```text
 scan / Quality page / code / notebook
                │
                ▼
        quality run  (draft) ──edit──► (reviewed) ──merge──► data contract (new version)
                │                                   └─discard─► (discarded)
                └── stored in the quality-contracts bucket, one file per run
```

- Runs saved from code, the CLI or a notebook appear in the dashboard's runs
  list, and runs from a scan appear in the CLI — it is one store.
- Every edit is kept in the run's history (who, when, why).
- A merge goes through the contract store's single writer: the contract gets a
  new version and the run is marked `merged`.

### What a merge changes

- Every **column that appears in the run** gets its rules **replaced** by the
  run's rules. Columns absent from the run keep theirs. Table-level rules are
  replaced when the run has table-level rules.
- `redibis quality-run diff` / `qa.diff()` shows exactly what will be added and
  removed before you merge.

### Personal-data columns

If the contract marks a column as personal data, the merge keeps rules that
embed real values (value lists, formats, lengths, ranges) **out of the
contract**, so it can be shared without leaking data. The diff lists these as
`~ … (not stored: personal-data column)`.

| Environment variable | Effect |
|---|---|
| _(default)_ | all rules on personal-data columns are withheld |
| `REDIBIS_PII_QUALITY_KEEP_SAFE=1` | keep value-free rules (*not null*, *unique*) |
| `REDIBIS_KEEP_QUALITY_ON_PII=1` | keep everything (disables the guard) |
| `REDIBIS_STRIP_PII_QUALITY_ALWAYS=1` | apply the guard on every write, including manual merges |

### Severity

Every rule carries a severity in the contract: `P1` (critical), `P2` (important),
`P3` (informational). A rule without one is `P1`. Monitors and pipeline gates read
it, typically blocking on `P1` and warning on the others. Set it in code with
`draft.set_severity(...)`, or pass `severity=` to `add_sql`:

```python
draft.set_severity("P2")                                          # every rule
draft.set_severity("P1", columns=["order_id"])                    # the key's rules
draft.set_severity("P1", columns=["email"], rule_types=["regex"]) # regex rules on email
draft.set_severity("P3", columns=[None])                          # table-level rules
```

The filters combine, unlike `drop()`, where any filter matches. `rule_types`
accepts the same names as `drop()`, including the Great Expectations name of a
rule stored in portable form (`expect_column_values_to_be_in_set` finds a
`validValues` rule).

### What validation reports

`draft.validate(df)`, `quality-monitor` and anything built on
`validate_contract_quality` return one result per rule. Each result carries:
- the rule's contract id and severity;
- the observed value;
- the number of unexpected rows;
- a sample of up to five failing values.

Two situations do not hide the other results:

- **A column the rule needs is missing.** The rule is reported as failed, with
  *column missing from the data: …*, and is not run. Schema drift (added and
  dropped columns) is reported for the table as well.
- **A rule raises inside the engine.** On Spark, Great Expectations computes
  metrics in bundles, so one failing rule used to fail its whole bundle, for
  example `match_json_schema` on a value that is not JSON. redibis re-runs the
  affected rules until the one that cannot run is isolated. That rule reports
  *could not run: …*; every other rule reports its real result.

---

## 4. Authoring rules that last

Rules discovered on one partition describe that partition. Three habits make
them hold on the next one:

1. **Drop snapshot rules.** Great Expectations writes some statistics as exact
   values (`mean == 2501.437562`, `row count == 1 000 000`). `SNAPSHOT_RULES` is
   the preset: row count, unique count, min, max, mean, median, stdev,
   quantiles, proportion of unique values.
2. **Relax ranges.** `relax(0.1)` / `--relax 0.1` widens numeric ranges by ±10 %
   of their span. A range never changes sign (`[0, 5000]` stays ≥ 0) and bounds
   are rounded outward, so two similar partitions produce identical rules.
   Value lengths, formats and value lists are not touched.
3. **Check on another partition.** `draft.validate(next_df)` runs the rules on
   other data and writes nothing.

Measured on two generated 1 000 000-row Spark partitions: as discovered, 19 of 30
rules held on the next day; after dropping snapshot rules, dropping the per-day
date column and relaxing ±10 %, 13 of 13 held.

**CSV input is read as text.** The scan, `quality-monitor run`, `redibis mask`
and the generated program's `load_sample` all read CSV columns as text, so
`01012345678` stays eleven characters. Read CSVs with `dtype=str` in your own
pandas code for the same reason.

---

## 5. The generated program (Quality page → Jupyter)

**Copy Jupyter Code** on the Quality pages (Review Quality Rules and Data quality)
opens the code with three choices:

| Choice | Options |
|---|---|
| Code | **Complete notebook** (default): imports, the uploaded file read with pandas as the scan read it, turned into a Spark DataFrame, one `qa.expect_…(…)` / `qa.add_sql(…)` line per rule, and `qa.validate(df, spark_mode="fused")`. **Rules only**: the rule lines and `qa.validate(df)`, for any DataFrame you already have. **Classic program**: the `RULES`-list program below |
| Engine | Spark (default) or pandas |
| Rules | All rules, or only the rules you approved on the page |

```python
import pandas as pd
from pyspark.sql import SparkSession
from redibis.quality import QualityDraft

DATA = '/…/scan_output/<session>/data.csv'          # the file uploaded to redibis
pdf = pd.read_csv(DATA)
spark = SparkSession.builder.appName("redibis-quality").getOrCreate()
df = spark.createDataFrame(pdf.astype(object).where(pdf.notna(), None))

qa = QualityDraft.new('shop.customers')
qa.expect_column_values_to_be_unique('email')
qa.expect_column_values_to_be_in_set('country', value_set=['AE', 'EG', 'SA'], severity='P2')
…
result = qa.validate(df, spark_mode="fused")        # aggregate rules in one Spark job
result.to_frame(only_failed=True)
```

Both styles paste back into the page's paste panel: the parser reads the
`qa.expect_…('column', …)`, `add_gx_expectation(…)` and `add_sql(…)` calls, severity
included, and never executes the text.

**Approving rules on the Quality pages.** Rows are light gray while they are
proposals. **+ approve** turns a row green; clicking **✓ approved** again unapproves
it; the batch buttons (all, passing only, not null…) approve several at once.
Nothing reaches the **Approved** page until **Send N to approval →** is confirmed;
from there the rules are reviewed and merged into the contract. A sent rule shows
**✓ sent · withdraw**, which takes it back out of the Approved basket.

The classic program, from **Classic program**, `redibis quality-run export -o x.py`,
the monitoring package and `draft.to_program()`, is one complete Python program:
every import, and every rule as a literal in an editable `RULES` list. Spark is the
default; a pandas flavour exists for small files. On Spark its `validate(df)` runs
with `spark_mode="fused"`, and `--partition dt=… --results-store URI` records the run
in the quality result store ([QUALITY_RESULTS.md](QUALITY_RESULTS.md)).

| In the program | Does |
|---|---|
| `RULES` | The rules — edit freely |
| `validate(df)` | Run `RULES` (Great Expectations and SQL) on a DataFrame; returns a JSON-ready report |
| `load_data(...)` / `load_sample(path)` | Read a table/partition (Spark) or a CSV/Parquet sample (pandas) |
| `discover_rules(df)` | Rules redibis finds on this DataFrame (a draft) |
| `add_rules(draft)` | Add them to `RULES`; skips a (type, column) already present — your rule wins (SQL rules: same query) |
| `sql_rule(query, …)` | A custom SQL rule for `RULES` (§1, Custom SQL rules) |
| `save_quality_run(df)` | Validate `RULES` on `df` and store them as a quality run; prints the next CLI steps |
| `OUTPUT_DIR` | The store to save into (same as the CLI's `--output-dir`) |
| `record_partition(df, "dt=…", store=…)` | Spark: validate `df` and store one row per rule in the quality result store |
| `main()` | Command-line entry: `spark-submit program.py --table … --partition-filter … [--partition dt=… --results-store URI] [--spark-mode fused\|persist\|ge]` |

Pasting the program into a notebook cell does not run `main()`. Importing a
program back (`quality-run import`, `edit --file`, `QualityDraft.from_program`)
**parses** it and reads only the literal `RULES`; the file is never executed.

---

## 6. Adding custom SQL to discovered rules

Discovery gives you the column rules; the business rules usually come from you.
Two ways to add SQL rules on top, both ending in a quality run you review and
merge. The examples use a `sales.transactions` table with `amount`, `currency`,
`status`, `refund_id` and `channel` columns.

### From code: rules redibis discovered

```python
from redibis.quality.authoring import QualityAuthor, SNAPSHOT_RULES

df = spark.table("sales.transactions").where("txn_date = '2026-09-25'")  # or a pandas DataFrame
qa = QualityAuthor("sales.transactions", output_dir="./reports")

draft = qa.author(df)                       # discovered on the whole partition
draft.drop(rule_types=SNAPSHOT_RULES)       # keep the rules that last

# your business rules, in SQL
draft.add_sql("SELECT * FROM ${object} WHERE amount < 0",
              description="no negative amounts")
draft.add_sql("SELECT COUNT(*) FROM ${object} WHERE status = 'refunded' AND refund_id IS NULL",
              description="every refund has a refund id")
draft.add_sql("SELECT COUNT(DISTINCT currency) FROM ${object}",
              mustBeBetween=[1, 3], description="at most three currencies a day")
draft.add_sql("SELECT * FROM ${object} WHERE channel = 'cash' AND amount > 10000",
              max_failures=5, description="large cash payments are rare")

report = draft.validate(df)                 # Great Expectations + SQL rules; writes nothing
for r in report.results:
    if r.expectation_type == "sql":
        print("pass" if r.success else "FAIL", r.observed_value, r.message)

run = qa.save(draft, note="discovered rules + business SQL")
print(qa.diff(run.run_id))
```

`add_sql` returns the rule as `draft.rules` lists it. Pass `column="amount"` to
keep a rule with its column instead of at table level. Then review and merge from
the CLI:

```bash
redibis quality-run show  sales.transactions --output-dir ./reports
#   [ 14] (table)   sql {'query': 'SELECT * FROM ${object} WHERE amount < 0', 'mustBeLessThan': 1}
#   [ 15] (table)   sql {'query': "SELECT COUNT(*) FROM ${object} WHERE status = 'refunded' AND …", …}
redibis quality-run diff  sales.transactions --output-dir ./reports
redibis quality-run merge sales.transactions --output-dir ./reports
```

### From the program copied from the Quality page

1. On the Quality page, review the discovered rules and press **Copy Jupyter
   Code**. SQL rules you added there (rule kind **SQL**) are already in the
   program.
2. Paste the program into a notebook cell and run it. It only defines things;
   `main()` does not run in a notebook.
3. In the next cell, load the data and append your SQL rules to `RULES`:

```python
df = load_data(partition_filter="txn_date = '2026-09-25'")   # Spark
# pandas flavour:  df = load_sample("transactions.csv", rows=200_000)

RULES.append(sql_rule("SELECT * FROM ${object} WHERE amount < 0",
                      description="no negative amounts"))
RULES.append(sql_rule(
    "SELECT COUNT(*) FROM ${object} WHERE status = 'refunded' AND refund_id IS NULL",
    description="every refund has a refund id"))

report = validate(df)                       # Great Expectations + SQL rules
[(r["kwargs"]["sql"], r["success"], r["observed_value"])
 for r in report["results"] if r["rule"] == "sql"]
```

   You can also type the rules straight into the `RULES` list, as `sql_rule(...)`
   calls or as `{"rule": "sql", …}` dicts. `add_rules(...)` tells SQL rules apart
   by their query, so two SQL rules never replace each other.

4. When the report looks right, store the rules as a quality run and merge:

```python
save_quality_run(df, note="Quality page rules + business SQL")
# prints: saved quality run …  /  review: redibis quality-run diff …  /  merge: …
```

To hand the program over as a file instead, save it and import it. Import reads
`sql_rule(...)` calls, `RULES.append(...)` lines and dict rules as text; the
program is never run:

```bash
redibis quality-run import sales.transactions program.py --output-dir ./reports
```

### Good to know

- The query sees **only this table**, the DataFrame being checked. Run checks
  that join other tables, such as orphaned keys, where both tables live (your
  warehouse or catalog), or load the joined data into the DataFrame first.
- Prefer `SELECT COUNT(*) FROM ${object} WHERE …` for rules you will export: it
  means the same in redibis and SodaCL (§1, Custom SQL rules).
- `relax()` and `--relax` never change SQL thresholds: they are business limits,
  not measured ranges.
- A merge replaces the table-level rules when the run has table-level rules, and
  SQL rules are table-level unless you gave a column. So keep every SQL rule you
  want in the run you merge.

---

## 7. After the merge

| Task | Command |
|---|---|
| Check new data against the contract (non-zero exit on failure → CI gate; reports schema drift) | `redibis quality-monitor run TABLE --sample FILE` |
| Validate several tables | `redibis quality-monitor batch …` |
| Standalone program + Airflow DAGs for a table | `redibis quality-monitor export TABLE -o DIR --engine spark` |
| Airflow DAGs only | `redibis quality-monitor airflow generate …` |
| List the contract's rules | `redibis rules list TABLE` |
| Export to Great Expectations / SodaCL / dbt | `redibis rules export TABLE --target ge\|sodacl\|dbt` (SodaCL and dbt need `pip install ".[contracts]"`) |

---

## 8. Reference

### Python — `redibis.quality.authoring`

| Call | Does |
|---|---|
| `QualityAuthor(table, output_dir="./reports")` · `QualityAuthor.from_s3(table, endpoint=…)` · `QualityAuthor(table, backend=…)` | Bind a table to a store (the CLI's `--output-dir`, S3/MinIO, or any backend) |
| `qa.author(df, count_rows=False)` | Discover + validate rules on a pandas or Spark DataFrame → `QualityDraft` |
| `qa.save(draft, note=…)` | Store as a quality run (status `draft`) |
| `qa.runs()` · `qa.load(run_id)` · `qa.update(run_id, draft)` | List · load · replace a stored run |
| `qa.diff(run_id)` · `qa.merge(run_id)` · `qa.active_rules()` | Preview · merge · rules in the contract today |
| `draft.summary()` · `draft.rules` | Numbered rules (same numbers as `quality-run show`) |
| `draft.drop(columns=…, rule_types=…, indices=…)` · `draft.relax(0.1)` · `draft.validate(df)` | Curate · widen · check on other data |
| `draft.to_yaml(path)` · `QualityDraft.from_yaml(path)` | File hand-off (`from_yaml` also reads ODCS partials and `.py` programs) |
| `draft.to_rules()` · `draft.to_program(engine)` · `QualityDraft.from_program(source)` | Draft ↔ `RULES` entries ↔ complete program |
| `author_quality(df, table)` · `draft_from_rules(table, RULES, df=None)` | Discovery alone · program `RULES` → draft |
| `scan(df, table="adhoc.dataframe", keep_snapshot_rules=False, relax=0.1)` · `QualityDraft.scan(...)` | Discover rules and curate them (no snapshot rules, ranges widened) → editable `QualityDraft` (`from redibis.quality import scan`) |
| `QualityDraft.new(table="adhoc.dataframe")` | An empty draft for rules written by hand |
| `draft.add_gx_expectation(expectation_name, column=None, severity=None, **kwargs)` · `draft.expect_…(column, …, severity=…)` | Add any Great Expectations rule — the line the Quality page generates, or the expectation called by name. Returns the draft (calls chain) |
| `draft.remove(expectation=None, column=…, index=None)` | Remove the rules matching **all** given filters (`column=None`: table-level); raises when nothing matches. Returns the draft |
| `draft.merge(*others, on_conflict="replace")` · `merge(*drafts)` | Combine drafts: in place, or into a new draft (`from redibis.quality import merge`). Same column + same check = conflict: `replace` (the draft merged in wins), `keep` or `both`; identical rules are not duplicated |
| `draft.to_code(var="qa", style="expect")` | The rules as short, runnable Python (`style="gx"`: the Quality page's `add_gx_expectation` lines) |
| `draft.validate(df, spark_mode="ge")` | Check the rules; on Spark `spark_mode="fused"` runs the aggregate rules in one job, `"persist"` caches the frame ([QUALITY_RESULTS.md §2a](QUALITY_RESULTS.md#2a-spark-one-job-for-many-rules)) |
| `result.to_frame(only_failed=False)` | A validation as a pandas DataFrame, one row per rule |
| `draft.add_rules([...])` | Add hand-written rules — any `expect_…` dict or `sql_rule(...)` — next to the discovered ones |
| `draft.add_sql(query, max_failures=0, column=None, description=None, severity=None, **thresholds)` | Add a custom SQL rule (§1, §6) |
| `draft.set_severity(severity, columns=…, rule_types=…, indices=…)` | Mark matching rules `P1`/`P2`/`P3` (filters combine) |
| `sql_rule(query, …)` · `run_sql_rules(df, rules, table=…)` | A `RULES` entry for SQL · run SQL rules on a DataFrame (`redibis.quality.sql_rules`) |
| `SNAPSHOT_RULES` · `relax_rules` · `diff_rules` · `drop_rules` · `list_rules` | Building blocks |

### CLI — `redibis quality-run` (alias `quality_run`)

All commands take `--output-dir DIR` (default `./reports`) or `--use-s3 [--s3-endpoint URL]`.

| Command | Does |
|---|---|
| `quality-run list TABLE` | Runs with status, rule count, pass count, engine and note |
| `quality-run show TABLE [--run R]` | Numbered rules (newest non-discarded run by default) |
| `quality-run diff TABLE [--run R]` | `+` / `−` / `~` against the active contract |
| `quality-run edit TABLE --run R [--drop-column C]… [--drop-rule T]… [--drop-index N]… [--relax F] [--file F] [--note N]` | Change a run before merging (kept in its history) |
| `quality-run export TABLE [--run R] -o FILE [--engine spark\|pandas]` | Editable YAML, or the complete program when `FILE` ends in `.py` |
| `quality-run import TABLE FILE [--run R] [--note N]` | Draft YAML, ODCS partial or `.py` program → new run |
| `quality-run merge TABLE [--run R] [--no-validate]` | Merge into the active contract |
| `quality-run discard TABLE --run R` | Drop a run |

Related: `redibis quality FILE TABLE [--automerge quality]` (quality scan),
`redibis rules …`, `redibis quality-monitor …`, and `redibis runs
list|merge|discard TABLE --kind pii|quality` (the generic run commands, for PII
runs too).

### HTTP API

Authentication applies (session cookie); interactive schemas at `/docs` after login.

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/sessions/{session_id}/step/quality` | Run the quality step of a scan session |
| `POST` | `/api/sessions/{session_id}/discovery/quality` | Discover rules for the session's data |
| `POST` | `/api/sessions/{session_id}/quality/evaluate` | Evaluate curated rules (Great Expectations and SQL) on the session's data |
| `POST` | `/api/sessions/{session_id}/quality/full-code` | The complete program (body: `engine` `spark`\|`pandas`, optional `rules`, `code`, `dropped_indices`) |
| `POST` | `/api/sessions/{session_id}/quality/export-package` | Monitoring package zip (program, rules, Airflow DAGs) |
| `POST` | `/api/quality/parse-rules` | Parse pasted rule code into structured rules (never executed) |
| `PUT` · `POST` · `DELETE` | `/api/sessions/{session_id}/config/quality`, `…/config/quality/rules[/{index}]` | Session quality configuration and rules |
| `GET` · `POST` · `DELETE` | `/api/configs/quality[/{name}]` | Saved quality configurations |
| `GET` | `/api/contracts/{table}/runs?kind=quality` | Runs for a table (includes runs saved from code/CLI) |
| `GET` · `PATCH` | `/api/runs/quality/{table}/{run_id}` | Read · edit a run |
| `POST` | `/api/runs/quality/{table}/{run_id}/merge` · `…/discard` | Merge · discard a run |
| `GET` | `/api/contracts/{table}/quality-view` | The contract's rules with decisions |
| `POST` | `/api/contracts/{table}/quality-decisions/{rule_id}/suppress` · `…/restore` · `…/suppress-all` · `…/manual` | Suppress · restore · add a manual rule |

---

## 9. Known limitations

- The onboarding assistant picks regex rules from a few built-in patterns; some
  are looser (or stranger) than the real format. Review `regex` rules.
- Discovery on Spark needs a Java runtime (Java 17+ for Spark 4).
- `rules export --target dbt` writes `sources: - name: null`; set the source name
  before using it with dbt.
- Rules already stored before 2026-09-26 may have `regex` entries without a
  pattern (the export dropped it); re-author or edit them.
- Some Great Expectations rules do not run on pandas or on Spark DataFrames in
  0.17 (`LIKE` patterns, `in_type_list` on pandas, `dateutil_parseable` on Spark,
  `row_count_to_equal_other_table`); see the footnotes in §1.
- Before 2026-09-26, `type: sql` rules in a contract were stored but not
  checked by `quality-monitor`; they are now. A rule whose query joins another
  table will fail with "table not found", because the query only sees the table
  being checked.
- Before 2026-09-26, reading an `expect_column_values_to_be_null` rule back from
  a contract turned it into *not null*; it now reads back as written.
- Before 2026-09-26, validation results reported every Great Expectations rule
  as `P1` with no sample of failing values, and a single missing column or
  failing Spark rule could fail every rule of a table. All three are fixed (§3,
  *What validation reports*).
- Discovered `regex` rules are generic patterns (sometimes an IP-address pattern
  on a timestamp). When several fit, the choice can differ between runs; review
  them and prefer explicit format rules.

## See also

- Validate each partition once and let consumers decide from stored results: [QUALITY_RESULTS.md](QUALITY_RESULTS.md)

- Tutorial: [tutorials/QUALITY_AUTHORING.md](tutorials/QUALITY_AUTHORING.md)
- Examples: [`examples/quality_authoring/`](../examples/quality_authoring/) — `author_pandas.py`, `author_spark.py`, `quality_authoring.ipynb`
