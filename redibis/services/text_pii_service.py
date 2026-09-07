"""Service facade for free-text PII scan + de-identification."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from redibis.pii.deid.applier import DeidApplier, DeidResult
from redibis.pii.deid.policy import DeidPolicy, suggest_policy_from_detections
from redibis.pii.rules.ruleset import RuleSet, RuleSetCompiler
from redibis.pii.scan.result import DetectionResult, TextScanConfig
from redibis.pii.scan.text_scanner import TextPIIScan, TextScanner

logger = logging.getLogger("services.text_pii")


class TextPIIServiceError(Exception):
    """Raised for validation / fail-closed errors (mapped to HTTP by routes)."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class TextPIIService:
    """Single config/factory boundary for REST + CLI adapters."""

    def __init__(
        self,
        *,
        redibis_config: Any = None,
        ruleset: Optional[RuleSet] = None,
        ner_backend: object | None = None,
        policies: Optional[dict[str, DeidPolicy]] = None,
    ):
        self._cfg = redibis_config
        self._policies = dict(policies or {})
        self._ruleset = ruleset or self._compile_ruleset()
        self._ner = ner_backend if ner_backend is not None else self._try_load_ner()
        self._llm = self._try_llm()
        self._scanner = TextScanner(
            ruleset=self._ruleset,
            ner_backend=self._ner,
            llm_refiner=self._llm,
        )
        self._facade = TextPIIScan(
            ruleset=self._ruleset,
            ner_backend=self._ner,
            llm_refiner=self._llm,
        )

    @property
    def config(self) -> Any:
        """The ``RedibisConfig`` this service was built from (may be ``None``)."""
        return self._cfg

    def _compile_ruleset(self) -> RuleSet:
        """Prefer active pack stack / config packs; fall back to builtin default."""
        thr = None
        region = "EG"
        labels = None
        overrides = None
        if self._cfg is not None:
            pii = getattr(self._cfg, "pii", None)
            if pii is not None:
                thr = getattr(pii, "thresholds", None)
                region = getattr(pii, "default_region", "EG") or "EG"
                ner = getattr(pii, "ner", None)
                if ner and getattr(ner, "labels", None):
                    labels = list(ner.labels)
                if getattr(pii, "regex_overrides", None):
                    from redibis.pii.regex_overrides import RegexOverrides

                    overrides = RegexOverrides.from_dict(pii.regex_overrides)

            # Config-referenced pack paths
            packs = getattr(self._cfg, "packs", None) or []
            if packs:
                try:
                    from redibis.pack import apply_packs_from_config

                    stack = apply_packs_from_config(self._cfg)
                    return RuleSetCompiler.from_stack(stack)
                except Exception as exc:
                    logger.info("pack RuleSet from config packs skipped: %s", exc)

            # Imported active stack (Settings Packs tab)
            try:
                from redibis.pack import PackStackStore

                store = PackStackStore()
                layers = [L for L in store.list_layers() if L.path]
                if layers:
                    stack = store.resolve(self._cfg)
                    return RuleSetCompiler.from_stack(stack)
            except Exception as exc:
                logger.info("pack RuleSet from active stack skipped: %s", exc)

        return RuleSetCompiler.default(
            regex_overrides=overrides,
            thresholds=thr,
            default_region=region,
            ner_labels=labels,
        )

    def _try_load_ner(self):
        """Same resolution as column scans: config path, env, Settings, then sole model."""
        if self._cfg is None:
            return None
        pii = getattr(self._cfg, "pii", None)
        if pii is None:
            return None
        from dataclasses import replace

        from redibis.config import load_global_settings_optional
        from redibis.pii.ner_registry import NERModelRegistry

        ner = pii.ner
        path = str(getattr(ner, "model_path", "") or "").strip()
        if not path:
            gs = load_global_settings_optional()
            path = str(gs.get("pii_gliner_model") or "").strip()
            pii_block = gs.get("pii")
            if not path and isinstance(pii_block, dict):
                path = str(pii_block.get("gliner_model") or "").strip()
            if path:
                ner = replace(ner, model_path=path)
        raw_dir = str(getattr(pii, "models_dir", "") or "").strip()
        # Default "/models" would skip ./models, where local weights actually live.
        models_dir = raw_dir if raw_dir and raw_dir != "/models" else None
        backend = NERModelRegistry.try_load(
            ner=ner,
            gliner=getattr(pii, "gliner", None),
            models_dir=models_dir,
        )
        if backend is None:
            logger.info(
                "text NER backend not attached (path=%r models_dir=%r) — regex/phone only",
                path,
                models_dir,
            )
        else:
            logger.info("text NER backend %s", getattr(backend, "name", backend))
        return backend

    def _try_llm(self):
        """Attach a refiner when ``pii.llm.enabled`` OR ``pii.text_refiner`` is bound."""
        from redibis.pii.text_llm import LlmTextRefiner

        llm = getattr(getattr(self._cfg, "pii", None), "llm", None) if self._cfg else None
        if llm and getattr(llm, "enabled", False):
            return LlmTextRefiner(redibis_config=self._cfg)

        # Capability-role path (Settings → Text Gateway Models / LLM roles)
        try:
            from redibis.config import load_global_settings_optional
            from redibis.enrich.capability_routing import RoutingError, get_provider_for_role

            gs = load_global_settings_optional()
            agents_cfg = getattr(self._cfg, "agents", None) if self._cfg else None
            get_provider_for_role("pii.text_refiner", gs=gs, agents_cfg=agents_cfg)
            return LlmTextRefiner(redibis_config=self._cfg)
        except Exception:
            return None

    def scan(
        self,
        text: str,
        *,
        language: str = "en",
        engines: str = "both",
        min_score: float = 0.35,
        return_text: bool = True,
        resolve: str = "priority",
        use_llm: bool = False,
        entities: Optional[list[str]] = None,
        max_chars: int = 50_000,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
        llm_provider: str = "",
        llm_model: str = "",
        preprocess_obfuscation: Optional[bool] = None,
        preprocess_expanders: Optional[list[str]] = None,
    ) -> DetectionResult:
        if text is None:
            raise TextPIIServiceError("text is required")
        if len(text) > max_chars:
            raise TextPIIServiceError(
                f"text exceeds max_chars ({max_chars})",
                status_code=413,
            )
        t0 = time.perf_counter()
        # Default preprocess off for library/admin API; Gateway opts in explicitly.
        if preprocess_obfuscation is None:
            preprocess_obfuscation = False
        cfg = TextScanConfig(
            engines=engines,
            language=language,
            min_score=min_score,
            return_text=return_text,
            resolve=resolve,
            use_llm=use_llm,
            max_chars=max_chars,
            entities=tuple(entities or ()),
            default_region=self._ruleset.default_region,
            arabic=(language or "").startswith("ar"),
            preprocess_obfuscation=bool(preprocess_obfuscation),
            preprocess_expanders=tuple(preprocess_expanders or ()),
        )
        llm_override = None
        if use_llm and (llm_provider or "").strip():
            llm_override = self._build_llm_override(llm_provider.strip(), (llm_model or "").strip())
        result = self._scanner.scan(text, cfg, progress_cb=progress_cb, llm_override=llm_override)
        # Metadata-only logging — never log raw body
        logger.info(
            "text_pii_scan chars=%s entities=%s engines=%s language=%s latency_ms=%.1f "
            "engines_unavailable=%s",
            result.char_count,
            dict(result.entity_counts),
            list(result.engines_ran),
            result.language,
            (time.perf_counter() - t0) * 1000,
            dict(result.engines_unavailable),
        )
        return result

    def _build_llm_override(self, provider_name: str, model: str):
        """Build a one-off refiner bound to a request-selected provider.

        Validated against the same provider registry used everywhere else
        (``list_providers`` / ``get_provider``) — never an arbitrary
        client-supplied endpoint. Raises ``TextPIIServiceError`` (400) for an
        unknown provider name so the route can surface a clean error.
        """
        from redibis.enrich.providers import EnrichmentError, get_provider
        from redibis.pii.text_llm import LlmTextRefiner

        try:
            provider = get_provider(provider_name, model=model)
        except (ValueError, EnrichmentError) as exc:
            raise TextPIIServiceError(f"unknown llm_provider {provider_name!r}: {exc}") from exc
        refiner = LlmTextRefiner(redibis_config=self._cfg, provider=provider)
        try:
            # Explicit per-request provider selection still honors the
            # local/cloud free-text gate (pii.llm.allow_external_raw_text).
            refiner._assert_local_provider(provider_name)
        except RuntimeError as exc:
            raise TextPIIServiceError(str(exc), status_code=403) from exc
        return refiner

    def deidentify(
        self,
        text: str,
        *,
        policy: Optional[DeidPolicy] = None,
        policy_id: Optional[str] = None,
        scan_kwargs: Optional[dict] = None,
    ) -> tuple[DetectionResult, DeidResult]:
        pol = policy
        if pol is None and policy_id:
            pol = self._policies.get(policy_id)
            if pol is None:
                # Reload from disk in case a policy was saved after construction
                self._policies.update(self._load_pack_policies())
                pol = self._policies.get(policy_id)
        if pol is None:
            raise TextPIIServiceError(
                "de-identification requires a policy (fail-closed)",
                status_code=400,
            )
        kwargs = dict(scan_kwargs or {})
        resolve = str(kwargs.get("resolve") or "priority")
        if resolve == "all":
            raise TextPIIServiceError(
                "resolve='all' is not allowed for de-identification "
                "(overlapping spans); use 'priority' or 'longest'",
                status_code=400,
            )
        result = self.scan(text, **kwargs)
        # Align policy fake locale with scan language + pack faker map when
        # the policy still carries a bare language tag (e.g. "fr" / "en").
        lang = str(kwargs.get("language") or result.language or "en")
        pol_locale = (pol.locale or "").strip()
        if len(pol_locale) <= 3 or pol_locale.lower() in ("en", "ar", "fr", "de", "es"):
            from dataclasses import replace

            pol = replace(pol, locale=self._ruleset.locale_for_language(lang))
        deid = DeidApplier().apply(text, result, pol)
        logger.info(
            "text_pii_deid chars=%s applied=%s policy=%s reversible=%s",
            len(text),
            len(deid.applied),
            deid.policy_id,
            deid.reversible_spans,
        )
        return result, deid

    def scan_batch(
        self,
        texts: list[str],
        *,
        max_items: int = 50,
        **scan_kwargs,
    ) -> list[DetectionResult]:
        if not isinstance(texts, list):
            raise TextPIIServiceError("texts must be a list")
        if len(texts) > max_items:
            raise TextPIIServiceError(
                f"batch exceeds max_items ({max_items})",
                status_code=413,
            )
        return [self.scan(t if t is not None else "", **scan_kwargs) for t in texts]

    def save_policy(self, policy: DeidPolicy, *, root: Optional[Any] = None) -> str:
        from pathlib import Path
        from redibis.pii.deid.pack_store import default_deid_policies_root, save_deid_policy

        dest = Path(root) if root else default_deid_policies_root()
        path = save_deid_policy(dest, policy)
        self.register_policy(policy)
        return str(path)

    def _load_pack_policies(self) -> dict[str, DeidPolicy]:
        from redibis.pii.deid.pack_store import default_deid_policies_root, load_deid_policies

        try:
            return load_deid_policies(default_deid_policies_root())
        except Exception as exc:
            logger.info("deid pack policies not loaded: %s", exc)
            return {}

    def entities(self, *, language: str = "en") -> dict:
        return {
            "entities": self._ruleset.entity_catalogue(),
            "language": language,
            "offset_unit": "unicode_codepoint",
            "ruleset_id": self._ruleset.id,
            "ruleset_version": self._ruleset.version,
        }

    def policies(self) -> dict:
        return {
            "policies": [
                {"id": p.id, "version": p.version, "locale": p.locale}
                for p in self._policies.values()
            ]
        }

    def get_policy(self, policy_id: str) -> Optional[DeidPolicy]:
        return self._policies.get(policy_id)

    def register_policy(self, policy: DeidPolicy) -> None:
        self._policies[policy.id] = policy

    def suggest_policy(self, text: str, **scan_kwargs) -> DeidPolicy:
        result = self.scan(text, **scan_kwargs)
        lang = str(scan_kwargs.get("language") or result.language or "en")
        return suggest_policy_from_detections(
            list(result.detections),
            locale=self._ruleset.locale_for_language(lang),
        )

    def health(self) -> dict:
        # "configured": a model path resolved and a backend object exists.
        # "loadable": the backend actually loaded (or would load) weights —
        # distinct because a broken interpreter (e.g. torch import failure)
        # can leave a backend *attached* but never *loadable*.
        ner_info: dict[str, Any] = {"configured": False, "loadable": False, "loaded": False}
        if self._ner is not None:
            try:
                ner_info = dict(self._ner.health_check())
            except Exception as exc:
                ner_info = {"error": str(exc)}
            ner_info["configured"] = True
            ner_info["loadable"] = bool(ner_info.get("loadable"))
            ner_info["loaded"] = ner_info["loadable"]  # back-compat alias
        llm_enabled = bool(
            getattr(getattr(getattr(self._cfg, "pii", None), "llm", None), "enabled", False)
        )
        role_bound = False
        role_provider = ""
        try:
            from redibis.config import load_global_settings_optional
            from redibis.enrich.capability_routing import get_provider_for_role

            gs = load_global_settings_optional()
            agents_cfg = getattr(self._cfg, "agents", None) if self._cfg else None
            prov, binding = get_provider_for_role(
                "pii.text_refiner", gs=gs, agents_cfg=agents_cfg
            )
            role_bound = True
            role_provider = binding.provider or getattr(prov, "name", "") or ""
        except Exception:
            role_bound = False
        # Probe regex/presidio availability
        regex_ok = True
        try:
            from redibis.pii.presidio_nlp import build_pattern_analyzer_engine  # noqa: F401
            from presidio_analyzer import RecognizerRegistry  # noqa: F401
        except ImportError:
            regex_ok = False
        return {
            "engines": {
                "regex": {"available": regex_ok},
                "phone": {"available": True},
                "preprocess": {"available": True},
                "ner": ner_info,
                "llm": {
                    "enabled": llm_enabled or role_bound,
                    "available": self._llm is not None or role_bound,
                    "role_bound": role_bound,
                    "role_provider": role_provider,
                },
            },
            "ruleset_id": self._ruleset.id,
            "ruleset_version": self._ruleset.version,
            "languages": sorted(self._ruleset.phone_regions.keys()),
            "offset_unit": "unicode_codepoint",
        }

    def redact(self, text: str, result: DetectionResult) -> str:
        """CLI --redact helper: replace spans with [ENTITY_TYPE] right-to-left."""
        out = text
        spans = sorted(
            [d for d in result.detections if d.start is not None and d.end is not None],
            key=lambda d: d.start or 0,
            reverse=True,
        )
        for d in spans:
            assert d.start is not None and d.end is not None
            out = out[: d.start] + f"[{d.entity_type}]" + out[d.end :]
        return out


def text_pii_service_from_config(cfg: Any = None, *, policies: Optional[dict] = None) -> TextPIIService:
    return TextPIIService(redibis_config=cfg, policies=policies)


def text_pii_service_from_env() -> TextPIIService:
    import os

    from redibis.config import RedibisConfig
    from redibis.pii.deid.pack_store import default_deid_policies_root, load_deid_policies

    path = os.environ.get("REDIBIS_CONFIG")
    cfg = RedibisConfig.from_yaml(path) if path else RedibisConfig()
    policies: dict[str, DeidPolicy] = {
        "full-redact": DeidPolicy.redact_all(),
    }
    try:
        policies.update(load_deid_policies(default_deid_policies_root()))
    except Exception as exc:
        logger.info("deid policies dir not loaded: %s", exc)
    return TextPIIService(redibis_config=cfg, policies=policies)
