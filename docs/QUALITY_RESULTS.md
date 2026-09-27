# Quality results: validate once, let every consumer decide

_Last updated: 2026-09-27_

Two problems appear as soon as several applications read the same tables:

1. **The same checks run again and again.** Each consumer validates the partitions it
   reads, so one partition is scanned once per consumer, every day.
2. **Consumers need different things.** Finance needs the P1 money rules to hold on
   the last three days. The ML team needs today's partition, plus one check of its own.

redibis separates **measuring** from **deciding**:

```text
 producer job writes partition dt=2026-09-26
        │
        ▼
 validate_partition()  ── contract rules + every consumer's custom SQL, computed ONCE
        │                 (skipped if this data + these rules were already validated)
        ▼
 result store: partition_runs · rule_results · rule_sets
 (Iceberg by default · Parquet on S3/MinIO · Postgres)
        │
        ├─► finance policy:  last 3 days, P1 rules          → ACCEPT / BLOCK + partitions to read
        ├─► ml policy:       today, 4 rules + its own SQL  → ACCEPT / BLOCK
        └─► dashboards (Superset / Trino / OpenMetadata) read the same tables
```

A consumer never scans the data to decide. It reads a few rows of stored results.

---

## 1. Result stores

The store is chosen with a URI (argument, `--store`, or
`REDIBIS_QUALITY_RESULTS_STORE`). The default is `iceberg://default?namespace=dq`.

| Store | URI | Needs |
|---|---|---|
| **Apache Iceberg** (default) | `iceberg://<catalog>?namespace=dq` — catalog from `~/.pyiceberg.yaml` or `PYICEBERG_CATALOG__<NAME>__*`; or inline: `iceberg://prod?namespace=dq&type=rest&uri=http://catalog:8181&warehouse=s3://lake/` | `pip install "redibis[iceberg]"` |
| **Parquet on S3 / MinIO** | `s3://bucket/prefix?endpoint=http://minio:9000` (or `minio://…`; AWS without `endpoint`) | credentials `S3_ACCESS_KEY`/`S3_SECRET_KEY` or `AWS_*` |
| **Parquet on a path** | `file:///data/dq` | — |
| **Postgres** | `postgresql://user:pass@host:5432/db?schema=dq` | `pip install "redibis[postgres]"` |
| any SQLAlchemy database | `sqlite:///dq.db`, `mysql://…` | its driver |

Every store holds the same three tables:

| Table | One row per | Main columns |
|---|---|---|
| `partition_runs` | validation of one partition | `table_name`, `partition`, `partition_column`, `partition_value`, `partition_ts`, `partition_date`, `run_id`, `validated_at`, `data_fingerprint`, `rule_set_digest`, `contract_version`, `engine`, `row_count`, `rules_total`, `rules_passed`, `p1_failed`, `status` |
| `rule_results` | rule × run | the partition columns above, `run_id`, `validated_at`, `rule_id`, `rule_type`, `expectation`, `column_name`, `severity`, `owner`, `passed`, `unexpected_count`, `observed`, `sample`, `message` |
| `rule_sets` | rule × rule-set version | `rule_id`, `column_name`, `rule_type`, `expectation`, `severity`, `owner` (`contract` or `consumer:<name>`), `description`, `definition` |

Every result row says which partition it is about, in three forms:

| Column | Type | Example | Use |
|---|---|---|---|
| `partition` | string | `dt=2026-09-26/hour=05` | the name, as given |
| `partition_column` · `partition_value` | string | `dt/hour` · `2026-09-26/05` | filter by partition column and value |
| `partition_ts` | TIMESTAMP (UTC) | `2026-09-26 05:00:00` | `SELECT max(partition_ts)`: the newest partition. Set from the first date / datetime value (`2026-09-26`, `20260926`, `2026-09-26T13:00`) plus an `hour` column; `NULL` for partitions that are not time (e.g. `region=eg`) unless you pass `partition_ts=` |
| `partition_date` | DATE | `2026-09-26` | day windows |

