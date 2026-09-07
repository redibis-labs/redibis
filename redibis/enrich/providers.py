"""
redibis.enrich.providers — one LiteLLM-backed adapter, configured by JSON.

Every provider (local vLLM, local Ollama, OpenRouter, Claude, Gemini, OpenAI,
…) is called through **LiteLLM**, so the request/response shape is identical no
matter which vendor is selected. Providers are defined declaratively in a JSON
file — add or change a provider without writing code.

Provider resolution order (first match wins):
    1. ``REDIBIS_LLM_PROVIDERS`` env var → path to a JSON file
    2. ``./llm_providers.json`` in the current working directory
    3. the packaged ``providers_default.json`` (ships with redibis)

JSON shape::

    {
      "providers": {
        "<name>": {
          "litellm_model": "openai/gpt-4o",   # default model (LiteLLM prefix incl.)
          "model_prefix":  "openai",            # prefix applied to a bare --model
          "api_base":      "http://...",        # self-hosted/local endpoint
          "api_key_env":   "OPENAI_API_KEY",    # env var holding the key (preferred)
          # "api_key" is rejected on save — never persist literal secrets
          "supports_json": true,                # request a JSON response_format
          "params": {"temperature": 0.2}        # default completion kwargs
        }
      }
    }

The public surface (``EnrichmentProvider`` base, ``get_provider``) is unchanged
so the rest of the codebase and tests keep working.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


class EnrichmentError(RuntimeError):
    """Raised when a provider request fails."""


_MODEL_ALIASES: dict[str, str] = {
    "claude opus 4.8": "claude-opus-4-8",
    "claude opus 4.7": "claude-opus-4-7",
    "claude opus 4.6": "claude-opus-4-6",
    "claude opus 4.5": "claude-opus-4-5",
    "claude sonnet 4.6": "claude-sonnet-4-6",
    "claude sonnet 4.5": "claude-sonnet-4-5",
    "claude haiku 4.5": "claude-haiku-4-5",
    "gpt 4o": "gpt-4o",
    "gpt 4o mini": "gpt-4o-mini",
}

# Anthropic Claude 4.7+ rejects temperature / top_p / top_k in the request body.
_SAMPLING_PARAM_KEYS = ("temperature", "top_p", "top_k")
_NO_SAMPLING_MODEL_MARKERS = ("-4-7", "-4-8")


def forbids_sampling_params(model: str) -> bool:
    """True when the provider model id must not receive sampling kwargs."""
    bare = (model or "").lower().split("/")[-1]
    return any(marker in bare for marker in _NO_SAMPLING_MODEL_MARKERS)


def _strip_sampling_params(kwargs: dict, model: str) -> None:
    if forbids_sampling_params(model):
        for key in _SAMPLING_PARAM_KEYS:
            kwargs.pop(key, None)


def _looks_like_api_key(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate or "://" in candidate or "/" in candidate:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{20,}", candidate))


def _validated_api_base(api_base: Optional[str], *, provider_name: str = "") -> Optional[str]:
    candidate = (api_base or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate

    provider = (provider_name or "provider").strip() or "provider"
    msg = (
        f"{provider!r} has an invalid api_base configuration. "
        "api_base must be a full http:// or https:// URL."
    )
    if provider.lower() in {"gemini", "claude", "openai", "openrouter"}:
        msg += " For hosted providers, leave api_base empty unless you are using a custom gateway."
    if _looks_like_api_key(candidate):
        msg += " The current value looks like an API key, not a URL; put it in the provider API key field or the matching *_API_KEY environment variable instead."
    raise EnrichmentError(msg)


def _normalize_endpoint_and_key(
    endpoint_url: Optional[str],
    api_key: Optional[str],
    *,
    registry_api_base: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Treat key-like endpoint/api_base values as api_key; keep real http(s) URLs."""
    key = (api_key or "").strip() or None
    ep_override = (endpoint_url or "").strip()
    reg_base = (registry_api_base or "").strip()

    if ep_override:
        if _looks_like_api_key(ep_override):
            if not key:
                key = ep_override
        else:
            return ep_override, key

    if reg_base:
        if _looks_like_api_key(reg_base):
            if not key:
                key = reg_base
        else:
            return reg_base, key

    return None, key


_LOCAL_OPENAI_COMPAT = frozenset({
    "sglang", "slang", "sg-lang", "vllm", "hosted_vllm", "ollama",
    "lmstudio", "llama_cpp", "llamacpp", "local",
})


