"""Column PII scanner — RuleSet-backed adapter over shared recognizers.

``ColumnScanner`` and ``TextScanner`` share one ``RuleSet`` and
``RegexRecognizer.recognize_values`` for column regex evidence;
``detect_pii()`` is a thin shim over this class.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from redibis.models import PIIDetection
from redibis.pii.ner_backend import NERBackend
from redibis.pii.regex_overrides import RegexOverrides
from redibis.pii.rules.ruleset import RuleSet, RuleSetCompiler
from redibis.pii.scan.result import Detection, DetectionResult


def _primary_engine(d: PIIDetection) -> str:
    scores = {
        "phone": float(d.phone_score or 0),
        "regex": float(d.presidio_score or 0),
        "ner": float(d.gliner_score or 0),
        "llm": float(d.llm_score or 0),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "regex"


def pii_detections_to_result(
    detections: list[PIIDetection],
    ruleset: RuleSet,
    *,
    language: str = "en",
    engines_ran: tuple[str, ...] = (),
) -> DetectionResult:
    """Map evidence/verdict rows into the unified ``DetectionResult`` shape."""
    out: list[Detection] = []
    counts: dict[str, int] = {}
    for d in detections:
        et = d.entity_type or "UNKNOWN"
        score = float(
            d.confidence
            or max(
                float(d.presidio_score or 0),
                float(d.gliner_score or 0),
                float(d.phone_score or 0),
                float(d.llm_score or 0),
            )
        )
        out.append(
            Detection(
                entity_type=et,
                score=score,
                engine=_primary_engine(d),
                start=None,
                end=None,
                text=d.column,
                detected=bool(d.detected),
                recognizer=d.presidio_pattern or d.ner_engine or "",
                validator="",
            )
        )
        if d.detected or d.entity_type:
            counts[et] = counts.get(et, 0) + 1
    return DetectionResult(
        kind="column",
        detections=tuple(out),
        entity_counts=counts,
        ruleset_id=ruleset.id,
        ruleset_version=ruleset.version,
        language=language,
        engines_ran=engines_ran,
        char_count=0,
        truncated=False,
    )


class ColumnScanner:
    """Scan DataFrame columns using a compiled ``RuleSet``.

    ``detect()`` returns the legacy ``list[PIIDetection]`` (evidence only).
    ``scan()`` wraps the same path as ``DetectionResult(kind="column")``,
    optionally applying ``VerdictResolver``.
    """

    def __init__(
        self,
        *,
        ruleset: Optional[RuleSet] = None,
        ner_backend: Optional[NERBackend] = None,
    ):
        self._ruleset = ruleset or RuleSetCompiler.default()
        self._ner = ner_backend

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    def detect(
        self,
        df: Any,
        columns: Optional[list[Any]] = None,
        engines: str = "both",
        gliner_always_run: bool = False,
        ner_always_run: bool = False,
        ner_backend: Optional[NERBackend] = None,
        ensemble=None,
        regex_overrides: Optional[RegexOverrides] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        *,
        table: str = "",
        log_samples: bool = False,
        log_regex_hits: bool = True,
        max_regex_hits_logged: int = 20,
        ner_label_groups: Optional[list[list[str]]] = None,
        **kwargs,
    ) -> list[PIIDetection]:
        from redibis.pii.detector import _detect_pii_impl

        ov = (
            regex_overrides
            if regex_overrides is not None
            else self._ruleset.regex_overrides
        )
        effective_rs = self._ruleset
        if ov is not None and ov != self._ruleset.regex_overrides:
            effective_rs = RuleSetCompiler.default(
                regex_overrides=ov,
                thresholds=self._ruleset.thresholds,
                default_region=self._ruleset.default_region,
                context_tokens=dict(self._ruleset.context_tokens),
                ner_labels=list(self._ruleset.ner_labels),
                ner_phrases=dict(self._ruleset.ner_phrases),
            )
        backend = ner_backend if ner_backend is not None else self._ner
        tokens = dict(effective_rs.context_tokens) if effective_rs.context_tokens else None

        # Prefer pack/ruleset region when caller did not supply pii_config.
        if kwargs.get("pii_config") is None and self._ruleset.default_region:
            from redibis.config import PIIConfig, resolve_pii_config
            from dataclasses import replace

            base = resolve_pii_config(None)
            kwargs["pii_config"] = replace(
                base if isinstance(base, PIIConfig) else PIIConfig(),
                default_region=self._ruleset.default_region,
                thresholds=self._ruleset.thresholds,
            )

        return _detect_pii_impl(
            df,
            columns=columns,
            engines=engines,
            gliner_always_run=gliner_always_run,
            ner_always_run=ner_always_run,
            ner_backend=backend,
            ensemble=ensemble,
            regex_overrides=ov,
            progress_callback=progress_callback,
            table=table,
            log_samples=log_samples,
            log_regex_hits=log_regex_hits,
            max_regex_hits_logged=max_regex_hits_logged,
            ner_label_groups=ner_label_groups,
            entity_tokens=tokens,
            ruleset=effective_rs,
            **kwargs,
        )

    def scan(
        self,
        df: Any,
        *,
        columns: Optional[list[Any]] = None,
        engines: str = "both",
        language: str = "en",
        apply_verdicts: bool = False,
        equation_mode: Optional[str] = None,
        **kwargs,
    ) -> DetectionResult:
        """Unified scan → ``DetectionResult(kind="column")``."""
        dets = self.detect(df, columns=columns, engines=engines, **kwargs)
        if apply_verdicts:
            from redibis.pii.rules.resolver import VerdictResolver

            pii_cfg = kwargs.get("pii_config")
            mode = equation_mode or getattr(pii_cfg, "equation_mode", None) or "balanced"
            thr = getattr(pii_cfg, "thresholds", None) or self._ruleset.thresholds
            dets = VerdictResolver().resolve(dets, equation_mode=mode, thresholds=thr)

        ran: list[str] = []
        eng = (engines or "both").lower()
        if eng in ("regex", "both"):
            ran.extend(["regex", "phone"])
        if eng in ("gliner", "ner", "both"):
            ran.append("ner")
        return pii_detections_to_result(
            dets,
            self._ruleset,
            language=language,
            engines_ran=tuple(ran),
        )

    def scan_and_deidentify(
        self,
        df: Any,
        policy: Any,
        *,
        columns: Optional[list[Any]] = None,
        engines: str = "both",
        language: str = "en",
        equation_mode: Optional[str] = None,
        keys: Any = None,
        only_detected: bool = True,
        **kwargs,
    ) -> tuple[DetectionResult, Any]:
        """Scan columns (with verdicts) then apply ``DeidPolicy`` via ``DeidApplier``."""
        from redibis.pii.deid.applier import DeidApplier

        result = self.scan(
            df,
            columns=columns,
            engines=engines,
            language=language,
            apply_verdicts=True,
            equation_mode=equation_mode,
            **kwargs,
        )
        deid = DeidApplier().apply(
            df, result, policy, keys=keys, only_detected=only_detected
        )
        return result, deid