`validated_at` (TIMESTAMP) orders the runs of one partition. Stores created by an
earlier version gain these columns automatically (old rows read as `NULL`).

They are ordinary tables, so Spark, Trino, Superset or Grafana query them directly:

```sql
SELECT partition, status, rules_passed, rules_total, p1_failed
FROM   lake.dq.partition_runs
WHERE  table_name = 'shop.orders' AND partition_date >= current_date - INTERVAL '7' DAY
```

The latest quality results of a table (newest partition, then its latest run):

```sql
SELECT r.expectation, r.column_name, r.severity, r.passed, r.unexpected_count, r.observed
FROM   lake.dq.rule_results r
WHERE  r.table_name = 'shop.orders'
AND    r.run_id = (SELECT run_id FROM lake.dq.partition_runs
                   WHERE  table_name = 'shop.orders'
                   ORDER  BY partition_ts DESC NULLS LAST, partition_date DESC NULLS LAST, validated_at DESC
                   LIMIT  1)
```

The same in Python, `run, results = store.latest("shop.orders")`, and from a terminal,
`redibis quality-results latest shop.orders`.

The Iceberg tables are partitioned by `table_name`. Postgres gets indexes on
`(table_name, partition_date)` and `run_id`. The Parquet layout is Hive-style
(`<root>/partition_runs/table_name=<t>/…`), and files are never rewritten.

**Adding a store** takes two methods: append rows and read rows with simple filters.
Everything else is inherited.

```python
from redibis.quality.results import QualityResultStore, register_store

@register_store("bigquery")
class BigQueryResultStore(QualityResultStore):
    def __init__(self, uri, **options): ...
    def _append(self, name, rows): ...                       # name: partition_runs | rule_results | rule_sets
    def _read(self, name, *, table_name, equals=None, isin=None, date_from=None, date_to=None): ...
```

---

## 2. Producer: validate a partition once

Run it in the job that writes the partition, right after the write, or before
publishing it with Iceberg's write-audit-publish.

**Choosing the partition is the caller's job.** Pass a DataFrame that holds exactly
that partition; redibis never filters it. It validates the frame and records the
partition you name: `partition="dt=2026-09-26"`, `"dt=2026-09-26/hour=05"`, or a
dict such as `{"dt": date(2026, 9, 26), "hour": 5}`.

```python
from redibis.quality.results import get_result_store, validate_partition, iceberg_snapshot_id

store = get_result_store()                                    # Iceberg by default
df = spark.table("lake.shop.orders").where("dt = '2026-09-26'")
run = validate_partition(
    df, "shop.orders", partition="dt=2026-09-26",
    contract=rules,                                           # published rules / contract dict, or a ContractStore
    store=store,
    fingerprint=iceberg_snapshot_id(spark, "lake.shop.orders"),
    policies="policies/",                                     # consumers' custom SQL rules are computed here too
)
print(run)   # shop.orders [dt=2026-09-26] FAILED — 35/52 rules passed, 12 P1 failed (validated; run …)
```

- **Once.** A run is keyed on `(table, partition, fingerprint, rule_set_digest)`. When
  that key has already been validated, `validate_partition` returns the stored run
  (`run.skipped` is `True`) and computes nothing.
- **New data or new rules trigger a new run.** Pass the data version as
  `fingerprint`: the Iceberg snapshot id, a load id or a file checksum. The rule set
  is the contract's rules plus every consumer's custom SQL for the table, and its
  digest changes when either changes. `force=True` re-validates anyway.
- **Everything is stored.** Each rule's result, observed value, unexpected count and a
  sample of failing values. Consumers never need to see the data.