def _local_openai_compat_needs_placeholder(
    *,
    provider_name: str,
    residency: str,
    api_base: Optional[str],
    litellm_model: str = "",
) -> bool:
    """True when LiteLLM's openai/ prefix would reject a missing api_key.

    Local OpenAI-compatible servers (SGLang, vLLM, …) usually ignore the key but
    LiteLLM still requires one for ``openai/…`` models. A placeholder is fine.
    """
    name = (provider_name or "").strip().lower()
    if name in _LOCAL_OPENAI_COMPAT or name.startswith("sglang"):
        return True
    base = (api_base or "").strip().lower()
    local_host = any(
        h in base for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")
    )
    if (residency or "").strip().lower() == "local" and (local_host or base.startswith("http://")):
        return True
    # Any openai/ model pointed at a local/private api_base (custom profile names)
    model = (litellm_model or "").strip().lower()
    if model.startswith("openai/") and base and (local_host or base.startswith("http://")):
        return True
    return False


def normalize_model_id(model: str, *, model_prefix: str = "") -> str:
    """
    Normalize a user-supplied model override to a provider API id.

    Accepts display names like ``Claude Opus 4.8`` and converts them to
    ``claude-opus-4-8``. Bare API ids and prefixed LiteLLM ids are preserved.
    """
    m = (model or "").strip()
    if not m:
        return m

    # ``anthropic/claude-opus-4-8`` → ``claude-opus-4-8``
    if "/" in m:
        prefix, bare = m.split("/", 1)
        if not model_prefix or prefix == model_prefix:
            m = bare.strip()

    alias_key = re.sub(r"[\s_]+", " ", m.lower()).strip()
    if alias_key in _MODEL_ALIASES:
        return _MODEL_ALIASES[alias_key]

    # Already a bare API id (no spaces) — keep casing for non-Claude models.
    if " " not in m and re.fullmatch(r"[\w.@:-]+", m):
        return m.lower() if m.lower().startswith("claude-") else m

    # Slugify display names: ``Claude Opus 4.8`` → ``claude-opus-4-8``
    slug = re.sub(r"[^a-z0-9]+", "-", m.lower()).strip("-")
    return slug or m


# ─────────────────────────────────────────────────────────────────────────────
# Adapter interface (kept stable for the rest of the codebase + tests)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EnrichmentProvider:
    """Base adapter. Subclasses implement ``complete``."""
    model: str = ""
    api_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    residency: str = ""
    extra: dict = field(default_factory=dict)

    name: str = "base"

    def complete(self, system_prompt: str, user_prompt: str, *,
                 json_mode: bool = True) -> str:
        raise NotImplementedError

    # Shared helper: parse a JSON object out of a possibly-fenced response.
    @staticmethod
    def parse_json(text: str) -> dict:
        text = (text or "").strip()
        if text.startswith("```"):
            # strip ```json ... ``` fences
            text = text.split("```", 2)[1] if text.count("```") >= 2 else text
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
        text = text.strip().strip("`").strip()
        # find the outermost JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
        return json.loads(text)


