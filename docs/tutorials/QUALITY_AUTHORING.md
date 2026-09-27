# Tutorial — author data-quality rules from code (Spark or pandas)

_Last updated: 2026-09-27_

redibis turns a DataFrame into **data-quality rules** — and keeps those rules in
the table's **data contract**, versioned and reviewed like everything else in it.

You point it at a DataFrame (usually one Spark partition with millions of rows),
it discovers rules with Great Expectations on the **whole** frame, checks them,
and hands you a draft. You curate the draft in the notebook, then either merge it
into the contract from code, or hand it to the `redibis` CLI for review, editing
and merging. Next month, run the same notebook on a new partition and redibis
shows exactly what would change in the contract before you accept it.

```text
 Quality page (UI)             notebook / spark-submit              CLI (or code)
 ┌──────────────────┐  .py   ┌───────────────────────────┐ run  ┌───────────────────────────────┐
 │ curate rules     ├───────►│ validate(df)  edit RULES   ├─────►│ redibis quality-run show/diff  │
 │ "full code"      │        │ discover_rules / add_rules │      │ redibis quality-run edit       │
 └──────────────────┘        │ — or — qa.author(df)       │ file │ redibis quality-run merge      │
                             │ save_quality_run(df)       ├─────►│   ──► data contract (versioned)│
                             └───────────────────────────┘      └───────────────────────────────┘
```

**Why author on big data?** Rules discovered on a 1 000-row sample describe the
sample. Rules discovered on a full partition — every null, every distinct value,
the real min and max — describe your data. The expensive part (profiling and
validation) runs inside Spark; nothing is collected to the driver except a
5 000-row sample used for lightweight column signals.

---

## 0. Install

```bash
pip install -e ".[ge,spark]" -c requirements/constraints.txt     # pandas only: ".[ge]"
```

Spark needs a Java runtime (Java 17+ for Spark 4). On a cluster, use the
cluster's Spark session and install redibis on the driver.

Everything below uses one storage location. Code and CLI must agree on it:

| You use | Code | CLI |
|---------|------|-----|
| Local folder | `QualityAuthor(table, output_dir="./reports")` | `--output-dir ./reports` |
| Shared S3 / MinIO (notebook on a cluster, CLI on your laptop) | `QualityAuthor.from_s3(table, endpoint="http://minio:9000")` | `--use-s3 --s3-endpoint http://minio:9000` |

