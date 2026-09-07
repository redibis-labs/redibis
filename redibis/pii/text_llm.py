"""Optional local-LLM span proposals for free-text PII scanning."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from redibis.pii.scan.result import Candidate, TextScanConfig

logger = logging.getLogger("pii.text_llm")

_SYSTEM_EN = """\
You are a PII span detector. Given a text, propose PII spans as JSON only:
{"spans":[{"start":0,"end":5,"entity_type":"PERSON","score":0.8}]}
Offsets are Unicode code-point indices into the given text (Python string indices).
Only propose clear PII. Do not invent offsets that do not match the text.
Entity types include: PERSON, PHONE_NUMBER, EG_NATIONAL_ID, EMAIL_ADDRESS,
IMEI, IMSI, ICCID, CREDIT_CARD, LOCATION, AGE, PASSWORD_HASH, API_KEY, SECRET,
OTP, PASSPORT, IBAN_CODE.
"""

_SYSTEM_AR = """\
You are a PII span detector for Arabic and mixed Arabic/English text, including
Egyptian call-center transcripts where digits may be spoken as words
(e.g. "زيرو حداشر أربعة…" → phone 011…). Propose PII spans as JSON only:
{"spans":[{"start":0,"end":5,"entity_type":"PHONE_NUMBER","score":0.8}]}
Offsets MUST be Unicode code-point indices into the given text (Python len/slice).
The span text MUST equal text[start:end] exactly — including spoken words and
parenthetical digits when both appear together.
Entity types: PERSON, PHONE_NUMBER, EG_NATIONAL_ID, EMAIL_ADDRESS, IMEI, IMSI,
ICCID, CREDIT_CARD, LOCATION, AGE, PASSWORD_HASH, API_KEY, SECRET, OTP,
PASSPORT, IBAN_CODE.
Only propose clear PII. Do not invent offsets.
"""

_LOCAL_LLM_PROVIDERS = frozenset({
    "ollama", "vllm", "sglang", "sglang-qwen", "slang", "sg-lang",
    "local", "llama_cpp", "llamacpp", "lmstudio", "demo",
})
_CLOUD_LLM_PROVIDERS = frozenset({
    "openai", "azure", "azure_openai", "gemini", "google", "google_genai",
    "anthropic", "bedrock", "vertex", "mistral", "cohere", "groq",
})


def _system_prompt(config: TextScanConfig) -> str:
    arabic = bool(getattr(config, "arabic", False)) or (config.language or "").startswith("ar")
    return _SYSTEM_AR if arabic else _SYSTEM_EN


class LlmTextRefiner:
    """Propose additional spans via guarded_model_call (local providers only by default)."""

    def __init__(
        self,
        *,
        redibis_config: Any = None,
        provider: Any = None,
        allow_cloud: bool = False,
    ):
        self._cfg = redibis_config
        self._provider = provider
        self._allow_cloud = allow_cloud

    def propose_spans(
        self,
        text: str,
        existing: list[Candidate],
        config: TextScanConfig,
    ) -> list[Candidate]:
        llm_cfg = getattr(getattr(self._cfg, "pii", None), "llm", None)
        if self._provider is None:
            enabled = bool(llm_cfg and getattr(llm_cfg, "enabled", False))
            if not enabled:
                # Probe capability role — if unbound, skip quietly.
                try:
                    self._resolve_from_role()
                except Exception:
                    return []

        system = _system_prompt(config)
        # Cap prompt size
        sample = text if len(text) <= 4000 else text[:4000]
        prompt = (
            f"Text:\n{sample}\n\n"
            f"Existing engines found {len(existing)} candidate(s). "
            "Propose any missed PII spans (spoken Arabic digits, obfuscated emails, "
            "names, addresses, telecom IDs)."
        )
        try:
            raw = self._call_model(prompt, system=system)
        except Exception as exc:
            logger.warning("LLM text call failed: %s", exc)
            raise

        spans = self._parse_spans(raw)
        out: list[Candidate] = []
        for item in spans:
            try:
                start = int(item["start"])
                end = int(item["end"])
                et = str(item.get("entity_type") or "").upper().replace(" ", "_")
                score = float(item.get("score") or 0.5)
            except (KeyError, TypeError, ValueError):
                continue
            if not et or start < 0 or end > len(text) or start >= end:
                continue
            # Source-slice validation — reject hallucinated offsets
            slice_text = text[start:end]
            if not slice_text.strip():
                continue
            if config.entities and et not in config.entities:
                continue
            out.append(Candidate(
                entity_type=et,
                score=min(1.0, max(0.0, score)),
                engine="llm",
                start=start,
                end=end,
                text=slice_text,
                recognizer="llm",
                is_proposal=True,
            ))
        return out

    def _external_raw_text_allowed(self) -> bool:
        """True when this instance or ``pii.llm.allow_external_raw_text`` opts in."""
        if self._allow_cloud:
            return True
        llm = getattr(getattr(self._cfg, "pii", None), "llm", None) if self._cfg else None
        return bool(getattr(llm, "allow_external_raw_text", False))

    def _assert_local_provider(self, provider_name: str) -> None:
        key = (provider_name or "").strip().lower()
        if self._external_raw_text_allowed():
            return
        if key in _CLOUD_LLM_PROVIDERS:
            raise RuntimeError(
                f"cloud LLM provider {provider_name!r} is not allowed on the free-text "
                "PII path by default; use a local provider (sglang/vllm/ollama) or set "
                "pii.llm.allow_external_raw_text: true (RAI residency policy still "
                "applies to the call)"
            )
        if key and key not in _LOCAL_LLM_PROVIDERS and key not in _CLOUD_LLM_PROVIDERS:
            # Unknown — only allow if endpoint looks local
            llm = getattr(getattr(self._cfg, "pii", None), "llm", None) if self._cfg else None
            endpoint = str(getattr(llm, "endpoint_url", "") or "")
            localish = any(
                h in endpoint for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1")
            )
            if not localish and endpoint.startswith("https://"):
                raise RuntimeError(
                    f"refusing non-local LLM endpoint for text PII: {endpoint!r}"
                )

    def _resolve_from_role(self) -> tuple[Any, str]:
        from redibis.config import load_global_settings_optional
        from redibis.enrich.capability_routing import get_provider_for_role

        gs = load_global_settings_optional()
        agents_cfg = getattr(self._cfg, "agents", None) if self._cfg else None
        llm = getattr(getattr(self._cfg, "pii", None), "llm", None) if self._cfg else None
        provider, binding = get_provider_for_role(
            "pii.text_refiner",
            gs=gs,
            agents_cfg=agents_cfg,
            api_key=(getattr(llm, "api_key", None) or None) if llm else None,
            endpoint_url=(getattr(llm, "endpoint_url", None) or None) if llm else None,
        )
        self._assert_local_provider(binding.provider)
        model_id = binding.model or getattr(provider, "model", "") or binding.provider
        return provider, model_id

    def _resolve_provider(self) -> tuple[Any, str]:
        """Resolve the actual provider object that will handle the call, plus
        a display ``model_id``. Prefer request override, then capability role
        ``pii.text_refiner``, then legacy ``pii.llm``.
        """
        if self._provider is not None:
            model_id = (
                getattr(self._provider, "model", "")
                or getattr(self._provider, "litellm_model", "")
                or getattr(self._provider, "name", "")
                or "pii-text-llm"
            )
            return self._provider, model_id

        # 1) Capability role (Settings → Text Gateway Models / LLM roles)
        try:
            return self._resolve_from_role()
        except Exception as exc:
            logger.debug("pii.text_refiner role unavailable: %s", exc)

        # 2) Legacy pii.llm block
        from redibis.enrich.providers import get_provider

        llm = getattr(self._cfg.pii, "llm", None) if self._cfg else None
        provider_name = getattr(llm, "provider", "sglang") if llm else "sglang"
        self._assert_local_provider(provider_name)
        model = getattr(llm, "model_name", "") if llm else ""
        endpoint = getattr(llm, "endpoint_url", None) if llm else None
        prov = get_provider(provider_name, model=model, endpoint_url=endpoint)
        model_id = model or provider_name or "pii-text-llm"
        return prov, model_id

    def _call_model(self, prompt: str, *, system: str = "") -> str:
        from redibis.telemetry.model_gateway import guarded_model_call

        provider, model_id = self._resolve_provider()
        system_prompt = system or _SYSTEM_EN

        def _invoke():
            if hasattr(provider, "complete"):
                return provider.complete(system_prompt, prompt)
            if callable(provider):
                return provider(prompt)
            raise TypeError(f"provider {provider!r} is not callable and has no complete()")

        result, _report = guarded_model_call(
            _invoke,
            model_id=model_id,
            provider=provider,
            redibis_config=self._cfg,
            user_prompt=prompt,
            system_prompt=system_prompt,
            attested_masked_external=False,
            model_role="pii.text_refiner",
        )
        if isinstance(result, dict):
            return str(result.get("content") or result.get("text") or "")
        return str(result or "")

    @staticmethod
    def _parse_spans(raw: str) -> list[dict]:
        text = (raw or "").strip()
        if not text:
            return []
        # Extract JSON object from fences if present
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        spans = data.get("spans") if isinstance(data, dict) else None
        return list(spans) if isinstance(spans, list) else []