# ─────────────────────────────────────────────────────────────────────────────
# The single LiteLLM-backed provider
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LiteLLMProvider(EnrichmentProvider):
    """
    Calls any LiteLLM-supported backend with one identical code path.

    The vendor is encoded entirely in ``litellm_model`` / ``model_prefix`` and
    ``api_base`` — there is no per-vendor branching.
    """
    name: str = "litellm"
    litellm_model: str = ""
    model_prefix: str = ""
    api_base: Optional[str] = None
    residency: str = ""
    supports_json: bool = True
    supports_json_schema: bool = False

    def _effective_model(self) -> str:
        """Resolve the LiteLLM model string from the config + optional override."""
        if not self.model:
            return self.litellm_model
        bare = normalize_model_id(self.model, model_prefix=self.model_prefix)
        if self.model_prefix and not bare.startswith(self.model_prefix + "/"):
            return f"{self.model_prefix}/{bare}"
        return bare

    def complete(self, system_prompt: str, user_prompt: str, *,
                 json_mode: bool = True) -> str:
        from redibis.enrich.llm_logging import (
            build_model_call_record,
            emit_llm_log,
            is_llm_debug_enabled,
            prompt_meta,
            record_llm_call,
            redact,
        )

        try:
            import litellm
        except ImportError as e:  # pragma: no cover - env dependent
            raise EnrichmentError(
                "litellm is required for LLM enrichment. "
                "Install it with: pip install 'redibis[enrich]'"
            ) from e

        model = self._effective_model()
        if not model:
            raise EnrichmentError(
                f"Provider {self.name!r} has no model. Set 'litellm_model' in "
                f"your llm_providers.json or pass --model."
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs = dict(self.extra or {})
        api_base, resolved_key = _normalize_endpoint_and_key(
            self.endpoint_url,
            self.api_key,
            registry_api_base=self.api_base,
        )
        api_base = _validated_api_base(api_base, provider_name=self.name)
        if api_base:
            kwargs.setdefault("api_base", api_base)
        effective_key = resolved_key or self.api_key
        if not effective_key and _local_openai_compat_needs_placeholder(
            provider_name=self.name,
            residency=str(getattr(self, "residency", "") or ""),
            api_base=api_base or self.api_base,
            litellm_model=model,
        ):
            # LiteLLM OpenAI client requires a non-empty key even for keyless
            # local servers (SGLang / vLLM openai-compat).
            effective_key = "EMPTY"
        if effective_key:
            kwargs.setdefault("api_key", effective_key)
        if json_mode and self.supports_json:
            if "response_format" not in kwargs:
                if getattr(self, "supports_json_schema", False):
                    try:
                        from redibis.enrich.delta_schema import enrichment_delta_json_schema

                        kwargs["response_format"] = {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "enrichment_delta",
                                "schema": enrichment_delta_json_schema(),
                                "strict": False,
                            },
                        }
                    except Exception:
                        kwargs["response_format"] = {"type": "json_object"}
                else:
                    kwargs["response_format"] = {"type": "json_object"}
        kwargs.setdefault("timeout", kwargs.get("timeout", 60))
        _strip_sampling_params(kwargs, model)

        pmeta = prompt_meta(system_prompt, user_prompt)
        resp = None
        status = "ok"
        err = ""
        _secrets = [self.api_key or ""]
        litellm_logger = logging.getLogger("LiteLLM")
        prev_litellm_level = litellm_logger.level
        debug_on = is_llm_debug_enabled()
        if debug_on:
            litellm_logger.setLevel(logging.DEBUG)

        t0 = time.perf_counter()
        try:
            resp = litellm.completion(model=model, messages=messages, **kwargs)
        except Exception as e:  # litellm raises many provider-specific types
            status = "error"
            from redibis.enrich.provider_profiles import format_provider_error

            detail = format_provider_error(e)
            err = detail
            msg = f"LiteLLM completion failed for model {model!r}: {detail}"
            err_text = f"{type(e).__name__} {e}".lower()
            if "notfound" in err_text or "not_found" in err_text:
                msg += (
                    ". That model id was not found on the provider. Use the exact served "
                    "id from GET {api_base}/models (e.g. Qwen/Qwen2.5-14B-Instruct), "
                    "not a display name, or leave Model empty to use the provider default."
                )
            elif "temperature" in err_text and "deprecated" in err_text:
                msg += (
                    ". Claude Opus 4.7/4.8 do not accept temperature, top_p, or top_k. "
                    "Remove them from your llm_providers.json params for this model."
                )
            elif "response_format" in err_text or "json_object" in err_text:
                msg += (
                    ". This server may reject response_format=json_object. "
                    "Set supports_json=false on the provider profile and retry."
                )
            raise EnrichmentError(redact(msg, secrets=_secrets)) from e
        finally:
            if debug_on:
                litellm_logger.setLevel(prev_litellm_level)
            latency_ms = (time.perf_counter() - t0) * 1000
            rec = build_model_call_record(
                self,
                model,
                resp if status == "ok" else None,
                latency_ms=latency_ms,
                status=status,
                error=err,
                api_base=api_base,
                system_prompt_len=pmeta["system_prompt_len"],
                user_prompt_len=pmeta["user_prompt_len"],
                error_secrets=_secrets,
            )
            record_llm_call(rec)
            emit_llm_log(rec, error=redact(err, secrets=_secrets))

        try:
            return resp.choices[0].message.content or ""
        except Exception:  # fall back to dict-style access
            return resp["choices"][0]["message"]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Offline demo provider (no network, no LiteLLM — open-source capability demo)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_enrich_prompt_doc(user_prompt: str) -> dict:
    """Best-effort YAML payload from the enrichment user prompt."""
    import yaml

    for marker in ("# COLUMN EVIDENCE", "# ACTIVE CONTRACT"):
        idx = user_prompt.find(marker)
        if idx >= 0:
            break
    else:
        idx = -1
    chunk = user_prompt[idx + len(marker):] if idx >= 0 else user_prompt
    yaml_lines: list[str] = []
    started = False
    for line in chunk.splitlines():
        if line.startswith("# ") and yaml_lines:
            break
        stripped = line.strip()
        if not started:
            if stripped.startswith((
                "apiVersion:", "kind:", "schema:", "table:", "columns:",
            )):
                started = True
            else:
                continue
        yaml_lines.append(line)
    try:
        doc = yaml.safe_load("\n".join(yaml_lines)) or {}
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def _extract_columns_from_enrich_prompt(user_prompt: str) -> list[str]:
    """Best-effort column list from the enrichment user prompt YAML block."""
    doc = _parse_enrich_prompt_doc(user_prompt)
    cols: list[str] = []
    if isinstance(doc.get("columns"), list):
        for item in doc["columns"]:
            if isinstance(item, dict) and item.get("name"):
                cols.append(str(item["name"]))
        if cols:
            return cols
    for schema in doc.get("schema") or []:
        for prop in schema.get("properties") or []:
            if isinstance(prop, dict) and prop.get("name"):
                cols.append(str(prop["name"]))
    return cols