- **Performance on Spark:** see [§2a](#2a-spark-one-job-for-many-rules).

From the command line (exit code 0 passed, 1 failed, 2 error):

```bash
redibis quality-results validate shop.orders --partition dt=2026-09-26 --input orders.parquet \
    --rules rules/shop.orders.quality.yaml --policies policies/ --fingerprint "$SNAPSHOT_ID" \
    --store "iceberg://prod?namespace=dq"
```

---

## 2a. Spark: one job for many rules

Great Expectations computes each rule's metrics separately: several Spark jobs per
rule. In the course, 52 rules on one partition ran 87 jobs, and each job reads the
source again unless the frame is persisted. `spark_mode` chooses how rules run:

| `spark_mode` | What runs | Use |
|---|---|---|
| `"ge"` (default) | Great Expectations, every rule | small frames, or to compare |
| `"persist"` | the same, on `df.persist()` (unpersisted afterwards if redibis cached it) | read the source once; same jobs |
| `"fused"` | every rule that is an aggregate compiled into **one** `df.agg(...)`: one pass over the data, one Spark action. The rest run with Great Expectations on the persisted frame | large partitions, many rules |

```python
run = validate_partition(df, "shop.orders", partition="dt=2026-09-26", contract=rules,
                         store=store, spark_mode="fused")
qa.validate(df, spark_mode="fused")            # the same for a draft
```

**How one query still gives one record per rule.** Each rule becomes named aggregate
expressions, for example:

```text
rule 3  not null (email)          → r3__bad = sum(email IS NULL)          r3__n = count(*)
rule 7  qty between 1 and 20      → r7__bad = sum(qty < 1 OR qty > 20)    r7__n = count(qty)
rule 9  mean(price) in [400, 500] → r9__v   = avg(price)
        SELECT count(*), r3__bad, r3__n, r7__bad, r7__n, r9__v, …  FROM partition   -- one job
```

Spark returns one row. The `r<i>__` prefix says which rule each value belongs to, so
the row is split back into one result per rule: passed, unexpected count, observed
value. These are the same fields Great Expectations reports, so `rule_results` gets
one row per table × partition × rule either way. When rules fail, a second small job
collects up to 5 failing values per failed rule (`sample`). The row count comes from
the same job, so `row_count` is filled at no cost.

**What is fused:** nulls, sets, ranges, lengths, regexes (Spark/Java regex syntax), uniqueness
(`mostly` = 1), compound keys, column pairs, multi-column sums, min / max / mean /
median / stdev / sum, distinct values and counts, row counts, and the
schema rules (columns, types). The last ones need no job at all. **What falls back to
Great Expectations:** ordering (increasing / decreasing), per-row Python parsing
(JSON, JSON schema, strftime, dateutil), quantiles (their value depends on the
interpolation method), most common value, z-scores and KL divergence.
`result.stats` tells how many ran each way, e.g.
`{"fused": 38, "ge": 3, "spark_jobs_fused": 1, "row_count": 20000}`. The run's
`engine` column records `spark-fused`.

On the course data, every rule gives the same verdict in both modes. One difference in
the numbers: for uniqueness, `unexpected_count` is the number of *extra* copies (Great
Expectations counts every row whose value repeats). Medians use Spark's exact
`percentile`, the same value as the pandas median. A Great Expectations suite also keeps one expectation per type and column, so a contract
with two rules of the same kind on one column (a discovered range and a business cap,
say) runs one of them in `ge` mode; `fused` runs and records both.

---

## 3. Consumer: choose rules and partitions, decide from stored results

Each consuming team owns a small policy file:

```yaml
consumer: finance_dashboard
table: shop.orders
window: {last_days: 2}          # partition >= today - 2 days (today, yesterday, the day before)
require: all                    # every partition in the window must pass
on_missing: block               # a partition not validated yet blocks (or: ignore)
rules:                          # choose from the table's rules …
  severities: [P1]              #   filters combine: P1 rules …
  columns: [items_value, total] #   … on these columns
  ids: [q_9f1c0e2ab44d7710]     # … plus exact rules (ids: `redibis quality-results rules shop.orders`)
custom_sql:                     # … plus checks of its own, computed ONCE by the producer
  - query: SELECT COUNT(*) FROM ${object} WHERE channel = 'web' AND payment_method = 'cash'
    description: no cash payments on the web channel
```

| Key | Options |
|---|---|
| `window` | `{today: true}` · `{last_days: N}` · `{since: 2026-09-01}` · `{partitions: [dt=2026-09-25, …]}` · `{latest: N}` |
| `require` | `all`: every partition in the window passes · `latest`: the most recent one passes · `any`: at least one passes |
| `on_missing` | `block` (default): an unvalidated partition in the window fails the decision · `ignore` |
| `rules` | `ids`, `columns` (`null` = table-level), `types` (e.g. `not_null`, `expect_column_values_to_be_between`), `severities`, `owners`, `all: true`. Filters combine with AND, then the explicit `ids` are added. With no selection, all rules are used. |
| `custom_sql` | [SQL rules](QUALITY_RULES.md#custom-sql-rules): `query` and a threshold (`mustBe`, `mustBeLessThan`, `max_failures`, …), `description` |

In the consumer's pipeline:

```python
from redibis.quality.results import ConsumerPolicy

decision = ConsumerPolicy.from_yaml("policies/finance_dashboard.yaml").evaluate()   # default store
print(decision)
if not decision.accepted:
    raise SystemExit(1)                                  # or alert and use yesterday's output
df = spark.table("lake.shop.orders").where(decision.partition_filter("dt"))   # only partitions that passed
```

```text
BLOCK finance_dashboard ← shop.orders (partition >= today - 2 days, require all, 9 rules): dt=2026-09-26 failed
  [dt=2026-09-24] passed  run 20260926T224042-dcf7ce2b
  [dt=2026-09-25] passed  run 20260926T224046-c8861fc4
  [dt=2026-09-26] failed  run 20260926T224049-d05b65f5
      ✗ P1 expect_column_min_to_be_between on items_value: observed -25.0
      ✗ P1 sql "no cash payments on the web channel" on (table): 20 unexpected — …
```

Each partition gets one verdict:
- **passed.**
- **failed:** the failing rules are listed.
- **missing:** the partition has not been validated yet.
- **incomplete:** the run predates a rule the consumer needs, such as newly added
  custom SQL. Re-validate the partition and it is computed.

From the command line (exit code 0 accepted, 1 blocked, 2 error):

```bash
redibis quality-results rules  shop.orders --store "$STORE"          # choose rules: ids, columns, types, owners
redibis quality-results rules  shop.orders --from-contract           # … straight from the active contract
redibis quality-results status shop.orders --days 7 --store "$STORE"
redibis quality-results latest shop.orders --store "$STORE"          # newest partition's results
redibis quality-results check  --policy policies/finance_dashboard.yaml --store "$STORE"   # [--as-of 2026-09-26] [--json]
```

In Python, `rules_catalog(store, table)` and `rules_catalog(table=…, contract=…)`
return the same list as a DataFrame.

---

## 4. Putting it together

1. Rules live in the data contract and are authored, reviewed and merged as quality
   runs ([QUALITY_RULES.md](QUALITY_RULES.md)).
2. Each consumer commits its policy file next to its pipeline, or in a shared
   `policies/` folder the producer reads.
3. The producer validates each partition once: `validate_partition` or
   `quality-results validate`, triggered by the write, not by a schedule.
4. Consumers run `evaluate()` / `quality-results check` before reading, and read only
   `decision.partition_filter(...)`.
5. Dashboards read the result tables directly. Catalogs such as OpenMetadata receive
   the same results through the existing sinks.

## Limitations

- The Parquet store reads a table's result files to answer a query. That is fine for
  daily partitions over months; for years of history use Iceberg or Postgres.
- A consumer rule added after a partition was validated makes that partition
  *incomplete* until it is re-validated. Nothing is back-filled automatically.
- PyIceberg's SQL catalog needs SQLAlchemy 2, and redibis pins 1.4 for Great
  Expectations. Use a REST, Hive or Glue catalog in production, or install the SQL
  catalog in a separate environment.
