"""
redibis.profiling.relationships
==============================
The *cross-column* profiling dimensions that per-column profilers (GE / OpenMetadata /
fingerprint) do NOT compute — correlation, redundancy, duplicate rows, and constant columns.

This is the ONLY genuinely-new information YData Profiling would add, recreated here with
**zero new dependencies** (pandas + numpy, both already required). ``phik`` (mixed-type
correlation) is used only if it happens to be installed; otherwise it is skipped.

Per-column stats (null/unique/min/max/mean/std/histogram/frequent_values/type-coercion) are
already produced by ``om_metrics.compute_column_metrics`` — do not duplicate them here.

Performance (Spark backend)
-------------------------
See ``relationships_spark.column_relationships_spark`` — exact duplicate / near-constant
detection shuffles the cluster; gate with ``approx=True`` or ``sample_fraction`` on huge tables.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from redibis.profiling.base import ProfileResult

RELATIONSHIP_KEYS = (
    "row_count",
    "duplicate_rows",
    "constant_columns",
    "near_constant_columns",
    "correlations",
    "redundant_pairs",
)


def normalize_methods(methods: Any) -> tuple[str, ...]:
    """Coerce list / str / tuple params (e.g. from YAML) to a method tuple."""
    if methods is None:
        return ("pearson", "spearman")
    if isinstance(methods, str):
        return (methods,)
    return tuple(str(m) for m in methods)


def resolve_relationship_data(
    df: pd.DataFrame,
    *,
    engine: str = "auto",
    source: Any = None,
    table: str | None = None,
    spark: Any = None,
) -> tuple[Any, str]:
    """
    Pick the dataset for relationship profiling: Spark table (Tier B) or pandas sample.

    When ``engine`` is ``auto`` or ``spark``, a hive/spark ``source``, ``table``, and
    injected ``spark`` session yield cluster-side profiling via ``spark.table(table)``.
    """
    eng = (engine or "auto").lower()
    if eng == "pandas":
        return df, "pandas"

    src_engine = (getattr(source, "engine", None) or "").lower() if source is not None else ""
    tbl = (table or "").strip()
    if spark is not None and tbl and eng in ("auto", "spark") and src_engine in ("hive", "spark"):
        try:
            return spark.table(tbl), "spark"
        except Exception as exc:
            if eng == "spark":
                raise RuntimeError(
                    f"relationship_engine='spark' failed for table {tbl!r}: {exc}"
                ) from exc

    if eng == "spark":
        raise RuntimeError(
            "relationship_engine='spark' requires source.engine hive/spark, "
            "a table name, and an injected Spark session"
        )
    return df, "pandas"


def _is_spark_dataframe(data: Any) -> bool:
    try:
        from pyspark.sql import DataFrame as SparkDataFrame
    except ImportError:
        return False
    return isinstance(data, SparkDataFrame)


def profile_relationships(
    data: Any,
    *,
    engine: str = "auto",
    **opts: Any,
) -> dict[str, Any]:
    """
    Dispatch relationship profiling to pandas or Spark based on input type / ``engine``.

    ``engine="auto"``: pandas ``DataFrame`` → pandas backend; Spark ``DataFrame`` → Spark backend.
    """
    eng = (engine or "auto").lower()
    if eng == "auto":
        if isinstance(data, pd.DataFrame):
            eng = "pandas"
        elif _is_spark_dataframe(data):
            eng = "spark"
        else:
            raise TypeError(
                f"profile_relationships: cannot infer engine for {type(data)!r}; "
                "pass engine='pandas' or engine='spark'"
            )
    opts = dict(opts)
    if "methods" in opts:
        opts["methods"] = normalize_methods(opts["methods"])

    if eng == "pandas":
        if not isinstance(data, pd.DataFrame):
            raise TypeError("pandas engine requires a pandas DataFrame")
        return column_relationships(data, **opts)
    if eng == "spark":
        from redibis.profiling.relationships_spark import column_relationships_spark

        return column_relationships_spark(data, **opts)
    raise ValueError(f"unknown relationships engine {engine!r}; choices: auto, pandas, spark")


def column_relationships(
    df: pd.DataFrame,
    *,
    methods: tuple[str, ...] = ("pearson", "spearman"),
    redundancy_threshold: float = 0.95,
    near_constant_threshold: float = 0.99,
) -> dict[str, Any]:
    """Dataset-level relationship signal. Cheap, dependency-light, JSON-serialisable."""
    out: dict[str, Any] = {
        "row_count": int(len(df)),
        "duplicate_rows": 0,
        "constant_columns": [],
        "near_constant_columns": [],
        "correlations": {},
        "redundant_pairs": [],
    }
    if df is None or df.empty:
        return out

    # 1) duplicate rows (a quality signal GE/OM don't give directly)
    try:
        out["duplicate_rows"] = int(df.duplicated().sum())
    except Exception:
        pass

    # 2) constant / near-constant columns (degenerate columns)
    for col in df.columns:
        s = df[col]
        nun = int(s.nunique(dropna=True))
        if nun <= 1:
            out["constant_columns"].append(str(col))
            continue
        try:
            top_share = float(s.value_counts(normalize=True, dropna=True).iloc[0])
            if top_share >= near_constant_threshold:
                out["near_constant_columns"].append(str(col))
        except Exception:
            pass

    # 3) numeric correlations (the cross-column dimension)
    numeric = df.select_dtypes(include="number")
    if numeric.shape[1] >= 2:
        for method in methods:
            try:
                out["correlations"][method] = numeric.corr(method=method).round(4).to_dict()
            except Exception:
                pass
        # 4) redundant pairs from |pearson| (feature-overlap / leakage hint)
        try:
            corr = numeric.corr(method="pearson").abs()
            cols = list(corr.columns)
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    v = float(corr.iloc[i, j])
                    if v >= redundancy_threshold:
                        out["redundant_pairs"].append(
                            {"a": str(cols[i]), "b": str(cols[j]), "pearson": round(v, 4)}
                        )
        except Exception:
            pass

    # 5) OPTIONAL mixed-type association if phik is present (no hard dependency)
    try:
        import phik  # noqa: F401
        out["correlations"]["phik"] = df.phik_matrix().round(4).to_dict()
    except Exception:
        pass

    return out


def _redundant_with_map(rel: dict[str, Any]) -> dict[str, list[str]]:
    """Build column → peers with |corr| ≥ threshold from redundant_pairs."""
    peers: dict[str, set[str]] = {}
    for pair in rel.get("redundant_pairs") or []:
        a, b = str(pair.get("a", "")), str(pair.get("b", ""))
        if not a or not b:
            continue
        peers.setdefault(a, set()).add(b)
        peers.setdefault(b, set()).add(a)
    return {col: sorted(names) for col, names in peers.items()}


def apply_relationship_flags(result: "ProfileResult", rel: dict[str, Any]) -> "ProfileResult":
    """
    Attach dataset-level relationships and per-column flags to a ``ProfileResult``.

    Sets ``result.raw["relationships"]``, updates ``ColumnProfile`` flags, and enriches
    fingerprint stats when fingerprints are already present.
    """
    result.raw["relationships"] = rel
    constant = {str(c) for c in rel.get("constant_columns") or []}
    near_constant = {str(c) for c in rel.get("near_constant_columns") or []}
    redundant = _redundant_with_map(rel)

    for profile in result.column_profiles:
        col = profile.column
        profile.constant = col in constant
        profile.near_constant = col in near_constant and not profile.constant
        profile.redundant_with = list(redundant.get(col, []))
        if profile.constant:
            profile.triage_score = max(profile.triage_score, 0.85)

    for profile in result.triage_signals:
        col = profile.column
        profile.constant = col in constant
        profile.near_constant = col in near_constant and not profile.constant
        profile.redundant_with = list(redundant.get(col, []))

    if result.fingerprints:
        apply_relationship_flags_to_fingerprints(result.fingerprints, rel)
    return result


def apply_relationship_flags_to_fingerprints(fingerprints: list, rel: dict[str, Any]) -> None:
    """Patch fingerprint stats with relationship flags (stable, dataset-level)."""
    from redibis.memory.fingerprint import FingerprintField

    constant = {str(c) for c in rel.get("constant_columns") or []}
    near_constant = {str(c) for c in rel.get("near_constant_columns") or []}
    redundant = _redundant_with_map(rel)

    for fp in fingerprints:
        col = fp.column
        if col in constant:
            fp.stats["constant"] = FingerprintField(True, source="sample", generalizes="stable")
        elif col in near_constant:
            fp.stats["near_constant"] = FingerprintField(
                True, source="sample", generalizes="stable",
            )
        peers = redundant.get(col)
        if peers:
            fp.stats["redundant_with"] = FingerprintField(
                peers, source="sample", generalizes="stable",
            )