def _extract_table_from_enrich_prompt(user_prompt: str) -> str:
    """Best-effort table name from the enrichment user prompt YAML block."""
    doc = _parse_enrich_prompt_doc(user_prompt)
    table = doc.get("table") or doc.get("physicalName") or ""
    if isinstance(table, dict):
        table = table.get("name") or table.get("physicalName") or ""
    return str(table).strip()


@dataclass
class DemoEnrichmentProvider(EnrichmentProvider):
    """Template-based enrichment for offline demos — no LLM call."""

    name: str = "demo"
    residency: str = "local"

    def complete(self, system_prompt: str, user_prompt: str, *,
                 json_mode: bool = True) -> str:
        from redibis.enrich.llm_logging import (
            build_model_call_record,
            emit_llm_log,
            prompt_meta,
            record_llm_call,
        )

        pmeta = prompt_meta(system_prompt, user_prompt)
        t0 = time.perf_counter()
        columns = _extract_columns_from_enrich_prompt(user_prompt)
        if not columns:
            columns = ["column_a", "column_b"]
        table_name = _extract_table_from_enrich_prompt(user_prompt) or "this table"
        delta: dict = {
            "table": {
                "description": (
                    f"Demo table definition for {table_name}: one row per record in this "
                    f"dataset. Generated offline by the redibis demo provider."
                ),
                "purpose": "demo enrichment",
            },
            "table_tags": ["demo", "redibis"],
            "columns": {},
        }
        for col in columns:
            label = col.replace("_", " ").replace("-", " ").strip().title()
            delta["columns"][col] = {
                "businessName": label,
                "business": {
                    "definition": (
                        f"Demo business definition for «{col}» — generated offline by the "
                        f"redibis demo provider. Replace with LLM enrichment (ollama/vllm) "
                        f"or edit manually on the Definitions page."
                    ),
                    "synonyms": [col, label.lower()],
                    "example_values": ["demo-value-1", "demo-value-2"],
                    "tags": ["demo"],
                },
            }
        out = json.dumps(delta)
        latency_ms = (time.perf_counter() - t0) * 1000
        rec = build_model_call_record(
            self,
            "offline-demo",
            None,
            latency_ms=latency_ms,
            status="ok",
            system_prompt_len=pmeta["system_prompt_len"],
            user_prompt_len=pmeta["user_prompt_len"],
        )
        record_llm_call(rec)
        emit_llm_log(rec)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# JSON-driven provider registry
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_FILE = Path(__file__).with_name("providers_default.json")
_ENV_VAR = "REDIBIS_LLM_PROVIDERS"
_CWD_FILE = "llm_providers.json"


def _config_path(explicit: Optional[str] = None) -> Path:
    """Resolve which JSON file defines the providers."""
    if explicit:
        return Path(explicit)
    env = os.getenv(_ENV_VAR)
    if env:
        return Path(env)
    cwd = Path.cwd() / _CWD_FILE
    if cwd.exists():
        return cwd
    return _DEFAULT_FILE


