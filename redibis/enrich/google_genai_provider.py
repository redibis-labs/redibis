"""Google Gen AI SDK enrichment provider (``google-genai`` / Vertex + Developer API).

Phase 4 codegen modernization: prefer this over LiteLLM's gemini path for new
work. Instantiated only via ``get_provider`` when ``kind: google_genai``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from redibis.enrich.providers import EnrichmentError, EnrichmentProvider


@dataclass
class GoogleGenAIProvider(EnrichmentProvider):
    """Calls Gemini through the unified Google Gen AI SDK (not deprecated vertexai.*)."""

    name: str = "google_genai"
    default_model: str = "gemini-2.5-flash"
    project: str = ""
    location: str = "us-central1"
    use_vertex: bool = False
    supports_json: bool = True
    residency: str = "gcp"
    extra: dict = field(default_factory=dict)

    def _effective_model(self) -> str:
        bare = (self.model or self.default_model or "").strip()
        # Strip accidental litellm-style gemini/ prefix
        if bare.startswith("gemini/"):
            bare = bare.split("/", 1)[1]
        return bare or "gemini-2.5-flash"

    def _build_client(self):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - env dependent
            raise EnrichmentError(
                "google-genai is required for the google_genai provider. "
                "Install it with: pip install google-genai"
            ) from exc

        project = (
            self.project
            or os.environ.get("VERTEX_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        location = (
            self.location
            or os.environ.get("VERTEX_LOCATION")
            or "us-central1"
        ).strip()
        use_vertex = self.use_vertex or bool(project)

        if use_vertex and project:
            return genai.Client(vertexai=True, project=project, location=location)

        api_key = self.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise EnrichmentError(
                "google_genai provider needs VERTEX_PROJECT / GOOGLE_CLOUD_PROJECT "
                "for Vertex, or GEMINI_API_KEY / GOOGLE_API_KEY for the Developer API."
            )
        return genai.Client(api_key=api_key)

    def complete(self, system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> str:
        from redibis.enrich.llm_logging import (
            build_model_call_record,
            emit_llm_log,
            is_llm_debug_enabled,
            prompt_meta,
            record_llm_call,
            redact,
        )

        model = self._effective_model()
        client = self._build_client()

        try:
            from google.genai import types
        except ImportError:  # pragma: no cover
            types = None  # type: ignore

        config_kwargs: dict[str, Any] = {}
        params = dict(self.extra or {})
        if "temperature" in params:
            config_kwargs["temperature"] = params["temperature"]
        if "max_tokens" in params:
            config_kwargs["max_output_tokens"] = params["max_tokens"]
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt
        if json_mode and self.supports_json:
            config_kwargs["response_mime_type"] = "application/json"

        config = None
        if types is not None and config_kwargs:
            config = types.GenerateContentConfig(**config_kwargs)

        started_debug = is_llm_debug_enabled()
        try:
            if config is not None:
                resp = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=config,
                )
            else:
                resp = client.models.generate_content(model=model, contents=user_prompt)
        except Exception as exc:
            raise EnrichmentError(f"google-genai call failed: {exc}") from exc

        text = getattr(resp, "text", None) or ""
        if not text and getattr(resp, "candidates", None):
            # Fallback extraction for SDK variants
            try:
                parts = resp.candidates[0].content.parts
                text = "".join(getattr(p, "text", "") or "" for p in parts)
            except Exception:
                text = ""

        if started_debug:
            try:
                record = build_model_call_record(
                    provider=self.name,
                    model_id=model,
                    system_prompt=redact(system_prompt),
                    user_prompt=redact(user_prompt),
                    response_text=redact(text),
                    meta=prompt_meta(system_prompt, user_prompt),
                )
                emit_llm_log(record)
                record_llm_call(record)
            except Exception:
                pass

        return (text or "").strip()
