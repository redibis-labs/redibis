"""Author quality rules on a 1M-row Spark partition, check them on the next one,
merge, and see what a month-later refresh would change.

Needs pyspark and Java (pip install -e ".[ge,spark]"). Run from the repository root:

    python examples/quality_authoring/author_spark.py

On a real cluster replace ``partition(...)`` with e.g.
``spark.table("sales.transactions").where("txn_date = '2026-09-24'")``.
Tutorial: docs/tutorials/QUALITY_AUTHORING.md
"""

import logging
import time

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from redibis.quality.authoring import SNAPSHOT_RULES, QualityAuthor

logging.getLogger("great_expectations").setLevel(logging.ERROR)

spark = (SparkSession.builder.master("local[4]").appName("redibis-quality-authoring")
         .config("spark.ui.enabled", "false").getOrCreate())
spark.sparkContext.setLogLevel("ERROR")

TABLE = "sales.transactions"


def partition(day: str, seed: int, rows: int = 1_000_000, currencies=("EGP", "USD", "EUR")):
    """A synthetic daily partition of a transactions table."""
    pick = F.element_at(F.array(*[F.lit(c) for c in currencies]),
                        (F.col("id") % len(currencies) + 1).cast("int"))
    return (spark.range(rows)
            .withColumn("txn_id", F.format_string(f"TXN-{day.replace('-', '')}-%09d", F.col("id")))
            .withColumn("amount", (F.rand(seed) * 5000).cast("decimal(12,2)"))
            .withColumn("currency", pick)
            .withColumn("channel", F.when(F.col("id") % 10 == 0, None).otherwise(F.lit("app")))
            .withColumn("txn_date", F.lit(day).cast("date"))
            .drop("id"))


qa = QualityAuthor(TABLE, output_dir="./reports")

# 1. discover on one full partition
started = time.time()
draft = qa.author(partition("2026-09-24", seed=1))
print(f"{draft!r} in {time.time() - started:.0f}s")
print(draft.summary())

# 2. is it true tomorrow?
next_day = partition("2026-09-25", seed=2, rows=950_000)
before = draft.validate(next_day)
print(f"\nas discovered, on the next partition: {before.rules_passed}/{before.rules_total}")

# 3. curate: snapshot statistics out, the per-day date column out, ranges ±10 %
draft.drop(rule_types=SNAPSHOT_RULES)
draft.drop(columns=["txn_date"])
draft.relax(0.1)
after = draft.validate(next_day)
print(f"after curation: {after.status} {after.rules_passed}/{after.rules_total}")

# 4. save as a run, look at the diff, merge
run = qa.save(draft, note="authored on 2026-09-24, checked on 2026-09-25")
print("\n" + qa.diff(run.run_id))
print("merged: v" + qa.merge(run.run_id).upsert.version_after)

# 5. a month later: a new currency appears — the diff shows it before anything changes
october = qa.author(partition("2026-10-24", seed=3, currencies=("EGP", "USD", "EUR", "SAR")),
                    log=None)
october.drop(rule_types=SNAPSHOT_RULES)
october.drop(columns=["txn_date"])
october.relax(0.1)
refresh = qa.save(october, note="October refresh")
print("\nOctober refresh vs the contract:\n" + qa.diff(refresh.run_id))
print(f"\nreview it:  redibis quality-run show {TABLE} --output-dir ./reports")
print(f"merge it:   redibis quality-run merge {TABLE} --run {refresh.run_id} "
      "--output-dir ./reports")