S3 credentials come from the environment (`S3_ACCESS_KEY` / `AWS_ACCESS_KEY_ID`
and friends) on both sides. If the notebook cannot reach the store at all, use
the file hand-off in [section 6](#6-no-shared-store-hand-off-a-file).

---

## 1. Quick start (pandas)

The shortest path — scan, edit, check:

```python
import pandas as pd
from redibis.quality import scan, QualityDraft

df = pd.read_csv("tests/data/realistic_eshop_customer_account.csv", dtype=str)

rules = scan(df, "eshop.customer_account")      # discovered and curated for you
rules.remove("regex", column="email_address")    # remove what you do not want
rules.expect_column_values_to_not_be_null("account_created_at", severity="P1")   # add what is missing
rules.validate(df).to_frame(only_failed=True)
print(rules.to_code())                           # the rules as Python you can keep and edit

# one rule on any DataFrame, nothing else
QualityDraft.new().expect_column_values_to_be_unique("eshop_customer_id").validate(df).to_frame()
```

`scan` is the steps below in one call. The rest of this tutorial shows each step,
plus saving the rules as a quality run and merging them into the contract.

```python
import pandas as pd
from redibis.quality.authoring import QualityAuthor

df = pd.read_csv("tests/data/realistic_eshop_customer_account.csv", dtype=str)

qa = QualityAuthor("eshop.customer_account", output_dir="./reports")
draft = qa.author(df)
print(draft.summary())
```

```text
eshop.customer_account: 38 rules on 9 columns (pandas; validation 38/38 passed; 1000 rows)
  [  0] (table)                  row_count {'mustBeBetween': [1000, 1000]}
  [  1] (table)                  expect_table_columns_to_match_set {...}
  [  2] account_created_at       unique {'mustBe': 0}
  [  3] account_created_at       not_null {'mustBe': 0}
  ...
```

Read CSVs with `dtype=str` unless you want pandas to guess types: a guessed
integer drops leading zeros (`01012345678` → `1012345678`), and the discovered
length rules will describe the damaged values. redibis's own readers (scan,
`quality-monitor run`, `mask`, and the generated program's `load_sample`) read
CSVs as text for exactly this reason.

---

## 2. Spark: one partition, millions of rows

```python
from pyspark.sql import SparkSession
from redibis.quality.authoring import QualityAuthor

spark = SparkSession.builder.getOrCreate()           # your notebook's session

df = spark.table("sales.transactions").where("txn_date = '2026-09-24'")

qa = QualityAuthor("sales.transactions", output_dir="./reports")
draft = qa.author(df)                                 # discovery + validation in Spark
print(draft)
```

```text
discovering quality rules for sales.transactions (spark)…
validating the discovered rules…
added baseline rules for columns the profiler skipped: txn_id
30 rules; validation 28/30 passed
QualityDraft(table='sales.transactions', rules=30, passed=28/30, engine='spark')
```

Measured on a laptop-sized `local[4]` session: **1 000 000 rows, 5 columns,
≈ 40–45 s** end to end. On a cluster the time is dominated by how fast Spark scans
the partition.

What happens under the hood:

| Step | Where it runs |
|------|---------------|
| Rule discovery (Great Expectations onboarding assistant: nulls, uniqueness, value sets, ranges, lengths, formats, row count, column set) | Spark, whole partition |
| **Coverage**: columns the assistant skipped (often the keys) get *not null*, *unique* and *length* rules computed in one aggregation | Spark, whole partition |
| Validation of every discovered rule | Spark, whole partition |
| Column triage signals (Arabic text, PII-like names) | 5 000-row sample on the driver |

Types come from the Spark schema: `bigint` → integer, `decimal(12,2)` →
decimal, `map<…>`/`struct<…>` → object, `array<…>` → array.

---

## 3. Curate the draft in code

Great Expectations writes some rules as an **exact snapshot** of the profiled
data: `mean == 2501.437562`, `median == 2503.83`, `row count == 1000000`. They
are true today and false tomorrow. Two tools deal with that:

```python
from redibis.quality.authoring import SNAPSHOT_RULES

next_day = spark.table("sales.transactions").where("txn_date = '2026-09-25'")

print(draft.validate(next_day).rules_passed)          # as discovered: 19 / 30

draft.drop(rule_types=SNAPSHOT_RULES)                  # mean/median/stdev/quantiles/…/row count
draft.drop(columns=["txn_date"])                       # one value per partition by design
draft.relax(0.1)                                       # widen the remaining numeric ranges ±10 %

result = draft.validate(next_day)                      # after curation: 13 / 13
print(result.status, result.rules_passed, "/", result.rules_total)
```

`validate()` runs the draft's rules against any DataFrame and writes nothing —
use it on a *different* partition than the one you authored on. That is the
honest test of whether a rule describes your data or just one day of it.

Other ways to remove rules:

```python
draft.drop(indices=[0, 12])                            # numbers from draft.summary()
draft.drop(rule_types=["in_set"])                      # also: set, validValues,
                                                       #   expect_column_values_to_be_in_set
draft.drop(columns=["notes", "free_text"])
```

| Rule type (`--drop-rule` / `rule_types=`) | Meaning |
|---|---|
| `not_null` (`missing`, `required`) | no nulls |
| `unique` | no duplicates |
| `set` (`in_set`, `validValues`) | values from a fixed list |
| `regex` (`pattern`, `format`) | values match a pattern |
| `unique_count` | number of distinct values in a range |
| `row_count` (`rows`) | number of rows in a range |
| any `expect_…` name | the Great Expectations expectation of that name |

Review `regex` rules before accepting them: the assistant picks the tightest of
a few built-in patterns that matches every value, which is sometimes looser (or
stranger) than the format you have in mind.

**Add your own business rules in SQL.** They sit next to the discovered rules,
run on the same DataFrame, and are validated, saved and merged with them:

```python
draft.add_sql("SELECT * FROM ${object} WHERE amount < 0",
              description="no negative amounts")
draft.add_sql("SELECT COUNT(*) FROM ${object} WHERE status = 'refunded' AND refund_id IS NULL",
              description="every refund has a refund id")
print(draft.validate(next_day).status)
```

A query returns the rows that break the rule, or one number from `SELECT COUNT(*)`
(also `SUM`, `AVG`, `MIN`, `MAX`); it passes at zero unless you set a threshold
(`max_failures=5`, `mustBeBetween=[1, 3]`, …). Any Great Expectations
expectation can be added too. The full catalogue, the SQL rules and a worked
example for the Quality-page program are in
[QUALITY_RULES.md](../QUALITY_RULES.md#6-adding-custom-sql-to-discovered-rules).

---

## 4. Save and merge — all in code

```python
run = qa.save(draft, note="authored on 2026-09-24, checked on 2026-09-25")
print(qa.diff(run.run_id))          # what the merge would add / remove
qa.merge(run.run_id)                # → the active contract (versioned)
```

```text
13 added, 0 removed (across 5 columns)
```

`qa.save()` stores the draft as a **quality run** — the same run bucket the
dashboard and `redibis quality-run` use — with status `draft`. `qa.merge()` goes
through the contract store's single writer, like every other merge: the contract
gets a new version and the run is marked `merged`.

**Merge semantics:** every column that appears in the run gets its quality rules
**replaced** by the run's rules; columns absent from the run keep their current
rules. A run from a full partition therefore refreshes every column it saw.

**Personal-data columns:** if the contract marks a column as personal data (from
a PII scan or a steward decision), the merge keeps rules that embed real values
— value lists, formats, lengths, ranges — **out of the contract**, so the
contract can be shared without leaking the data. By default every rule on such a
column is withheld; set `REDIBIS_PII_QUALITY_KEEP_SAFE=1` to keep the value-free
ones (*not null*, *unique*). The diff lists these as `~ … (not stored:
personal-data column)` instead of promising them.

---

## 5. Or: review, edit and merge from the CLI

After `qa.save(...)` (shared store), everything else can happen in a terminal:

```bash
redibis quality-run list  sales.transactions --output-dir ./reports
redibis quality-run show  sales.transactions --output-dir ./reports          # numbered rules
redibis quality-run diff  sales.transactions --output-dir ./reports          # + / - vs the contract
```

```text
sales.transactions · run author_20260926_… · draft · 30 rules · validation 28/30 · spark
  [  0] (table)                  row_count {'mustBeBetween': [1000000, 1000000]}
  [  1] (table)                  expect_table_columns_to_match_set {...}
  [  2] amount                   not_null {'mustBe': 0}
  ...
```

Edit the run before merging — every edit is kept in the run's history:

```bash
redibis quality-run edit sales.transactions --run author_20260926_… \
  --drop-rule row_count --drop-rule expect_column_mean_to_be_between \
  --drop-column txn_date --drop-index 14 \
  --relax 0.1 --note "snapshot stats removed; ranges ±10%" \
  --output-dir ./reports

redibis quality-run diff  sales.transactions --output-dir ./reports
redibis quality-run merge sales.transactions --run author_20260926_… --output-dir ./reports
```

For bigger edits, export the run to YAML, edit it in any editor, and load it back:

```bash
redibis quality-run export sales.transactions --run author_20260926_… -o rules.yaml --output-dir ./reports
#   … edit rules.yaml …
redibis quality-run edit   sales.transactions --run author_20260926_… --file rules.yaml --output-dir ./reports
```

Changed your mind? `redibis quality-run discard sales.transactions --run …`.

---

## 6. No shared store? Hand off a file

When the notebook runs somewhere that cannot reach the contract store (a locked
down cluster, a customer environment), write the draft to a file and import it
where the CLI runs:

```python
draft.to_yaml("sales_transactions_rules.yaml")
```

```bash
redibis quality-run import sales.transactions sales_transactions_rules.yaml \
  --note "from cluster notebook" --output-dir ./reports
redibis quality-run diff  sales.transactions --output-dir ./reports
redibis quality-run merge sales.transactions --output-dir ./reports
```

`quality-run import` refuses a file whose table does not match. It also accepts a bare
ODCS quality section (for example one produced by `quality-run export`).

---

## 7. Start on the Quality page, finish in Jupyter

Rules you curate on the **Quality** page don't have to be merged from the UI.
**Copy Jupyter Code** gives you one complete Python program: every import, every
rule as a literal in an editable `RULES` list, and a Spark `validate(df)` (the
default; a pandas flavour exists for small files). Paste it into a notebook
cell — it runs as-is — then carry on with the full data:

```python
df = spark.table("sales.transactions").where("txn_date = '2026-09-24'")

report = validate(df)                           # the page's rules, on the whole partition
print(report["statistics"])

RULES.append({"rule": "expect_column_values_to_be_unique",
              "column": "txn_id", "kwargs": {"column": "txn_id"}})

more = discover_rules(df)                       # what redibis finds on this data
more.drop(rule_types=SNAPSHOT_RULES)
add_rules(more)                                 # fills gaps; your existing rules win

run_id = save_quality_run(df, note="page rules + notebook edits")
```

```text
UI rules on full partition: 3 / 3
added 7 rule(s); RULES now has 10
saved quality run author_20260926_…: 10/10 rules pass on this data
review: redibis quality-run diff sales.transactions --run author_20260926_… --output-dir ./reports
merge:  redibis quality-run merge sales.transactions --run author_20260926_… --output-dir ./reports
```

`save_quality_run` validates `RULES` on your DataFrame and stores them as a
quality run — nothing reaches the contract until you merge. Set `OUTPUT_DIR` at
the bottom of the program (or use `QualityAuthor.from_s3`) so it writes to the
same store as your CLI.

The same program runs outside a notebook too — `spark-submit sales_transactions_quality.py
--table … --partition-filter …` — and pasting it into Jupyter does **not** trigger that
command-line entry point.

**The other direction.** Any quality run — including one authored in code —
can be turned into that same complete program, and an edited program can come
back as a run. The program file is **parsed, never executed**, when imported:

```bash
redibis quality-run export sales.transactions -o sales_transactions_quality.py --output-dir ./reports
#   … open it in Jupyter, run validate(df), edit RULES, save the file …
redibis quality-run import sales.transactions sales_transactions_quality.py --output-dir ./reports
redibis quality-run diff   sales.transactions --output-dir ./reports
```

From code: `draft.to_program()` renders it, `QualityDraft.from_program(text)` reads it back.

**Custom SQL in the program.** Append SQL rules to `RULES` before you validate
and save. SQL rules added on the Quality page are already in the copied program:

```python
RULES.append(sql_rule("SELECT * FROM ${object} WHERE amount < 0",
                      description="no negative amounts"))
report = validate(df)                     # Great Expectations + SQL rules
save_quality_run(df)
```

---

## 8. Update an existing contract next month

The cycle is the same — old contract in, new rules proposed, you decide:

```python
qa = QualityAuthor("sales.transactions", output_dir="./reports")
print(len(qa.active_rules()), "rules in the contract today")

draft = qa.author(spark.table("sales.transactions").where("txn_date = '2026-10-24'"))
draft.drop(rule_types=SNAPSHOT_RULES); draft.drop(columns=["txn_date"]); draft.relax(0.1)
run = qa.save(draft, note="October refresh")
print(qa.diff(run.run_id))
```

```text
currency:
  + set {'arguments': {'validValues': ['EGP', 'EUR', 'SAR', 'USD']}}
  - set {'arguments': {'validValues': ['EGP', 'EUR', 'USD']}}
1 added, 1 removed (across 1 column)
```

A new currency appeared; the diff shows it before anything changes. Merge with
`qa.merge(run.run_id)` or `redibis quality-run merge …`, or leave the run unmerged and
investigate. To start from the contract's rules instead of a fresh discovery,
load them with `qa.load(run_id)` (any stored run) and save an edited copy with
`qa.update(run_id, draft)`.

---

## 9. After the merge: use the rules everywhere

The contract is now the single place that says what good data looks like:

```bash
# Check a new partition against the contract (non-zero exit on failure → CI gate)
redibis quality-monitor run sales.transactions --sample new_partition.parquet --output-dir ./reports

# Export the same rules to other tools
redibis rules export sales.transactions --target ge     --output-dir ./reports   # GE suite (JSON)
redibis rules export sales.transactions --target sodacl --output-dir ./reports   # needs .[contracts]
redibis rules export sales.transactions --target dbt    --output-dir ./reports   # needs .[contracts]

# A standalone Spark program (+ Airflow DAGs) that validates a partition in the cluster
redibis quality-monitor export sales.transactions -o ./monitoring --engine spark --output-dir ./reports
spark-submit monitoring/sales_transactions-monitoring/sales_transactions_quality.py \
  --table sales.transactions --partition-filter "txn_date = '2026-09-26'" --out results.json
```

The generated program can also be imported in a notebook:
`from sales_transactions_quality import validate; validate(spark_df)`.

---

## Reference

### Python — `redibis.quality.authoring`

| Call | Does |
|------|------|
| `QualityAuthor(table, output_dir=…)` / `QualityAuthor.from_s3(table, endpoint=…)` | Bind a table to the same store the CLI uses |
| `qa.author(df, count_rows=False)` | Discover + validate rules on a pandas or Spark DataFrame → `QualityDraft` |
| `draft.summary()` / `draft.rules` | Numbered rules (same numbers as `quality-run show`) |
| `draft.drop(columns=…, rule_types=…, indices=…)` | Remove rules |
| `draft.relax(0.1)` | Widen numeric ranges ±10 % |
| `draft.add_sql(query, max_failures=0, severity=None, …)` | Add a custom SQL rule |
| `scan(df, table=…)` (`from redibis.quality import scan`) | Discover + curate in one call → editable `QualityDraft` |
| `QualityDraft.new(table=…)` | Empty draft; `table` is optional for a quick check |
| `draft.add_gx_expectation(expectation_name=…, column=…, severity=…, **kwargs)` / `draft.expect_…(column, …)` | Add any expectation — paste the Quality page's line, or call it by name; chains |
| `draft.remove(expectation, column=…)` | Remove the rules matching all given filters |
| `draft.merge(other, on_conflict="replace")` / `merge(a, b)` | Combine drafts (e.g. scanned + curated); on the same column and check, the draft merged in wins |
| `draft.to_code()` / `draft.to_code(style="gx")` | The rules as short, runnable Python (one `expect_…` / `add_gx_expectation` call per rule) |
| `draft.add_rules([...])` | Add hand-written rules (any `expect_…` dict or `sql_rule(...)`) |
| `draft.set_severity("P2", columns=…, rule_types=…)` | Mark rules `P1`/`P2`/`P3`; monitors and pipeline gates read it |
| `draft.validate(other_df)` | Check the rules on other data; writes nothing (`.to_frame()` → one row per rule) |
| `draft.to_yaml(path)` / `QualityDraft.from_yaml(path)` | File hand-off |
| `qa.save(draft, note=…)` | Store as a quality run (status `draft`) |
| `qa.runs()` / `qa.load(run_id)` / `qa.update(run_id, draft)` | List, load, replace a stored run |
| `qa.diff(run_id)` | What a merge would change |
| `qa.merge(run_id)` | Merge into the active contract |
| `qa.active_rules()` | Rules in the contract today |
| `author_quality(df, table)` | The discovery step alone, no store |
| `draft_from_rules(table, RULES, df=df)` | Generated-program `RULES` → draft (validated on `df` when given) |
| `draft.to_rules()` / `draft.to_program(engine)` | Draft → `RULES` entries / the complete Jupyter program |
| `QualityDraft.from_program(source)` | Program → draft (AST parse, never executed) |

### CLI — `redibis quality-run …`

| Command | Does |
|---------|------|
| `quality-run list TABLE` | Runs and their status |
| `quality-run show TABLE [--run R]` | Numbered rules of a run (latest by default) |
| `quality-run diff TABLE [--run R]` | + / − against the active contract |
| `quality-run edit TABLE --run R [--drop-column C] [--drop-rule T] [--drop-index N] [--relax F] [--file F] [--note N]` | Edit before merging (recorded) |
| `quality-run export TABLE [--run R] -o FILE [--engine spark\|pandas]` | Run → editable YAML, or the complete program when `FILE` ends in `.py` |
| `quality-run import TABLE FILE [--run R] [--note N]` | Draft YAML, ODCS partial or `.py` program → new run |
| `quality-run merge TABLE [--run R]` | Merge into the active contract |
| `quality-run discard TABLE --run R` | Drop a run |

All commands take `--output-dir DIR` or `--use-s3 [--s3-endpoint URL]`.

### Runnable examples

- [`examples/quality_authoring/author_pandas.py`](../../examples/quality_authoring/author_pandas.py) — section 1 and 4 on the shipped sample CSV
- [`examples/quality_authoring/author_spark.py`](../../examples/quality_authoring/author_spark.py) — sections 2–4 on two generated 1M-row partitions (local Spark)
- [`examples/quality_authoring/quality_authoring.ipynb`](../../examples/quality_authoring/quality_authoring.ipynb) — the same, as a notebook
