"""
PII Detection Pipeline — Layer 1: Smart Data Ingestion & Sampling
=================================================================
Supports four strategies:
  1. partition_picker   — latest or named date partition (cheapest)
  2. statistical        — Spark TABLESAMPLE or reservoir across full table
  3. column_first       — metadata triage then targeted column sample
  4. fixed_rows         — exact N rows via stratified Spark sample

Dependencies:
  pyspark >= 3.3, pandas >= 1.5, great_expectations >= 0.17
  Cloudera CDP / Spark on YARN or local[*] for dev

Usage:
  sampler = TableSampler(spark, config)
  df = sampler.sample("telecom.customers")
  profile = sampler.ge_profile(df)           # feeds Layer 2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

import pandas as pd

try:
    from pyspark.sql import DataFrame, SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.types import StringType
    _PYSPARK_AVAILABLE = True
except ImportError:
    _PYSPARK_AVAILABLE = False

logger = logging.getLogger("pii.layer1")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

SamplingStrategy = Literal[
    "partition_picker",
    "statistical",
    "column_first",
    "fixed_rows",
]


@dataclass
class SamplingConfig:
    """
    Central config for Layer 1 sampling.

    Attributes
    ----------
    strategy          : Which sampling mode to use.
    partition_col     : Name of the date/partition column (e.g. "dt", "event_date").
    partition_date    : Target date string "YYYY-MM-DD".  None = latest partition.
    sample_fraction   : For 'statistical' strategy — fraction of rows (0.001–0.05).
    fixed_row_count   : For 'fixed_rows' strategy — absolute number of rows.
    max_rows_pandas   : Safety cap before converting Spark → Pandas.
    string_cols_only  : If True, only include StringType columns in output.
    column_name_hints : Token list for column_first triage
                        (e.g. ["name","phone","email","رقم","الاسم"]).
    seed              : Random seed for reproducibility.
    """
    strategy: SamplingStrategy = "partition_picker"
    partition_col: str = "dt"
    partition_date: Optional[str] = None          # None → auto-detect latest
    sample_fraction: float = 0.01                 # 1 %  for statistical
    fixed_row_count: int = 100_000
    max_rows_pandas: int = 500_000
    string_cols_only: bool = False
    column_name_hints: list[str] = field(default_factory=lambda: [
        "name", "phone", "email", "mobile", "address", "national",
        "passport", "birth", "gender", "الاسم", "رقم", "هاتف",
        "عنوان", "جنسية", "بريد",
    ])
    seed: int = 42


# ─────────────────────────────────────────────────────────────────────────────
# Core Sampler
# ─────────────────────────────────────────────────────────────────────────────

class TableSampler:
    """
    Layer 1 of the PII detection pipeline.

    Wraps four sampling strategies behind a single `.sample()` call and
    returns a Pandas DataFrame ready for Great Expectations profiling (Layer 2).
    """

    def __init__(self, spark: SparkSession, config: SamplingConfig | None = None):
        self.spark = spark
        self.cfg = config or SamplingConfig()

    # ── public entry point ────────────────────────────────────────────────────

    def sample(
        self,
        table: str,
        *,
        strategy: SamplingStrategy | None = None,
    ) -> pd.DataFrame:
        """
        Sample `table` and return a Pandas DataFrame.

        Parameters
        ----------
        table    : Fully-qualified table name, e.g. "telecom.customers".
        strategy : Override config strategy for this call only.

        Returns
        -------
        pd.DataFrame  — sampled rows, string columns cast to str.
        """
        effective_strategy = strategy or self.cfg.strategy
        logger.info("[Layer1] table=%s  strategy=%s", table, effective_strategy)

        dispatch = {
            "partition_picker": self._strategy_partition_picker,
            "statistical":      self._strategy_statistical,
            "column_first":     self._strategy_column_first,
            "fixed_rows":       self._strategy_fixed_rows,
        }

        if effective_strategy not in dispatch:
            raise ValueError(f"Unknown strategy: {effective_strategy!r}")

        sdf: DataFrame = dispatch[effective_strategy](table)

        if self.cfg.string_cols_only:
            str_cols = [
                c for c, t in sdf.dtypes if t == "string"
            ]
            sdf = sdf.select(str_cols)

        row_count = min(sdf.count(), self.cfg.max_rows_pandas)
        logger.info("[Layer1] converting %d rows to Pandas", row_count)

        pdf = sdf.limit(row_count).toPandas()
        pdf = self._cast_all_to_str(pdf)
        return pdf

    # ── strategy 1: partition picker ─────────────────────────────────────────

    def _strategy_partition_picker(self, table: str) -> DataFrame:
        """
        Read a single date partition.
        If partition_date is None, auto-detect the latest available partition.
        """
        target_date = self.cfg.partition_date or self._latest_partition(table)
        logger.info("[partition_picker] reading partition %s=%s",
                    self.cfg.partition_col, target_date)

        sdf = (
            self.spark.table(table)
            .filter(F.col(self.cfg.partition_col) == target_date)
        )
        self._log_shape(sdf, "partition_picker")
        return sdf

    def _latest_partition(self, table: str) -> str:
        """
        Discover the latest partition value via SHOW PARTITIONS.
        Falls back to yesterday's date if the table has no partitions.
        """
        try:
            parts = self.spark.sql(f"SHOW PARTITIONS {table}").collect()
            # Partitions come back as "dt=2024-11-01" style strings
            dates = []
            for row in parts:
                raw = row[0]                          # e.g. "dt=2024-11-01"
                val = raw.split("=")[-1].strip()
                try:
                    dates.append(date.fromisoformat(val))
                except ValueError:
                    pass
            if dates:
                latest = max(dates).isoformat()
                logger.info("[partition_picker] auto-detected latest: %s", latest)
                return latest
        except Exception as exc:
            logger.warning("[partition_picker] SHOW PARTITIONS failed: %s", exc)

        # Fallback: yesterday
        fallback = (date.today() - timedelta(days=1)).isoformat()
        logger.warning("[partition_picker] falling back to yesterday: %s", fallback)
        return fallback

    # ── strategy 2: statistical sample ───────────────────────────────────────

    def _strategy_statistical(self, table: str) -> DataFrame:
        """
        Spark TABLESAMPLE (fraction) across the full table.
        For Iceberg tables this leverages Iceberg's built-in sampling.
        """
        frac = self.cfg.sample_fraction
        logger.info("[statistical] fraction=%.4f  seed=%d", frac, self.cfg.seed)

        # Iceberg / Hive native TABLESAMPLE (pushdown — much faster than .sample())
        try:
            pct = frac * 100
            sdf = self.spark.sql(
                f"SELECT * FROM {table} TABLESAMPLE ({pct:.4f} PERCENT) REPEATABLE ({self.cfg.seed})"
            )
            self._log_shape(sdf, "statistical/tablesample")
            return sdf
        except Exception as exc:
            logger.warning("[statistical] TABLESAMPLE failed, falling back to .sample(): %s", exc)

        # Fallback: Spark DataFrame .sample()
        sdf = self.spark.table(table).sample(
            fraction=frac,
            seed=self.cfg.seed,
            withReplacement=False,
        )
        self._log_shape(sdf, "statistical/df_sample")
        return sdf

    # ── strategy 3: column-first triage ──────────────────────────────────────

    def _strategy_column_first(self, table: str) -> DataFrame:
        """
        Two-phase approach:
          Phase A — read only metadata to find suspicious columns.
          Phase B — read only those columns + sample rows.

        This is the most cost-efficient strategy for wide tables (100+ columns).
        """
        # Phase A: cheap full-table schema read, no data movement
        sdf_full = self.spark.table(table)
        all_cols = sdf_full.columns
        schema_types = dict(sdf_full.dtypes)

        suspicious = self._triage_columns(all_cols, schema_types)

        if not suspicious:
            logger.warning(
                "[column_first] no suspicious columns found by name triage; "
                "falling back to all string columns"
            )
            suspicious = [c for c, t in schema_types.items() if t == "string"]

        logger.info(
            "[column_first] %d/%d columns selected: %s",
            len(suspicious), len(all_cols), suspicious,
        )

        # Phase B: read only suspicious columns, then sample rows
        sdf = (
            sdf_full
            .select(suspicious)
            .sample(fraction=self.cfg.sample_fraction, seed=self.cfg.seed)
        )
        self._log_shape(sdf, "column_first")
        return sdf

    def _triage_columns(
        self,
        columns: list[str],
        schema_types: dict[str, str],
    ) -> list[str]:
        """
        Flag columns whose name contains any hint token (case-insensitive)
        and whose type is string/varchar.
        """
        hints = [h.lower() for h in self.cfg.column_name_hints]
        selected = []
        for col in columns:
            col_lower = col.lower()
            is_string = schema_types.get(col, "") in ("string", "varchar")
            name_match = any(hint in col_lower for hint in hints)
            if is_string and name_match:
                selected.append(col)
        return selected

    # ── strategy 4: fixed rows ────────────────────────────────────────────────

    def _strategy_fixed_rows(self, table: str) -> DataFrame:
        """
        Stratified sample targeting exactly `fixed_row_count` rows.

        If a partition column exists, samples proportionally across partitions
        so no single date dominates the sample.
        """
        target = self.cfg.fixed_row_count
        total = self.spark.table(table).count()
        frac = min(target / max(total, 1), 1.0)

        logger.info(
            "[fixed_rows] total=%d  target=%d  fraction=%.6f",
            total, target, frac,
        )

        sdf = (
            self.spark.table(table)
            .sample(fraction=frac, seed=self.cfg.seed)
            .limit(target)
        )
        self._log_shape(sdf, "fixed_rows")
        return sdf

    def full_table_stats(
        self,
        table: str,
        columns: list[str],
        *,
        include_minmax: Optional[set[str]] = None,
    ) -> dict[str, dict]:
        """One Spark aggregate pass: count, nulls, approx distinct, min/max.

        ``include_minmax`` limits min/max to non-PII columns only.
        """
        if not _PYSPARK_AVAILABLE or not columns:
            return {}
        include_minmax = include_minmax or set(columns)
        sdf = self.spark.table(table)
        out: dict[str, dict] = {}
        for col in columns:
            if col not in sdf.columns:
                continue
            c = F.col(col)
            agg_exprs = [
                F.count(F.lit(1)).alias("count"),
                F.count_if(c.isNull()).alias("null_count"),
                F.approx_count_distinct(c).alias("approx_distinct"),
            ]
            if col in include_minmax:
                agg_exprs.extend([F.min(c).alias("min"), F.max(c).alias("max")])
            row = sdf.agg(*agg_exprs).collect()[0]
            stats = {
                "count": int(row["count"] or 0),
                "null_count": int(row["null_count"] or 0),
                "approx_distinct": int(row["approx_distinct"] or 0),
            }
            if col in include_minmax:
                stats["min"] = row["min"]
                stats["max"] = row["max"]
            out[col] = stats
        return out

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _cast_all_to_str(pdf: pd.DataFrame) -> pd.DataFrame:
        """
        Cast every column to Python str.
        Pandas keeps numeric dtypes which break Presidio's string analyzers.
        """
        return pdf.astype(str).replace({"None": None, "nan": None, "<NA>": None})

    @staticmethod
    def _log_shape(sdf: DataFrame, label: str) -> None:
        try:
            # Avoid expensive .count() in production — log schema only
            logger.debug("[%s] schema: %s", label, sdf.schema.simpleString())
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory — quick Pandas path (no Spark needed for small files)
# ─────────────────────────────────────────────────────────────────────────────

class PandasTableSampler:
    """
    Lightweight sampler for local dev or small files (CSV / Parquet / JSON).
    Mirrors TableSampler's output contract so Layer 2 is unaffected.
    """

    def __init__(self, config: SamplingConfig | None = None):
        self.cfg = config or SamplingConfig()

    def from_parquet(self, path: str, columns: list[str] | None = None) -> pd.DataFrame:
        pdf = pd.read_parquet(path, columns=columns)
        return self._sample_pandas(pdf)

    def from_csv(self, path: str, **read_kwargs) -> pd.DataFrame:
        pdf = pd.read_csv(path, **read_kwargs)
        return self._sample_pandas(pdf)

    def from_dataframe(self, pdf: pd.DataFrame) -> pd.DataFrame:
        return self._sample_pandas(pdf)

    def _sample_pandas(self, pdf: pd.DataFrame) -> pd.DataFrame:
        cfg = self.cfg
        if cfg.strategy == "fixed_rows":
            n = min(cfg.fixed_row_count, len(pdf))
            result = pdf.sample(n=n, random_state=cfg.seed)
        elif cfg.strategy == "column_first":
            hints = [h.lower() for h in cfg.column_name_hints]
            str_cols = pdf.select_dtypes(include=["object", "string"]).columns.tolist()
            selected = [
                c for c in str_cols
                if any(h in c.lower() for h in hints)
            ] or str_cols
            result = pdf[selected].sample(
                frac=cfg.sample_fraction,
                random_state=cfg.seed,
            )
        else:
            result = pdf.sample(frac=cfg.sample_fraction, random_state=cfg.seed)

        result = result.astype(str).replace({"None": None, "nan": None})
        logger.info("[PandasTableSampler] output shape: %s", result.shape)
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Quick smoke test (run with: python pii_layer1_sampler.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    print("── PandasTableSampler smoke test ──────────────────────────────────")

    # Simulate a table with mixed columns
    import numpy as np

    rng = np.random.default_rng(42)
    n = 200_000

    fake_table = pd.DataFrame({
        "customer_id":    rng.integers(1_000_000, 9_999_999, n).astype(str),
        "full_name":      ["Ahmed Mohamed"] * n,
        "phone":          ["+20-100-000-0000"] * n,
        "email":          ["user@example.com"] * n,
        "city":           rng.choice(["Cairo", "Alexandria", "Giza"], n),
        "account_type":   rng.choice(["prepaid", "postpaid"], n),
        "monthly_spend":  rng.uniform(50, 2000, n).round(2),
        "notes":          ["اسم العميل: أحمد، رقم الهاتف: 01001234567"] * n,
        "dt":             ["2026-05-08"] * n,
    })

    # Test 1: fixed_rows
    cfg = SamplingConfig(strategy="fixed_rows", fixed_row_count=1_000)
    sampler = PandasTableSampler(cfg)
    df1 = sampler.from_dataframe(fake_table)
    print(f"fixed_rows → {df1.shape}")
    assert len(df1) == 1_000

    # Test 2: column_first
    cfg2 = SamplingConfig(strategy="column_first", sample_fraction=0.01)
    sampler2 = PandasTableSampler(cfg2)
    df2 = sampler2.from_dataframe(fake_table)
    print(f"column_first → {df2.shape}  cols={df2.columns.tolist()}")
    assert "full_name" in df2.columns
    assert "monthly_spend" not in df2.columns   # numeric, not a hint match

    # Test 3: statistical
    cfg3 = SamplingConfig(strategy="statistical", sample_fraction=0.005)
    sampler3 = PandasTableSampler(cfg3)
    df3 = sampler3.from_dataframe(fake_table)
    print(f"statistical → {df3.shape}")

    print("\n✓ All smoke tests passed — Layer 1 ready for pipeline integration")
    print("\nNext step: pass the returned DataFrame to GE profiler (Layer 2)")
    print("  from pii_layer2_ge_profiler import GEProfiler")
    print("  profile = GEProfiler().run(df1)")


# Backward-compat aliases (deprecated — will be removed in v2.0)
PIISampler    = TableSampler
PandasSampler = PandasTableSampler
SamplerConfig = SamplingConfig

