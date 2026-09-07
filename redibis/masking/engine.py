"""
redibis.masking.engine — apply a MaskingPlan to a DataFrame.

The engine owns per-run secrets (``RunKeys``: a master key + seed, with named
sub-keys via ``key_ref``). Keys are minted per run and never exported with the
data; only the engine (or a re-run with the same keys) can reverse ``encrypt`` /
``fpe`` columns.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from redibis.masking.plan import MaskingPlan, ColumnMaskRule
from redibis.masking import transforms as T
from redibis.masking.regex_library import resolve_pattern


def json_scalar(v):
    """Coerce a pandas/numpy cell value to a JSON-serializable Python scalar."""
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


def _position_bounds(params: dict) -> tuple[int, Optional[int]] | None:
    """Return (start_index, end_index) if position params are set, else None."""
    if "start_index" not in params and "end_index" not in params:
        return None
    start = int(params.get("start_index", 0))
    end_raw = params.get("end_index")
    end = int(end_raw) if end_raw is not None else None
    return start, end


def _apply_segment(value: str, transform_fn, params: dict) -> str:
    bounds = _position_bounds(params)
    if bounds is None:
        return transform_fn(value)
    start, end = bounds
    return T.apply_position_slice(value, transform_fn, start_index=start, end_index=end)


@dataclass
class RunKeys:
    """Per-run secret material. ``to_public()`` strips key bytes for manifests."""
    run_id: str
    seed: str
    master_key_hex: str
    key_refs: dict = field(default_factory=dict)   # name → {algo/created_at} metadata

    @property
    def master_key(self) -> bytes:
        return bytes.fromhex(self.master_key_hex)

    @classmethod
    def mint(cls, run_id: Optional[str] = None, seed: Optional[str] = None) -> "RunKeys":
        rid = run_id or f"mask_{secrets.token_hex(6)}"
        sd = seed or secrets.token_hex(16)
        # Master key derived from the seed so a known seed reproduces a run.
        mk = hashlib.sha256(("redibis-mask:" + sd).encode("utf-8")).hexdigest()
        return cls(run_id=rid, seed=sd, master_key_hex=mk,
                   key_refs={"k1": {"algo": "hmac-sha256"}})

    def to_secret_dict(self) -> dict:
        """Full secret payload — stored under session/_meta/env, NEVER exported."""
        return {"run_id": self.run_id, "seed": self.seed,
                "master_key_hex": self.master_key_hex, "key_refs": self.key_refs}

    @classmethod
    def from_secret_dict(cls, d: dict) -> "RunKeys":
        return cls(run_id=d["run_id"], seed=d["seed"],
                   master_key_hex=d["master_key_hex"],
                   key_refs=dict(d.get("key_refs") or {"k1": {}}))

    def to_public(self) -> dict:
        """Manifest-safe view — references only, no key bytes or seed value."""
        return {"run_id": self.run_id, "seed_ref": "session/_meta/env",
                "key_refs": sorted(self.key_refs.keys())}


class MaskingEngine:
    """Applies a plan to a DataFrame using per-run keys."""

    def __init__(self, plan: MaskingPlan, keys: RunKeys):
        self.plan = plan
        self.keys = keys
        self.column_run_meta: dict[str, dict] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    def transform_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        self.column_run_meta = {}
        out = df.copy()
        name_col = self.plan.name_column()
        # Pass 1: everything except email (so faked names exist for derivation).
        for rule in self.plan.columns:
            if rule.column not in out.columns:
                continue
            if rule.strategy == "fake" and rule.params.get("kind") == "email":
                continue
            out[rule.column] = self._transform_series(df[rule.column], rule, out, name_col)
        # Pass 2: emails (may reference the faked name column).
        for rule in self.plan.columns:
            if rule.column not in out.columns:
                continue
            if not (rule.strategy == "fake" and rule.params.get("kind") == "email"):
                continue
            out[rule.column] = self._transform_series(df[rule.column], rule, out, name_col)
        return out

    def compare_sample(self, df: pd.DataFrame, rows: int = 10) -> dict:
        """Original-vs-transformed side-by-side sample for the preview UI."""
        sample = df.head(rows)
        transformed = self.transform_dataframe(sample)
        cols = [c for c in df.columns]
        return {
            "columns": cols,
            "strategies": {r.column: r.strategy for r in self.plan.columns},
            "original": sample.where(pd.notnull(sample), None).values.tolist(),
            "transformed": transformed.where(pd.notnull(transformed), None).values.tolist(),
            "rows": int(min(rows, len(df))),
        }

    def compare_record(self, df: pd.DataFrame, row_index: int = 0) -> dict:
        """Single-row original vs transformed for the record navigator UI."""
        total = len(df)
        if total == 0:
            return {"row_index": 0, "total_rows": 0, "fields": [], "strategies": {}}
        idx = max(0, min(row_index, total - 1))
        sample = df.iloc[idx: idx + 1]
        transformed = self.transform_dataframe(sample)
        ent_map = {r.column: r.detected_entity for r in self.plan.columns}
        strat_map = {r.column: r.strategy for r in self.plan.columns}
        fields = []
        for col in df.columns:
            raw = sample[col].iloc[0]
            masked = transformed[col].iloc[0]
            fields.append({
                "column": col,
                "entity": ent_map.get(col),
                "strategy": strat_map.get(col, "passthrough"),
                "raw": json_scalar(raw),
                "masked": json_scalar(masked),
            })
        return {
            "row_index": idx,
            "total_rows": total,
            "fields": fields,
            "strategies": strat_map,
        }

    # ── Per-column ──────────────────────────────────────────────────────────

    def _transform_series(self, series: pd.Series, rule: ColumnMaskRule,
                          working: pd.DataFrame, name_col: Optional[str]) -> pd.Series:
        strat = rule.strategy
        if strat == "passthrough":
            return series
        if strat == "redact":
            replacement = rule.params.get("replacement", None)
            return series.map(lambda v: replacement)

        mk = self.keys.master_key
        col = rule.column
        det = rule.deterministic
        require_crypto = self.plan.require_authenticated_crypto

        def per_value(idx_value):
            idx, v = idx_value
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return v
            s = str(v)
            p = rule.params
            if strat == "mask":
                def _mask(seg: str) -> str:
                    return T.mask_value(seg, keep_first=p.get("keep_first", 0),
                                        keep_last=p.get("keep_last", 4),
                                        mask_char=p.get("mask_char", "*"),
                                        preserve_length=p.get("preserve_length", True))
                return _apply_segment(s, _mask, p)
            if strat == "hash":
                def _hash(seg: str) -> str:
                    hkey = T._derive_key(mk, p["hmac_key_ref"]) if p.get("hmac_key_ref") else None
                    return T.hash_value(seg, algo=p.get("algo", "sha256"), hmac_key=hkey,
                                        truncate=int(p.get("truncate", 0)),
                                        prefix=p.get("prefix", ""))
                return _apply_segment(s, _hash, p)
            if strat == "encrypt":
                key = T._derive_key(mk, p.get("key_ref", "k1"))
                return _apply_segment(
                    s,
                    lambda seg: T.encrypt_value(seg, key, require_aes_gcm=require_crypto),
                    p,
                )
            if strat == "fpe":
                key = T._derive_key(mk, p.get("key_ref", "k1"))
                meta: dict = {"modes_used": set()}
                def _fpe(seg: str) -> str:
                    return T.fpe_transform(
                        seg, key,
                        alphabet=p.get("alphabet", "digits"),
                        tweak=p.get("tweak", "") or col,
                        mode=p.get("mode"),
                        short_value_policy=p.get("short_value_policy", "error"),
                        column=col,
                        require_ff3=require_crypto,
                        meta_out=meta,
                    )
                result = _apply_segment(s, _fpe, p)
                if meta.get("modes_used"):
                    prev = self.column_run_meta.get(col, {}).get("modes_used", set())
                    self.column_run_meta[col] = {
                        "modes_used": prev | meta["modes_used"],
                        "mode": p.get("mode") or T.resolve_fpe_mode(
                            None, column=col, require_ff3=require_crypto,
                        ),
                    }
                return result
            if strat == "fake":
                return _apply_segment(
                    s,
                    lambda seg: self._fake_value(seg, idx, rule, mk, col, det, working, name_col),
                    p,
                )
            return s

        return pd.Series([per_value((i, v)) for i, v in zip(series.index, series.tolist())],
                         index=series.index)

    def _fake_value(self, s: str, idx, rule: ColumnMaskRule, mk: bytes, col: str,
                    det: bool, working: pd.DataFrame, name_col: Optional[str]) -> str:
        p = rule.params
        kind = p.get("kind", "free_text")
        key_ref = p.get("key_ref", "k1")
        rng = T._value_rng(mk, key_ref, col, s, det)
        locale = T.resolve_locale(
            s,
            T.effective_faker_locale(p.get("locale"), self.plan.default_locale),
        )
        preserve = p.get("preserve", {}) or {}

        if kind == "name":
            gender = "f" if preserve.get("gender") and rng.randint(0, 1) else \
                     ("m" if preserve.get("gender") else None)
            return T.fake_name(rng, locale, gender=gender)
        if kind == "address":
            return T.fake_address(rng, locale)
        if kind == "company":
            return T.fake_company(rng, locale)
        if kind == "phone":
            return T.fake_phone(rng, s, preserve_format=preserve.get("format", True),
                                preserve_country_code=preserve.get("country_code", True),
                                region=preserve.get("region", ""))
        if kind == "email":
            local_part = None
            if name_col and name_col in working.columns:
                try:
                    faked_name = working.at[idx, name_col]
                    if faked_name is not None and not (isinstance(faked_name, float) and pd.isna(faked_name)):
                        local_part = str(faked_name)
                except Exception:
                    local_part = None
            return T.fake_email(rng, s, preserve_domain=preserve.get("domain", True),
                                local_part=local_part)
        if kind == "national_id":
            return T.fake_national_id(rng, s)
        if kind in ("credit_card", "iban"):
            return T.fake_credit_card(rng, s)
        if kind == "date":
            return T.fake_date(rng, s, jitter_days=int(p.get("jitter_days", 30)))
        if kind == "uuid":
            return T.fake_uuid(rng)
        if kind == "regex":
            pattern = p.get("regex_pattern") or ""
            if not pattern:
                lib_name = p.get("regex_library") or ""
                pattern = resolve_pattern(lib_name) or ""
            if not pattern:
                raise ValueError(
                    f"fake/regex on column {col!r}: set regex_pattern or regex_library"
                )
            return T.fake_from_regex(rng, pattern, deterministic=det)
        # free_text
        if p.get("redact", True):
            return "[redacted]"
        return "lorem ipsum"


# ─────────────────────────────────────────────────────────────────────────────
# Risk analysis (the §6 risk panel)
# ─────────────────────────────────────────────────────────────────────────────

_QUASI_HINTS = ("dob", "birth", "zip", "postal", "gender", "sex", "age", "nationality")


def _fpe_manifest_mode(rule: ColumnMaskRule, run_meta: Optional[dict]) -> tuple[str, str]:
    """Return (mode, algorithm) for manifest column entries."""
    configured = (rule.params or {}).get("mode")
    if run_meta:
        modes_used = run_meta.get("modes_used") or set()
        if "keystream" in modes_used and "ff3" in modes_used:
            mode = "mixed"
        elif "passthrough" in modes_used and modes_used - {"passthrough"}:
            mode = "mixed"
        elif "keystream" in modes_used:
            mode = "keystream"
        elif "ff3" in modes_used:
            mode = "ff3"
        else:
            mode = configured or run_meta.get("mode", "keystream")
        return mode, T.fpe_algorithm_label(mode, modes_used)
    mode = configured or "keystream"
    return mode, T.fpe_algorithm_label(mode)


def column_manifest_entry(rule: ColumnMaskRule,
                          run_meta: Optional[dict] = None) -> dict:
    entry = {
        "column": rule.column,
        "strategy": rule.strategy,
        "detected_entity": rule.detected_entity,
        "deterministic": rule.deterministic,
        "params": dict(rule.params or {}),
    }
    if rule.strategy == "fpe":
        mode, algo = _fpe_manifest_mode(rule, run_meta)
        entry["mode"] = mode
        entry["algorithm"] = algo
    elif rule.strategy == "encrypt":
        entry["algorithm"] = "AES-256-GCM" if T._HAS_AESGCM else "HMAC-XOR (legacy)"
    return entry


def build_manifest(plan: MaskingPlan, keys: RunKeys, *,
                   column_run_meta: Optional[dict] = None,
                   rows: int = 0,
                   session_id: Optional[str] = None,
                   created_at: Optional[str] = None) -> dict:
    """Build a v2 masking manifest (plan + key references, never key bytes)."""
    meta = column_run_meta or {}
    manifest = {
        "redibis_masking_manifest": "v2",
        "schema_table": plan.schema_table,
        "run_id": keys.run_id,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "default_locale": plan.default_locale,
        "require_authenticated_crypto": plan.require_authenticated_crypto,
        "keys": keys.to_public(),
        "columns": [
            column_manifest_entry(c, meta.get(c.column))
            for c in plan.columns
        ],
        "capabilities": T.capabilities(),
    }
    if session_id:
        manifest["session_id"] = session_id
    return manifest


def build_audit_report(plan: MaskingPlan, keys: RunKeys, *,
                       df: pd.DataFrame,
                       column_run_meta: Optional[dict] = None,
                       session_id: Optional[str] = None,
                       created_at: Optional[str] = None,
                       export_format: str = "csv",
                       export_filename: str = "",
                       source_label: Optional[str] = None) -> dict:
    """Build a self-contained audit report for a masked export (no key bytes)."""
    meta = column_run_meta or {}
    col_names = [c for c in df.columns]
    plan_rules = [r for r in plan.columns if r.column in df.columns]
    by_strategy: dict[str, int] = {}
    transformed = 0
    pii_detected = 0
    for rule in plan_rules:
        by_strategy[rule.strategy] = by_strategy.get(rule.strategy, 0) + 1
        if rule.strategy != "passthrough":
            transformed += 1
        if rule.detected_entity:
            pii_detected += 1

    risk = risk_report(df, plan)
    risk_counts = {
        sev: sum(1 for f in risk if f.get("severity") == sev)
        for sev in ("high", "medium", "low")
    }
    ts = created_at or datetime.now(timezone.utc).isoformat()
    manifest = build_manifest(
        plan, keys,
        column_run_meta=meta,
        rows=len(df),
        session_id=session_id,
        created_at=ts,
    )

    return {
        "redibis_masking_audit": "v1",
        "run_id": keys.run_id,
        "created_at": ts,
        "session_id": session_id,
        "schema_table": plan.schema_table,
        "source": {
            "label": source_label or plan.schema_table,
            "rows": len(df),
            "columns": len(col_names),
            "column_names": col_names,
        },
        "export": {
            "format": export_format,
            "filename": export_filename,
        },
        "summary": {
            "columns_total": len(plan_rules),
            "columns_transformed": transformed,
            "columns_passthrough": len(plan_rules) - transformed,
            "pii_columns_detected": pii_detected,
            "by_strategy": by_strategy,
            "risk_high": risk_counts["high"],
            "risk_medium": risk_counts["medium"],
            "risk_low": risk_counts["low"],
        },
        "columns": [
            column_manifest_entry(c, meta.get(c.column))
            for c in plan_rules
        ],
        "risk_findings": risk,
        "keys": keys.to_public(),
        "manifest": manifest,
        "compliance_notes": [
            "Key material and seeds are NOT included in the export, manifest, or audit report.",
            "Reversible strategies (encrypt, fpe) can be reversed only by the key-holder using "
            "per-run keys stored under session/_meta/env.",
            "Each apply mints new keys unless the plan seed is reused; exports are not "
            "cross-linkable across runs unless keys are reused.",
        ],
    }


def risk_report(df: pd.DataFrame, plan: MaskingPlan) -> list[dict]:
    """Flag weak choices: low-cardinality deterministic hashing, free-text leaks,
    reversible columns, and lingering quasi-identifiers."""
    findings: list[dict] = []
    n = max(1, len(df))
    for rule in plan.columns:
        col = rule.column
        if col not in df.columns:
            continue
        nunique = int(df[col].nunique(dropna=True))
        cardinality = nunique / n

        if rule.strategy == "hash" and rule.deterministic and cardinality < 0.1:
            findings.append({"column": col, "severity": "high",
                "issue": "Deterministic hash on low-cardinality column",
                "detail": f"{nunique} distinct values — vulnerable to dictionary attack. "
                          "Use a per-run HMAC salt (set) or switch to 'fake'."})
        if rule.strategy == "passthrough":
            low = col.lower()
            if any(h in low for h in _QUASI_HINTS):
                findings.append({"column": col, "severity": "medium",
                    "issue": "Quasi-identifier passed through",
                    "detail": "DOB/ZIP/gender-like field left as-is can re-identify "
                              "individuals in combination. Consider masking or generalizing."})
        p = rule.params or {}
        if rule.strategy in ("encrypt", "fpe"):
            if rule.strategy == "fpe":
                mode = p.get("mode") or ("ff3" if T._HAS_FF3 else "keystream")
                if mode == "keystream" or p.get("mode") == "keystream":
                    findings.append({"column": col, "severity": "high",
                        "issue": "Legacy keystream FPE",
                        "detail": "Recoverable from a single known plaintext. "
                                  "Install ff3 and set mode: ff3 (default when installed)."})
                elif p.get("short_value_policy") == "keystream":
                    findings.append({"column": col, "severity": "medium",
                        "issue": "Mixed-mode FPE column",
                        "detail": "Short values fall back to legacy keystream while "
                                  "long values use FF3-1. Manifest records mixed mode."})
                else:
                    findings.append({"column": col, "severity": "low",
                        "issue": "Reversible strategy (fpe)",
                        "detail": "Reversible by the key-holder only; keys are per-run and "
                                  "never exported. Safe to share the data; keep the keys back."})
            else:
                if not T._HAS_AESGCM and plan.require_authenticated_crypto:
                    findings.append({"column": col, "severity": "high",
                        "issue": "Unauthenticated encrypt fallback blocked",
                        "detail": "cryptography not installed; export would fail with "
                                  "require_authenticated_crypto: true."})
                elif not T._HAS_AESGCM:
                    findings.append({"column": col, "severity": "high",
                        "issue": "Unauthenticated fallback cipher",
                        "detail": "cryptography not installed — encrypt uses HMAC-XOR (x1:). "
                                  "Install cryptography or set require_authenticated_crypto: false."})
                else:
                    findings.append({"column": col, "severity": "low",
                        "issue": "Reversible strategy (encrypt)",
                        "detail": "Reversible by the key-holder only; keys are per-run and "
                                  "never exported. Safe to share the data; keep the keys back."})
        if rule.strategy == "fake" and rule.params.get("kind") == "free_text" \
                and not rule.params.get("redact", True):
            findings.append({"column": col, "severity": "medium",
                "issue": "Free-text not redacted",
                "detail": "Narrative text can leak PII mid-sentence. Prefer redact "
                          "unless the text was separately scrubbed."})
    return findings