def load_provider_configs(config_path: Optional[str] = None) -> dict:
    """Load the {name: config} mapping from the active JSON file.

    The packaged defaults are always available as a base layer; a user file
    (env var or ./llm_providers.json) overrides/extends entries by name.
    """
    base = _read_providers(_DEFAULT_FILE)
    path = _config_path(config_path)
    if path != _DEFAULT_FILE and path.exists():
        base.update(_read_providers(path))
    return base


def _read_providers(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        raise EnrichmentError(f"Failed to read provider config {path}: {e}") from e
    providers = data.get("providers", data) if isinstance(data, dict) else {}
    # Drop comment/meta keys and non-dict entries.
    return {k: v for k, v in providers.items()
            if isinstance(v, dict) and not k.startswith("_")}


def bare_litellm_model(litellm_model: str, *, model_prefix: str = "") -> str:
    """Strip LiteLLM provider prefix for UI / session overrides."""
    m = (litellm_model or "").strip()
    prefix = (model_prefix or "").strip()
    if prefix and m.startswith(prefix + "/"):
        return m[len(prefix) + 1 :]
    if "/" in m:
        return m.split("/", 1)[1]
    return m


def list_providers(config_path: Optional[str] = None) -> list[dict]:
    """Lightweight listing for UIs/CLIs: name + description + default model.

    Delegates to ``provider_profiles.list_profiles`` when available so Enrich UI
    gets ``source`` / ``editable`` / ``supports_json`` without a second call.
    """
    try:
        from redibis.enrich.provider_profiles import list_profiles
        return list_profiles(config_path)
    except Exception:
        pass
    cfgs = load_provider_configs(config_path)
    out = []
    for name, cfg in sorted(cfgs.items()):
        kind = cfg.get("kind", "")
        api_key_env = cfg.get("api_key_env") or ""
        litellm_model = cfg.get("litellm_model", "") or ("offline" if kind == "demo" else "")
        model_prefix = cfg.get("model_prefix") or ""
        known_models = list(cfg.get("known_models") or [])
        default_bare = bare_litellm_model(litellm_model, model_prefix=model_prefix)
        if default_bare and default_bare not in known_models:
            known_models.insert(0, default_bare)
        out.append({
            "name": name,
            "description": cfg.get("description", ""),
            "model": litellm_model,
            "model_prefix": model_prefix,
            "default_model_bare": default_bare,
            "known_models": known_models,
            "needs_key": False if kind == "demo" else bool(cfg.get("api_key_env") or cfg.get("api_key")),
            "api_key_env": api_key_env,
            "api_key_env_set": bool(
            api_key_env and os.getenv(api_key_env)
        ) or (
            name == "gemini" and bool(os.getenv("GOOGLE_API_KEY"))
        ),
            "api_key_saved": bool(cfg.get("api_key")),
            "api_base": cfg.get("api_base"),
            "residency": cfg.get("residency") or "",
            "kind": kind or "litellm",
            "offline": kind == "demo",
            "cloud": name in ("claude", "gemini", "openai", "openrouter", "google_genai"),
            "supports_json": bool(cfg.get("supports_json", True)) if kind != "demo" else False,
            "editable": kind == "custom",
            "source": "custom" if kind == "custom" else "packaged",
        })
    order = {
        "google_genai": 0, "gemini": 1, "ollama": 2, "vllm": 3, "sglang": 4,
        "openai": 5, "claude": 6, "openrouter": 7, "demo": 99,
    }
    out.sort(key=lambda row: (order.get(row["name"], 50), row["name"]))
    return out


def _resolve_key(cfg: dict, override: Optional[str], *, provider_name: str = "") -> Optional[str]:
    if override:
        return override
    if cfg.get("api_key"):
        return cfg["api_key"]
    env_name = cfg.get("api_key_env")
    if env_name:
        val = os.getenv(env_name)
        if val:
            return val
    # LiteLLM accepts GOOGLE_API_KEY interchangeably for Gemini.
    if (provider_name or "").lower() == "gemini":
        return os.getenv("GOOGLE_API_KEY")
    return None


_PROVIDER_ALIASES = {
    "slang": "sglang",
    "sg-lang": "sglang",
    "google-genai": "google_genai",
    "genai": "google_genai",
}


def get_provider(name: str, *, model: str = "", api_key: Optional[str] = None,
                 endpoint_url: Optional[str] = None,
                 config_path: Optional[str] = None, **extra) -> EnrichmentProvider:
    """Build a provider from the JSON registry by name."""
    cfgs = load_provider_configs(config_path)
    key = _PROVIDER_ALIASES.get((name or "").lower(), (name or "").lower())
    cfg = cfgs.get(key)
    if cfg is None:
        raise ValueError(
            f"Unknown enrichment provider {name!r}. Defined: {sorted(cfgs)}. "
            f"Add it to your llm_providers.json (or set {_ENV_VAR})."
        )
    if cfg.get("kind") == "demo" or key == "demo":
        return DemoEnrichmentProvider(name=key)

    _reserved = {"model", "messages", "api_base", "api_key", "response_format"}
    params = {
        k: v for k, v in dict(cfg.get("params") or {}).items()
        if k not in _reserved
    }
    params.update({k: v for k, v in (extra or {}).items() if k not in _reserved})
    api_base, resolved_key = _normalize_endpoint_and_key(
        endpoint_url, api_key, registry_api_base=cfg.get("api_base"),
    )

    kind = (cfg.get("kind") or "").strip().lower()
    if kind in ("google_genai", "google-genai", "genai") or key in ("google_genai", "google-genai"):
        from redibis.enrich.google_genai_provider import GoogleGenAIProvider

        return GoogleGenAIProvider(
            name=key,
            model=model or "",
            default_model=str(
                cfg.get("default_model")
                or bare_litellm_model(cfg.get("litellm_model") or cfg.get("model") or "gemini-2.5-flash")
                or "gemini-2.5-flash"
            ),
            api_key=_resolve_key(cfg, resolved_key, provider_name="gemini"),
            project=str(cfg.get("project") or ""),
            location=str(cfg.get("location") or cfg.get("vertex_location") or "us-central1"),
            use_vertex=bool(cfg.get("use_vertex", False)),
            supports_json=bool(cfg.get("supports_json", True)),
            residency=str(cfg.get("residency") or "gcp"),
            extra=params,
        )

    return LiteLLMProvider(
        name=key,
        model=model or "",
        litellm_model=cfg.get("litellm_model") or cfg.get("model") or "",
        model_prefix=cfg.get("model_prefix", ""),
        api_base=api_base,
        api_key=_resolve_key(cfg, resolved_key, provider_name=key),
        supports_json=bool(cfg.get("supports_json", True)),
        supports_json_schema=bool(cfg.get("supports_json_schema", False)),
        residency=str(cfg.get("residency") or ""),
        extra=params,
    )


# Back-compat introspection symbol (names defined by the packaged defaults).
PROVIDERS = _read_providers(_DEFAULT_FILE)


def user_provider_config_path() -> Path:
    """Writable path for user provider overrides (never the packaged default file)."""
    env = os.getenv(_ENV_VAR)
    if env:
        return Path(env)
    return Path.cwd() / _CWD_FILE


def get_provider_config(name: str, config_path: Optional[str] = None) -> dict:
    """Return one provider entry for editing (no literal api_key)."""
    cfgs = load_provider_configs(config_path)
    key = (name or "").lower()
    cfg = cfgs.get(key)
    if cfg is None:
        raise ValueError(f"Unknown provider {name!r}")
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    out["api_key_saved"] = bool(cfg.get("api_key"))
    out["name"] = key
    return out


def save_user_provider_configs(providers: dict, *, config_path: Optional[str] = None) -> str:
    """Persist user provider registry to JSON.

    Literal ``api_key`` values are rejected. Use ``api_key_env`` / credential
    references instead. Existing on-disk keys are stripped on rewrite.
    """
    path = Path(config_path) if config_path else user_provider_config_path()
    clean: dict[str, dict] = {}
    for name, cfg in (providers or {}).items():
        if not isinstance(cfg, dict) or str(name).startswith("_"):
            continue
        key = str(name).lower()
        if cfg.get("api_key"):
            raise EnrichmentError(
                f"Provider {key!r}: literal api_key is not allowed; "
                "set api_key_env (or credential_ref) to an environment variable name"
            )
        entry = {k: v for k, v in cfg.items() if k not in {"api_key", "api_key_saved"}}
        raw_base = entry.get("api_base")
        if raw_base is not None and str(raw_base).strip():
            entry["api_base"] = _validated_api_base(
                str(raw_base), provider_name=key
            )
        elif "api_base" in entry:
            entry.pop("api_base")
        clean[key] = entry
    payload = {"providers": clean}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return str(path.resolve())

