"""
redibis.profiling.vectorize
===========================
Hand-crafted deterministic feature vectors for column fingerprint similarity.
"""

from __future__ import annotations

import hashlib
import math

from redibis.profiling.fingerprint import ColumnFingerprint

FEATURE_DIM = 64
_MASK_SLOTS = 16  # bag-of-masks sub-vector size


def _clip_norm(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _hash_mask(mask: str, dim: int = _MASK_SLOTS) -> int:
    h = hashlib.md5(mask.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % dim


def fingerprint_to_vector(fp: ColumnFingerprint) -> list[float]:
    """Assemble a normalized fixed-length feature vector for similarity search."""
    vec: list[float] = []

    # Scalar features (0–23)
    vec.append(_clip_norm(fp.len_mean, 0, 200))
    vec.append(_clip_norm(fp.len_std, 0, 50))
    vec.append(_clip_norm(float(fp.len_p50), 0, 200))
    vec.append(_clip_norm(float(fp.len_p90), 0, 200))
    vec.append(_clip_norm(float(fp.len_min), 0, 200))
    vec.append(_clip_norm(float(fp.len_max), 0, 200))
    vec.append(fp.frac_digit)
    vec.append(fp.frac_alpha)
    vec.append(fp.frac_upper)
    vec.append(fp.frac_space)
    vec.append(fp.frac_punct)
    vec.append(fp.frac_arabic)
    vec.append(fp.distinct_ratio)
    vec.append(1.0 if fp.is_unique else 0.0)
    vec.append(1.0 if fp.is_constant else 0.0)
    vec.append(fp.null_rate)
    vec.append(1.0 if fp.has_nulls else 0.0)
    vec.append(fp.empty_rate)
    vec.append(_clip_norm(fp.token_count_mean, 0, 20))
    vec.append(_clip_norm(fp.len_entropy, 0, 8))
    vec.append(1.0 if fp.is_pii_reduced else 0.0)
    vec.append(1.0 if fp.stats_source == "full" else 0.0)
    vec.append(_clip_norm(float(fp.sample_size), 0, 10000))

    # Numeric magnitude is kept in the stored fingerprint for reranking but
    # excluded from the vector — ±1e9 clip_norm collapses real values to ~0.5
    # and adds noise without discriminating structurally similar ID columns.
    vec.extend([0.0, 0.0, 0.0, 0.0])

    # Bag-of-masks via feature hashing (28–43)
    mask_vec = [0.0] * _MASK_SLOTS
    for mask, frac in fp.pattern_masks:
        if mask == "other":
            mask_vec[-1] += frac
        else:
            idx = _hash_mask(mask)
            mask_vec[idx] += frac
    vec.extend(mask_vec)

    # Pad / truncate to FEATURE_DIM
    while len(vec) < FEATURE_DIM:
        vec.append(0.0)
    vec = vec[:FEATURE_DIM]

    # L2-normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec
