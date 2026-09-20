"""Guarded profile store — GE/ydata stats + consent-gated samples.

Layout (contracts bucket)::

    _meta/profiles/{table}/{run_id}/manifest.json
    _meta/profiles/{table}/{run_id}/columns/{column}.json
    _meta/profiles/{table}/{run_id}/samples/{column}.json   # only when consent approved

Samples are never written as an empty placeholder. Read of samples is gated by
``SamplingConsentStore`` *and* ``role_can(role, "view_samples")``, and every
sample read is logged on the decision channel without values.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from redibis.store.storage_backend import StorageBackend

RESIDENCY_PORTABLE = "portable"
RESIDENCY_CONTAINS_VALUES = "contains_values"

# Cell-value statistics. On PII columns these can be the identifiers themselves
# (min/max of a national-id column, histogram bin labels, …). They follow the
# same role gate as samples so explorers never receive them.
_VALUE_BEARING_STAT_KEYS = (
    "min", "max", "mean", "median", "stdev", "std",
    "histogram", "quantiles", "frequent_values", "top_k",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact_value_bearing_stats(stats: dict) -> dict:
    """Drop min/max/histogram/etc. Keep rates, types, and masked top-k counts."""
    out = dict(stats or {})
    for key in _VALUE_BEARING_STAT_KEYS:
        out.pop(key, None)
    return out


def _mask_topk(values: list) -> list:
    """Keep counts, drop raw labels that look like cell values."""
    out = []
    for item in values or []:
        if isinstance(item, dict):
            entry = {k: v for k, v in item.items() if k in ("count", "freq", "share", "rank")}
            if "value" in item or "label" in item:
                entry["masked"] = True
            out.append(entry)
        else:
            out.append({"masked": True})
    return out


class ProfileStore:
    PREFIX = "_meta/profiles"

    def __init__(self, backend: StorageBackend, bucket: str, *, consent=None):
        self.backend = backend
        self.bucket = bucket
        self.consent = consent

    def _base(self, table: str, run_id: str) -> str:
        return f"{self.PREFIX}/{table}/{run_id}"

    def _manifest_key(self, table: str, run_id: str) -> str:
        return f"{self._base(table, run_id)}/manifest.json"

    def _column_key(self, table: str, run_id: str, column: str) -> str:
        return f"{self._base(table, run_id)}/columns/{column}.json"

    def _sample_key(self, table: str, run_id: str, column: str) -> str:
        return f"{self._base(table, run_id)}/samples/{column}.json"

    def write(
        self,
        table: str,
        run_id: str,
        profile_result: Any = None,
        quality_result: Any = None,
        samples: Optional[dict[str, list]] = None,
        consent: Any = None,
        *,
        engines: Optional[list[str]] = None,
        actor: str = "",
    ) -> dict:
        """Persist one run's profiles. Samples written only with consent."""
        consent_store = consent if consent is not None else self.consent
        samples = samples or {}
        column_stats = _stats_from_profile(profile_result)
        quality_by_col = _quality_by_column(quality_result)
        written_samples: list[str] = []
        column_digests: dict[str, str] = {}

        all_columns = sorted(set(column_stats) | set(quality_by_col) | set(samples))
        for column in all_columns:
            stats = dict(column_stats.get(column) or {})
            if "top_k" in stats:
                stats["top_k_masked"] = _mask_topk(stats.pop("top_k") or [])
            if "frequent_values" in stats:
                stats["top_k_masked"] = _mask_topk(stats.pop("frequent_values") or [])
            stats["quality"] = list(quality_by_col.get(column) or [])
            payload = stats
            column_digests[column] = _digest(payload)
            self.backend.put_json(self.bucket, self._column_key(table, run_id, column), payload)

            approved = False
            if consent_store is not None and hasattr(consent_store, "is_approved"):
                try:
                    approved = bool(consent_store.is_approved(table, column))
                except Exception:
                    approved = False
            raw = samples.get(column)
            if approved and raw:
                self.backend.put_json(
                    self.bucket,
                    self._sample_key(table, run_id, column),
                    {"column": column, "values": [str(v) for v in raw]},
                )
                written_samples.append(column)

        residency = RESIDENCY_CONTAINS_VALUES if written_samples else RESIDENCY_PORTABLE
        consent_snapshot = {}
        if consent_store is not None and hasattr(consent_store, "list"):
            try:
                consent_snapshot = {e.get("column"): bool(e.get("approved")) for e in consent_store.list(table)}
            except Exception:
                consent_snapshot = {}
        manifest = {
            "table": table,
            "run_id": run_id,
            "engines": list(engines or []),
            "residency": residency,
            "consent_snapshot": consent_snapshot,
            "columns_written": list(all_columns),
            "samples_written": written_samples,
            "digests": column_digests,
            "written_at": _utc_now_iso(),
            "written_by": actor,
        }
        self.backend.put_json(self.bucket, self._manifest_key(table, run_id), manifest)
        return manifest

    def latest_run_id(self, table: str) -> Optional[str]:
        prefix = f"{self.PREFIX}/{table}/"
        keys = self.backend.list_keys(self.bucket, prefix=prefix)
        run_ids: set[str] = set()
        for key in keys:
            rest = key[len(prefix):]
            run_id = rest.split("/", 1)[0]
            if run_id:
                run_ids.add(run_id)
        if not run_ids:
            return None
        # Prefer the run whose manifest has the latest written_at.
        newest = None
        newest_ts = ""
        for run_id in run_ids:
            man = self.manifest(table, run_id)
            ts = str((man or {}).get("written_at") or "")
            if newest is None or ts >= newest_ts:
                newest = run_id
                newest_ts = ts
        return newest

    def manifest(self, table: str, run_id: str) -> Optional[dict]:
        key = self._manifest_key(table, run_id)
        if not self.backend.exists(self.bucket, key):
            return None
        return self.backend.get_json(self.bucket, key)

    def read(
        self,
        table: str,
        run_id: str,
        column: str,
        *,
        actor: str = "",
        include_samples: bool = False,
        allow_samples: bool = True,
    ) -> dict:
        """Gated read. Samples returned only with consent AND ``allow_samples``.

        ``allow_samples`` is computed by the caller (``role_can(role, "view_samples")``)
        so this store never imports the webapp.
        """
        col_key = self._column_key(table, run_id, column)
        stats = {}
        if self.backend.exists(self.bucket, col_key):
            stats = self.backend.get_json(self.bucket, col_key) or {}
        if not allow_samples:
            stats = _redact_value_bearing_stats(stats)
        out: dict[str, Any] = {
            "stats": stats,
            "quality": list(stats.get("quality") or []),
            "format_signature": stats.get("format_signature") or "",
            "samples": None,
            "samples_withheld": "",
        }
        if not include_samples:
            return out

        if not allow_samples:
            out["samples_withheld"] = "role"
            _log_sample_read(table, column, actor, withheld="role")
            return out

        consent_ok = False
        if self.consent is not None and hasattr(self.consent, "is_approved"):
            try:
                consent_ok = bool(self.consent.is_approved(table, column))
            except Exception:
                consent_ok = False
        sample_key = self._sample_key(table, run_id, column)
        has_file = self.backend.exists(self.bucket, sample_key)
        if not consent_ok or not has_file:
            out["samples_withheld"] = "no consent"
            _log_sample_read(table, column, actor, withheld="no consent")
            return out

        payload = self.backend.get_json(self.bucket, sample_key) or {}
        out["samples"] = list(payload.get("values") or [])
        _log_sample_read(table, column, actor, withheld="")
        return out

    def has_samples(self, table: str, run_id: str, column: str) -> bool:
        return self.backend.exists(self.bucket, self._sample_key(table, run_id, column))


