"""
redibis.profiling.relationships_spark
=======================================
Spark backend for dataset-level relationship profiling (Tier B / pushdown).

Performance caveats
-----------------
* ``distinct().count()`` and per-column ``groupBy`` for near-constant detection are
  cluster-wide shuffles — fine for large-but-not-huge tables.
* For very large tables, pass ``approx=True`` (default) to skip exact duplicate counts
  and near-constant ``groupBy`` scans; use ``sample_fraction`` to profile a fraction
  of rows instead of the full table.
* Prefer ``methods=("pearson",)`` when you only need Pearson — it uses ``df.stat.corr``
  per pair (cheaper than assembling a full ML correlation matrix for Spearman).
"""

from __future__ import annotations

from typing import Any, Sequence

from redibis.profiling.relationships import normalize_methods

# Rows above which exact duplicate / near-constant shuffles are skipped unless opted in.
DEFAULT_EXACT_DUP_MAX_ROWS = 50_000_000


def _pearson_only(methods: tuple[str, ...]) -> bool:
    return len(methods) == 1 and methods[0] == "pearson"


def column_relationships_spark(
    sdf,
    *,
    methods: Sequence[str] = ("pearson",),
    redundancy_threshold: float = 0.95,
    near_constant_threshold: float = 0.99,
    approx: bool = True,
    sample_fraction: float | None = None,
    exact_dup_max_rows: int = DEFAULT_EXACT_DUP_MAX_ROWS,
) -> dict[str, Any]:
    """Dataset-level relationship signal computed in Spark. Same shape as ``column_relationships``."""
    from pyspark.sql import functions as F

    methods = normalize_methods(methods)
    work = sdf
    if sample_fraction is not None and 0 < sample_fraction < 1:
        work = sdf.sample(withReplacement=False, fraction=float(sample_fraction), seed=42)

    n = int(work.count())
    out: dict[str, Any] = {
        "row_count": n,
        "duplicate_rows": 0,
        "constant_columns": [],
        "near_constant_columns": [],
        "correlations": {},
        "redundant_pairs": [],
    }
    if n == 0:
        return out

    use_exact_dup = not approx or n <= exact_dup_max_rows
    if use_exact_dup:
        out["duplicate_rows"] = int(n - work.distinct().count())
    else:
        out["duplicate_rows"] = None
        out["perf_note"] = (
            f"duplicate_rows skipped (row_count={n} > exact_dup_max_rows={exact_dup_max_rows}); "
            "pass approx=False to force exact count"
        )

    distinct_counts = work.agg(*[
        F.approx_count_distinct(F.col(c)).alias(c) for c in work.columns
    ]).first().asDict()

    for c in work.columns:
        if int(distinct_counts.get(c) or 0) <= 1:
            out["constant_columns"].append(str(c))
            continue
        if not use_exact_dup:
            continue
        top = work.groupBy(c).count().orderBy(F.desc("count")).limit(1).first()
        if top and (top["count"] / n) >= near_constant_threshold:
            out["near_constant_columns"].append(str(c))

    numeric = [
        f.name for f in work.schema.fields
        if f.dataType.typeName() in (
            "integer", "long", "double", "float", "decimal", "short", "byte",
        )
    ]
    if len(numeric) < 2:
        return out

    if _pearson_only(methods):
        mat: dict[str, dict[str, float]] = {}
        for i, a in enumerate(numeric):
            for b in numeric[i:]:
                try:
                    cval = work.stat.corr(a, b, "pearson")
                except Exception:
                    cval = None
                if cval is None:
                    continue
                rounded = round(float(cval), 4)
                mat.setdefault(a, {})[b] = rounded
                mat.setdefault(b, {})[a] = rounded
                if a != b and abs(cval) >= redundancy_threshold:
                    out["redundant_pairs"].append(
                        {"a": str(a), "b": str(b), "pearson": rounded}
                    )
        out["correlations"]["pearson"] = mat
    else:
        from pyspark.ml.feature import VectorAssembler
        from pyspark.ml.stat import Correlation

        vec = VectorAssembler(
            inputCols=numeric, outputCol="_v", handleInvalid="skip",
        ).transform(work)
        for method in methods:
            if method not in ("pearson", "spearman"):
                continue
            corr_row = Correlation.corr(vec, "_v", method).head()
            if corr_row is None:
                continue
            m = corr_row[0].toArray().round(4)
            out["correlations"][method] = {
                numeric[i]: {
                    numeric[j]: float(m[i][j]) for j in range(len(numeric))
                }
                for i in range(len(numeric))
            }
            if method == "pearson":
                for i in range(len(numeric)):
                    for j in range(i + 1, len(numeric)):
                        v = abs(float(m[i][j]))
                        if v >= redundancy_threshold:
                            out["redundant_pairs"].append(
                                {
                                    "a": str(numeric[i]),
                                    "b": str(numeric[j]),
                                    "pearson": round(v, 4),
                                }
                            )

    return out
