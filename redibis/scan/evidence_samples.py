"""
redibis.scan.evidence_samples
===============================
Sample collection for the evidence bundle. Unlike ``evidence_bundle.py`` and
``profile_metrics.py``, this module is **impure** — it holds the DataFrame
directly, because that is the only place a random sample can be drawn from.

Determinism: ``seed_from_run_id(run_id)`` derives a stable seed from the run
id, so re-emitting the bundle for the same run reproduces byte-identical
sample values (bundle-to-bundle diffs then show real change, not sampling
noise).
"""

from __future__ import annotations

import hashlib
import random
from typing import Optional

import pandas as pd


def seed_from_run_id(run_id: str) -> int:
    """Stable seed derived from ``run_id`` — same run, same samples, always."""
    digest = hashlib.sha256((run_id or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _json_scalar(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    return v


def collect_samples(
    df: pd.DataFrame,
    *,
    n: int = 10,
    seed: int,
    mask_plan: Optional["MaskingPlan"] = None,  # noqa: F821 - typed lazily below
) -> dict[str, list]:
    """Random ``n`` NON-NULL values per column, applying ``mask_plan`` when given.

    Sampling is index-based: the same row indices are drawn regardless of
    ``mask_plan`` so a masked bundle still reflects value shape at the same
    positions a raw bundle would have sampled.
    """
    rng = random.Random(seed)

    transformed_df = None
    if mask_plan is not None:
        from redibis.masking.engine import MaskingEngine, RunKeys

        keys = RunKeys.mint(run_id=f"evidence_{seed:x}", seed=str(seed))
        transformed_df = MaskingEngine(mask_plan, keys).transform_dataframe(df)

    out: dict[str, list] = {}
    for col in df.columns:
        series = df[col]
        non_null_idx = series[series.notna()].index.tolist()
        if not non_null_idx:
            out[col] = []
            continue
        chosen_idx = (
            non_null_idx if len(non_null_idx) <= n else rng.sample(non_null_idx, n)
        )
        source_series = transformed_df[col] if transformed_df is not None else series
        out[col] = [_json_scalar(source_series.loc[i]) for i in chosen_idx]
    return out