def _log_sample_read(table: str, column: str, actor: str, *, withheld: str) -> None:
    try:
        from redibis.obs.decision import DecisionRecord, decision
        decision(DecisionRecord(
            stage="profile.samples_read",
            fn="ProfileStore.read",
            table=table,
            column=column,
            verdict="withheld" if withheld else "returned",
            confidence=1.0,
            rule=actor or "unknown",
            inputs={"mode": withheld or "include_samples", "engine": "profile_store"},
        ))
    except Exception:
        pass


def _stats_from_profile(profile_result: Any) -> dict[str, dict]:
    if profile_result is None:
        return {}
    if isinstance(profile_result, dict):
        cols = profile_result.get("columns") or profile_result
        if isinstance(cols, dict):
            return {str(k): dict(v) for k, v in cols.items() if isinstance(v, dict)}
        return {}
    out: dict[str, dict] = {}
    for cp in getattr(profile_result, "column_profiles", None) or []:
        name = getattr(cp, "column", None) or (cp.get("column") if isinstance(cp, dict) else None)
        if not name:
            continue
        if isinstance(cp, dict):
            out[str(name)] = dict(cp)
            continue
        out[str(name)] = {
            "logical_type": getattr(cp, "logical_type", None) or getattr(cp, "dtype", None),
            "physical_type": getattr(cp, "physical_type", None) or getattr(cp, "dtype", None),
            "null_rate": getattr(cp, "null_rate", None),
            "ndv_ratio": getattr(cp, "cardinality_ratio", None),
            "ndv": getattr(cp, "nunique", None),
            "avg_value_length": getattr(cp, "avg_value_length", None),
            "arabic_fraction": getattr(cp, "arabic_fraction", None),
            "constant": getattr(cp, "constant", False),
            "near_constant": getattr(cp, "near_constant", False),
        }
    raw = getattr(profile_result, "raw", None) or {}
    metrics = raw.get("column_metrics") or raw.get("om_metrics") or {}
    if isinstance(metrics, dict):
        for name, m in metrics.items():
            if not isinstance(m, dict):
                continue
            entry = dict(out.get(name) or {})
            for key in ("nunique", "ndv", "null_rate", "min", "max", "mean", "histogram",
                        "format_masks", "inferred_class", "frequent_values"):
                if key in m and key not in entry:
                    entry[key] = m[key]
            if m.get("nunique") is not None:
                entry.setdefault("ndv", m.get("nunique"))
            out[str(name)] = entry
    return out


def _quality_by_column(quality_result: Any) -> dict[str, list]:
    if quality_result is None:
        return {}
    out: dict[str, list] = {}
    if isinstance(quality_result, dict):
        results = quality_result.get("results") or quality_result.get("run_results") or quality_result
        if isinstance(results, dict):
            for name, items in results.items():
                if isinstance(items, list):
                    out[str(name)] = items
        elif isinstance(results, list):
            for item in results:
                if not isinstance(item, dict):
                    continue
                col = str(item.get("column") or item.get("column_name") or "")
                if col:
                    out.setdefault(col, []).append(item)
        return out
    run_results = getattr(quality_result, "run_results", None)
    if isinstance(run_results, dict):
        for name, items in run_results.items():
            if isinstance(items, list):
                out[str(name)] = [
                    i if isinstance(i, dict) else {"result": str(i)} for i in items
                ]
    return out
