"""Apply a DeidPolicy to span or column DetectionResults via MaskingEngine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

import pandas as pd

from redibis.masking.engine import MaskingEngine, RunKeys
from redibis.masking.plan import ColumnMaskRule, MaskingPlan
from redibis.pii.deid.policy import DeidPolicy, resolve_rule
from redibis.pii.scan.result import Detection, DetectionResult

Source = Union[str, pd.DataFrame]


@dataclass(frozen=True)
class SpanAction:
    """One applied transform — span offsets and/or a column name."""

    entity_type: str
    strategy: str
    start: int
    end: int
    before_len: int
    after_len: int
    rule_id: str
    score: float
    column: str = ""

    def to_dict(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "strategy": self.strategy,
            "start": self.start,
            "end": self.end,
            "before_len": self.before_len,
            "after_len": self.after_len,
            "rule_id": self.rule_id,
            "score": round(self.score, 4),
            "column": self.column,
        }


@dataclass(frozen=True)
class DeidResult:
    """Unified de-id output for text spans or DataFrame columns."""

    original_spans: tuple[Detection, ...]
    deidentified_text: str
    applied: tuple[SpanAction, ...]
    policy_id: str
    policy_version: str
    reversible_spans: int
    run_key_ref: str
    kind: str = "span"
    # Column path only — DataFrame held by reference (not serialized with secrets).
    deidentified_frame: Any = field(default=None, hash=False, compare=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "kind": self.kind,
            "applied": [a.to_dict() for a in self.applied],
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reversible_spans": self.reversible_spans,
            "run_key_ref": self.run_key_ref,
            "original_spans": [
                {
                    "start": s.start,
                    "end": s.end,
                    "entity_type": s.entity_type,
                    "score": s.score,
                    "engine": s.engine,
                    "is_proposal": s.is_proposal,
                    "text": s.text,
                    "detected": s.detected,
                }
                for s in self.original_spans
            ],
        }
        if self.kind == "column" and self.deidentified_frame is not None:
            frame = self.deidentified_frame
            d["deidentified_frame"] = frame.to_dict(orient="list")
            d["deidentified_text"] = ""
        else:
            d["deidentified_text"] = self.deidentified_text
        return d


class DeidApplier:
    """Apply de-id policy to text spans or column DetectionResults.

    Span path: right-to-left splice. Column path: per-cell transform through the
    same ``_transform_span`` / ``MaskingEngine`` primitives so text and column
    outputs match for the same value + keys + strategy.
    """

    def apply(
        self,
        source: Source,
        result: DetectionResult,
        policy: DeidPolicy,
        *,
        keys: Optional[RunKeys] = None,
        only_detected: bool = False,
    ) -> DeidResult:
        if policy is None:
            raise ValueError("policy is required (fail-closed)")
        if result.kind == "column":
            if not isinstance(source, pd.DataFrame):
                raise TypeError("column DetectionResult requires a pandas DataFrame source")
            return self._apply_columns(
                source, result, policy, keys=keys, only_detected=only_detected
            )
        if not isinstance(source, str):
            raise TypeError("span DetectionResult requires a str source")
        return self._apply_spans(source, result, policy, keys=keys)

    def _apply_spans(
        self,
        text: str,
        result: DetectionResult,
        policy: DeidPolicy,
        *,
        keys: Optional[RunKeys] = None,
    ) -> DeidResult:
        run_keys = keys or RunKeys.mint()
        spans = [
            d for d in result.detections
            if d.start is not None and d.end is not None and d.start < d.end
        ]
        # Right-to-left so earlier offsets stay valid
        ordered = sorted(spans, key=lambda d: d.start or 0, reverse=True)
        out = text
        applied: list[SpanAction] = []
        reversible = 0

        for det in ordered:
            assert det.start is not None and det.end is not None
            original = text[det.start:det.end]
            rule = resolve_rule(policy, det.entity_type, det.score)
            transformed = self._transform_span(
                original,
                rule.strategy,
                dict(rule.params),
                entity_type=det.entity_type,
                replacement=rule.replacement,
                locale=policy.locale,
                keys=run_keys,
            )
            if rule.strategy == "fpe":
                reversible += 1
            out = out[: det.start] + transformed + out[det.end :]
            applied.append(SpanAction(
                entity_type=det.entity_type,
                strategy=rule.strategy,
                start=det.start,
                end=det.end,
                before_len=len(original),
                after_len=len(transformed),
                rule_id=f"{policy.id}:{rule.entity_type}",
                score=det.score,
            ))

        applied_ltr = tuple(reversed(applied))
        return DeidResult(
            kind="span",
            original_spans=tuple(spans),
            deidentified_text=out,
            applied=applied_ltr,
            policy_id=policy.id,
            policy_version=policy.version,
            reversible_spans=reversible,
            run_key_ref=run_keys.run_id,
        )

    def _apply_columns(
        self,
        df: pd.DataFrame,
        result: DetectionResult,
        policy: DeidPolicy,
        *,
        keys: Optional[RunKeys] = None,
        only_detected: bool = False,
    ) -> DeidResult:
        run_keys = keys or RunKeys.mint()
        out = df.copy()
        applied: list[SpanAction] = []
        reversible = 0
        used: list[Detection] = []

        for det in result.detections:
            col = (det.text or "").strip()
            if not col or col not in out.columns:
                continue
            et = (det.entity_type or "").strip()
            if not et or et == "UNKNOWN":
                continue
            if only_detected and not det.detected:
                continue
            rule = resolve_rule(policy, et, det.score)
            if rule.strategy == "fpe":
                reversible += 1

            series = out[col]
            new_values: list[Any] = []
            before_lens = 0
            after_lens = 0
            n = 0
            for val in series:
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    try:
                        if pd.isna(val):
                            new_values.append(val)
                            continue
                    except (TypeError, ValueError):
                        pass
                if isinstance(val, str) and val.upper() in ("NONE", "NAN", "N/A", ""):
                    new_values.append(val)
                    continue
                original = str(val)
                transformed = self._transform_span(
                    original,
                    rule.strategy,
                    dict(rule.params),
                    entity_type=et,
                    replacement=rule.replacement,
                    locale=policy.locale,
                    keys=run_keys,
                )
                before_lens += len(original)
                after_lens += len(transformed)
                n += 1
                new_values.append(transformed)
            out[col] = new_values
            used.append(det)
            applied.append(SpanAction(
                entity_type=et,
                strategy=rule.strategy,
                start=0,
                end=n,
                before_len=before_lens,
                after_len=after_lens,
                rule_id=f"{policy.id}:{rule.entity_type}",
                score=det.score,
                column=col,
            ))

        return DeidResult(
            kind="column",
            original_spans=tuple(used),
            deidentified_text="",
            deidentified_frame=out,
            applied=tuple(applied),
            policy_id=policy.id,
            policy_version=policy.version,
            reversible_spans=reversible,
            run_key_ref=run_keys.run_id,
        )

    def _transform_span(
        self,
        value: str,
        strategy: str,
        params: dict,
        *,
        entity_type: str,
        replacement: str,
        locale: str,
        keys: RunKeys,
    ) -> str:
        if strategy == "passthrough":
            return value
        if strategy == "redact":
            return replacement or params.get("replacement") or f"[{entity_type}]"

        # Adapt mask params: start_index/end_index → keep via MaskingEngine
        if strategy == "mask":
            p = dict(params)
            if "keep_last" not in p and "end_index" in p:
                end_idx = int(p.get("end_index", -4))
                if end_idx < 0:
                    p["keep_last"] = abs(end_idx)
                p.setdefault("keep_first", int(p.get("start_index", 0) or 0))
            params = p

        col = "_span"
        rule = ColumnMaskRule(
            column=col,
            strategy=strategy,
            detected_entity=entity_type,
            params=params,
            deterministic=True,
        )
        if strategy == "fake" and "locale" not in rule.params:
            rule.params = {**dict(rule.params), "locale": locale}

        plan = MaskingPlan(
            schema_table="text_deid",
            columns=[rule],
            default_locale=locale,
            require_authenticated_crypto=False,
        )
        engine = MaskingEngine(plan, keys)
        series = pd.Series([value], name=col)
        working = pd.DataFrame({col: series})
        out = engine._transform_series(series, rule, working, None)
        result = out.iloc[0]
        return "" if result is None else str(result)
