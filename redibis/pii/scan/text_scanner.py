"""Free-text PII scanner — shared RuleSet recognizers → span DetectionResult."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from redibis.pii.rules.recognizers import (
    NerRecognizer,
    PhoneRecognizer,
    RecognizeContext,
    RegexRecognizer,
)
from redibis.pii.rules.resolver import SpanResolver
from redibis.pii.rules.ruleset import RuleSet, RuleSetCompiler
from redibis.pii.scan.result import Candidate, DetectionResult, TextScanConfig

logger = logging.getLogger("pii.scan.text")


class TextScanner:
    """Scan a free-text string into span detections."""

    def __init__(
        self,
        ruleset: Optional[RuleSet] = None,
        *,
        ner_backend: object | None = None,
        llm_refiner: object | None = None,
        obfuscation_pipeline: object | None = None,
    ):
        self.ruleset = ruleset or RuleSetCompiler.default()
        self._regex = RegexRecognizer(self.ruleset)
        self._phone = PhoneRecognizer(self.ruleset)
        self._ner = NerRecognizer(self.ruleset, backend=ner_backend)
        self._resolver = SpanResolver()
        self._llm = llm_refiner
        self._ner_backend = ner_backend
        self._obfuscation = obfuscation_pipeline

    def _get_obfuscation(self, cfg: TextScanConfig):
        if not cfg.preprocess_obfuscation:
            return None
        if self._obfuscation is not None:
            return self._obfuscation
        from redibis.pii.text_preprocess import default_pipeline

        names = list(cfg.preprocess_expanders) or None
        return default_pipeline(expander_names=names)

    def _ner_health(self) -> dict:
        """Cheap-when-failing, cached-when-succeeding backend readiness check.

        Distinguishes "attached but not loadable" (e.g. broken torch) from a
        legitimate zero-hit scan — a silent ``[]`` from a failed lazy-load
        must never be reported as ``engines_ran: [..., "ner"]``.
        """
        checker = getattr(self._ner_backend, "health_check", None)
        if not callable(checker):
            return {"loadable": True}
        try:
            return dict(checker())
        except Exception as exc:  # pragma: no cover - defensive
            return {"loadable": False, "error": str(exc)}

    def scan(
        self,
        text: str,
        config: Optional[TextScanConfig] = None,
        *,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
        llm_override: object | None = None,
    ) -> DetectionResult:
        """Scan ``text``. ``progress_cb(stage, detail)`` fires after each stage
        completes (``validate``, ``preprocess``, ``regex``, ``phone``, ``ner``,
        ``llm``, ``resolve``) — used by the Text Gateway streaming endpoint.
        Never raises; scanning proceeds even if the callback itself errors.
        """

        def _emit(stage: str, **detail) -> None:
            if progress_cb is None:
                return
            try:
                progress_cb(stage, detail)
            except Exception:  # pragma: no cover - never let UI plumbing break a scan
                logger.debug("progress_cb failed at stage=%s", stage, exc_info=True)

        cfg = config or TextScanConfig()
        raw = text if text is not None else ""
        truncated = False
        if len(raw) > cfg.max_chars:
            raw = raw[: cfg.max_chars]
            truncated = True

        engines = self._engine_set(cfg.engines)
        ctx = RecognizeContext(
            language=cfg.language,
            arabic=cfg.arabic or (cfg.language or "").startswith("ar"),
            group="free_text",
            entities=cfg.entities,
        )
        _emit("validate", char_count=len(raw), truncated=truncated)

        candidates: list[Candidate] = []
        engines_ran: list[str] = []
        engines_unavailable: dict[str, str] = {}

        # Deterministic spoken / obfuscated expanders (original offsets).
        pre_hits: list[Candidate] = []
        pipeline = self._get_obfuscation(cfg)
        if pipeline is not None:
            try:
                pre_hits = list(pipeline.recognize(raw, ctx) or [])
                candidates.extend(pre_hits)
                engines_ran.append("preprocess")
                _emit("preprocess", hits=len(pre_hits))
            except Exception as exc:
                logger.warning("obfuscation preprocess failed: %s", exc)
                engines_unavailable["preprocess"] = str(exc)
                _emit("preprocess", hits=0, unavailable=True, reason=str(exc))
        else:
            _emit("preprocess", hits=0, skipped=True)

        regex_hits: list[Candidate] = []
        if "regex" in engines or "phone" in engines:
            regex_hits = self._regex.recognize(raw, ctx)
            if "regex" in engines:
                candidates.extend(regex_hits)
                engines_ran.append("regex")
        _emit("regex", hits=len(regex_hits) if "regex" in engines else 0)

        if "phone" in engines:
            phone_hits = self._phone.recognize(
                raw, ctx, seed_candidates=list(regex_hits) + list(pre_hits)
            )
            candidates.extend(phone_hits)
            engines_ran.append("phone")
            _emit("phone", hits=len(phone_hits))
        else:
            _emit("phone", hits=0, skipped=True)

        if "ner" in engines and self._ner_backend is not None:
            health = self._ner_health()
            if health.get("loadable", True):
                ner_hits = self._ner.recognize(raw, ctx)
                candidates.extend(ner_hits)
                engines_ran.append("ner")
                _emit("ner", hits=len(ner_hits))
            else:
                reason = str(health.get("error") or "NER backend not loadable")
                engines_unavailable["ner"] = reason
                logger.warning("NER backend unavailable — skipping (%s)", reason)
                _emit("ner", hits=0, unavailable=True, reason=reason)
        elif "ner" in engines:
            engines_unavailable["ner"] = "no NER backend attached (no model configured)"
            logger.info("NER requested but no backend is attached — skipping")
            _emit("ner", hits=0, unavailable=True, reason=engines_unavailable["ner"])
        else:
            _emit("ner", hits=0, skipped=True)

        active_llm = llm_override if llm_override is not None else self._llm
        llm_ran = False
        if cfg.use_llm and active_llm is not None:
            try:
                llm_hits = active_llm.propose_spans(raw, candidates, cfg)
                llm_ran = True
                if llm_hits:
                    candidates.extend(llm_hits)
                engines_ran.append("llm")
                _emit("llm", hits=len(llm_hits or []))
            except Exception as exc:
                logger.warning("LLM text refinement skipped: %s", exc)
                engines_unavailable["llm"] = str(exc)
                _emit("llm", hits=0, unavailable=True, reason=str(exc))
        elif cfg.use_llm and active_llm is None:
            engines_unavailable["llm"] = (
                "no LLM refiner attached (enable pii.llm or bind pii.text_refiner, "
                "or pass llm_provider)"
            )
            _emit("llm", hits=0, unavailable=True, reason=engines_unavailable["llm"])
        else:
            _emit("llm", hits=0, skipped=True)

        # Merge spoken + parenthetical equivalents before overlap resolution.
        if pre_hits:
            try:
                from redibis.pii.text_preprocess.equivalence import VariantEquivalenceMerger

                candidates = VariantEquivalenceMerger().merge(candidates, text=raw)
            except Exception as exc:
                logger.debug("equivalence merge skipped: %s", exc)

        detections = self._resolver.resolve(
            candidates,
            min_score=cfg.min_score,
            mode=cfg.resolve,
            entities=cfg.entities,
        )
        _emit("resolve", detections=len(detections))

        # Enforce source-slice integrity
        checked = []
        for d in detections:
            if d.start is None or d.end is None:
                continue
            if not (0 <= d.start < d.end <= len(raw)):
                continue
            slice_text = raw[d.start:d.end]
            text_out = slice_text if cfg.return_text else ""
            if cfg.return_text and d.text and d.text != slice_text:
                # Trust source offsets
                from dataclasses import replace
                d = replace(d, text=slice_text)
            elif not cfg.return_text:
                from dataclasses import replace
                d = replace(d, text="")
            elif d.text is None:
                from dataclasses import replace
                d = replace(d, text=slice_text)
            checked.append(d)

        counts: dict[str, int] = {}
        for d in checked:
            counts[d.entity_type] = counts.get(d.entity_type, 0) + 1

        result = DetectionResult(
            kind="span",
            detections=tuple(checked),
            entity_counts=counts,
            ruleset_id=self.ruleset.id,
            ruleset_version=self.ruleset.version,
            language=cfg.language,
            engines_ran=tuple(engines_ran),
            char_count=len(raw),
            truncated=truncated,
            engines_unavailable=engines_unavailable,
        )
        _emit("done", entity_count=len(checked), llm_ran=llm_ran)
        return result

    @staticmethod
    def _engine_set(engines: str) -> set[str]:
        e = (engines or "both").lower().strip()
        if e in ("both", "all"):
            return {"regex", "phone", "ner"}
        if e == "regex":
            return {"regex", "phone"}
        if e == "ner":
            return {"ner"}
        if e == "phone":
            return {"phone"}
        # Comma-separated allowlist
        parts = {p.strip() for p in e.split(",") if p.strip()}
        return parts or {"regex", "phone", "ner"}


class TextPIIScan:
    """Public facade matching design doc usage."""

    def __init__(
        self,
        config: Optional[TextScanConfig] = None,
        *,
        ruleset: Optional[RuleSet] = None,
        ner_backend: object | None = None,
        llm_refiner: object | None = None,
    ):
        self.config = config or TextScanConfig()
        self._scanner = TextScanner(
            ruleset=ruleset,
            ner_backend=ner_backend,
            llm_refiner=llm_refiner,
        )

    def scan(self, text: str, config: Optional[TextScanConfig] = None) -> DetectionResult:
        return self._scanner.scan(text, config or self.config)

    def scan_and_deidentify(self, text: str, policy, config: Optional[TextScanConfig] = None):
        from redibis.pii.deid.applier import DeidApplier

        result = self.scan(text, config)
        return DeidApplier().apply(text, result, policy)
