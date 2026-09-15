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
OTP, PASSPORT, IBAN_CODE, SIM_PUK, VOUCHER, SUPPORT_TICKET.
Do not flag role labels (Agent, Caller) or quantity+unit phrases (30 GB, 100 جنيه).
When an address cue is present, span the whole address clause, not one keyword.
You are also shown spans the deterministic engines already found. For each,
reply in "review" with: {"start":N,"end":M,"verdict":"PII"|"NOT_PII"|"UNSURE",
"entity_type":"TYPE_OR_UNKNOWN","confidence":0.0-1.0,"reason":"short"}.
Use NOT_PII only when you are confident the span is not personal data (a
quantity with a unit, a product code, a role label). Use UNSURE when unsure.
Omitting a span means UNSURE, not NOT_PII.
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
PASSPORT, IBAN_CODE, SIM_PUK, VOUCHER, SUPPORT_TICKET.
Do not flag role labels (Agent, Caller) or quantity+unit phrases (30 GB, 100 جنيه).
When an address cue such as العنوان is present, span the whole address clause.
Only propose clear PII. Do not invent offsets.
You are also shown spans the deterministic engines already found. For each,
reply in "review" with: {"start":N,"end":M,"verdict":"PII"|"NOT_PII"|"UNSURE",
"entity_type":"TYPE_OR_UNKNOWN","confidence":0.0-1.0,"reason":"short"}.
Use NOT_PII only when you are confident the span is not personal data (a
quantity with a unit, a product code, a role label). Use UNSURE when unsure.
Omitting a span means UNSURE, not NOT_PII.
"""

_LOCAL_LLM_PROVIDERS = frozenset({
    "ollama", "vllm", "sglang", "sglang-qwen", "slang", "sg-lang",
    "local", "llama_cpp", "llamacpp", "lmstudio", "demo",
})
_CLOUD_LLM_PROVIDERS = frozenset({
    "openai", "azure", "azure_openai", "gemini", "google", "google_genai",
    "anthropic", "bedrock", "vertex", "mistral", "cohere", "groq",
})


def _overlay_prompt(text_rules: object | None) -> str:
    if text_rules is None:
        return ""
    to_dict = getattr(text_rules, "to_dict", None)
    data = to_dict() if callable(to_dict) else {}
    if not isinstance(data, dict) or not data:
        return ""
    lines = ["Operator cues (use these; take the whole clause when extend=sentence):"]
    cues = data.get("context_cues") or {}
    if isinstance(cues, dict):
        for et, cue in list(cues.items())[:12]:
            if isinstance(cue, dict):
                trigs = ", ".join(str(t) for t in (cue.get("triggers") or [])[:8])
                extend = cue.get("extend") or ""
            else:
                trigs = str(cue)
                extend = ""
            extra = f" extend={extend}" if extend else ""
            lines.append(f"- {et}: {trigs}{extra}")
    excludes = data.get("exclude_terms") or []
    if excludes:
        lines.append("Never flag these surfaces: " + ", ".join(str(x) for x in excludes[:24]))
    noise = data.get("noise_terms") or []
    if noise:
        shown = [str(x) for x in noise[:40]]
        lines.append(
            "Ignore these filler words entirely; they are never part of a value: "
            + ", ".join(shown)
        )
    units = data.get("quantity_units") or []
    if units:
        lines.append("Numbers next to these units are not PII: " + ", ".join(str(x) for x in units[:16]))
    lines.append(
        "You are also shown spans the deterministic engines already found. For each, "
        'reply in "review" with: {"start":N,"end":M,"verdict":"PII"|"NOT_PII"|"UNSURE",'
        '"entity_type":"TYPE_OR_UNKNOWN","confidence":0.0-1.0,"reason":"short"}. '
        "Use NOT_PII only when you are confident the span is not personal data. "
        "Omitting a span means UNSURE, not NOT_PII."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def _candidate_summary(
    existing: list[Candidate],
    text: str,
    *,
    limit: int = 24,
    offset: int = 0,
    window_len: int | None = None,
) -> str:
    """Existing candidates, printed in the coordinate system of the prompt.

    ``offset`` is the window start. Offsets printed here MUST match the
    convention the prompt asks the model to reply in, or the model mirrors the
    wrong one and every rebased span lands outside the document. See D1.

    Slice the surface with the **absolute** offsets against the full ``text``;
    print the **window-relative** ones. Do not conflate the two.
    """
    if not existing:
        return "Existing engines found 0 candidates.\n"
    win_len = int(window_len) if window_len is not None else max(0, len(text) - int(offset))
    lines = [f"Existing engines found {len(existing)} candidate(s):"]
    shown = 0
    eligible = 0
    for cand in existing:
        if cand.start is None or cand.end is None:
            continue
        rel_start = max(0, min(cand.start - offset, win_len))
        rel_end = max(0, min(cand.end - offset, win_len))
        if rel_end <= rel_start:
            continue
        eligible += 1
        if shown >= limit:
            continue
        slice_txt = ""
        if 0 <= cand.start < cand.end <= len(text):
            slice_txt = text[cand.start:cand.end][:48].replace("\n", " ")
        lines.append(
            f"- [{rel_start}:{rel_end}] {cand.entity_type} {cand.engine} {slice_txt!r}"
        )
        shown += 1
    if eligible > shown:
        lines.append(f"- … {eligible - shown} more")
    lines.append("")
    return "\n".join(lines)


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
        api_key: Optional[str] = None,
    ):
        self._cfg = redibis_config
        self._provider = provider
        self._allow_cloud = allow_cloud
        self._api_key = (api_key or "").strip() or None
        self.last_coverage: dict = {}
        self.last_reviews: list = []

    def propose_spans(
        self,
        text: str,
        existing: list[Candidate],
        config: TextScanConfig,
        *,
        text_rules: object | None = None,
    ) -> list[Candidate]:
        self.last_reviews = []
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
        from redibis.pii.ner_window import windows as split_windows

        target = int(getattr(config, "llm_window_chars", 3500) or 3500)
        overlap = int(getattr(config, "llm_window_overlap", 300) or 300)
        max_wins = int(getattr(config, "llm_max_windows", 8) or 8)
        wins = split_windows(text, target_chars=target, overlap_chars=overlap)
        total = len(wins)
        capped = False
        if len(wins) > max_wins:
            wins = wins[:max_wins]
            capped = True

        existing_by_window: dict[int, list[Candidate]] = {i: [] for i in range(len(wins))}
        for cand in existing:
            if cand.start is None or cand.end is None:
                continue
            for i, win in enumerate(wins):
                if cand.start < win.end and cand.end > win.start:
                    existing_by_window[i].append(cand)

        out: list[Candidate] = []
        seen: set[tuple[int, int, str]] = set()
        reviews: list = []
        covered_end = 0
        for i, win in enumerate(wins):
            covered_end = max(covered_end, win.end)
            prompt = (
                f"Text:\n{win.text}\n\n"
                f"{_overlay_prompt(text_rules)}"
                "Offsets are 0-based indices into the Text block above, which is an excerpt.\n"
                "The first character of the Text block is offset 0. Existing candidates below\n"
                "use the same convention.\n"
                f"{_candidate_summary(existing_by_window.get(i) or [], text, offset=win.start, window_len=len(win.text))}\n"
                "Propose any missed PII spans (spoken Arabic digits, obfuscated emails, "
                "names, full addresses, telecom IDs, PUK, voucher PINs, ticket IDs). "
                "Extend partial address/name spans to the full clause. "
                'Reply as JSON: {"spans":[...],"review":[...]}.'
            )
            try:
                raw = self._call_model(prompt, system=system)
            except Exception as exc:
                logger.warning("LLM text call failed: %s", exc)
                raise

            spans, review_rows = self._parse_response(raw)
            for item in spans:
                try:
                    start = int(item["start"]) + win.start
                    end = int(item["end"]) + win.start
                    et = str(item.get("entity_type") or "").upper().replace(" ", "_")
                    score = float(item.get("score") or 0.5)
                except (KeyError, TypeError, ValueError):
                    continue
                if not et or start < 0 or end > len(text) or start >= end:
                    continue
                slice_text = text[start:end]
                if not slice_text.strip():
                    continue
                if config.entities and et not in config.entities:
                    continue
                key = (start, end, et)
                if key in seen:
                    continue
                seen.add(key)
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
            for row in review_rows:
                parsed = self._coerce_review(row, text=text, offset=win.start, existing=existing)
                if parsed is not None:
                    reviews.append(parsed)
        fraction = (covered_end / len(text)) if text else 1.0
        self.last_coverage = {
            "fraction": round(fraction, 4),
            "windows_scanned": len(wins),
            "windows_total": total,
            "reasons": {"llm": f"window cap reached ({max_wins})"} if capped else {},
        }
        self.last_reviews = reviews
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
            api_key=self._api_key or ((getattr(llm, "api_key", None) or None) if llm else None),
            endpoint_url=(getattr(llm, "endpoint_url", None) or None) if llm else None,
        )
        self._assert_local_provider(binding.provider)
        model_id = binding.model or getattr(provider, "model", "") or binding.provider
        return provider, model_id

    def resolve_with_path(self) -> tuple[Any, str, str]:
        """Resolve provider + model and name the path that won.

        Paths: ``request override`` / ``capability role pii.text_refiner`` /
        ``legacy pii.llm``.
        """
        if self._provider is not None:
            model_id = (
                getattr(self._provider, "model", "")
                or getattr(self._provider, "litellm_model", "")
                or getattr(self._provider, "name", "")
                or "pii-text-llm"
            )
            return self._provider, model_id, "request override"

        try:
            provider, model_id = self._resolve_from_role()
            return provider, model_id, "capability role pii.text_refiner"
        except Exception as exc:
            logger.debug("pii.text_refiner role unavailable: %s", exc)

        from redibis.enrich.providers import get_provider

        llm = getattr(self._cfg.pii, "llm", None) if self._cfg else None
        provider_name = getattr(llm, "provider", "sglang") if llm else "sglang"
        self._assert_local_provider(provider_name)
        model = getattr(llm, "model_name", "") if llm else ""
        endpoint = getattr(llm, "endpoint_url", None) if llm else None
        prov = get_provider(
            provider_name, model=model, endpoint_url=endpoint, api_key=self._api_key,
        )
        model_id = model or provider_name or "pii-text-llm"
        return prov, model_id, "legacy pii.llm"

    def _resolve_provider(self) -> tuple[Any, str]:
        """Resolve the actual provider object that will handle the call, plus
        a display ``model_id``. Prefer request override, then capability role
        ``pii.text_refiner``, then legacy ``pii.llm``.
        """
        provider, model_id, _path = self.resolve_with_path()
        return provider, model_id

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
        spans, _reviews = LlmTextRefiner._parse_response(raw)
        return spans

    @staticmethod
    def _parse_response(raw: str) -> tuple[list[dict], list[dict]]:
        text = (raw or "").strip()
        if not text:
            return [], []
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return [], []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [], []
        if not isinstance(data, dict):
            return [], []
        spans = data.get("spans")
        review = data.get("review")
        return (
            list(spans) if isinstance(spans, list) else [],
            list(review) if isinstance(review, list) else [],
        )

    @staticmethod
    def _coerce_review(
        row: object,
        *,
        text: str,
        offset: int,
        existing: list[Candidate],
    ):
        from redibis.pii.privacy import scrub_pii_text
        from redibis.pii.span_arbiter import LlmReview

        if not isinstance(row, dict):
            return None
        try:
            start = int(row.get("start")) + int(offset)
            end = int(row.get("end")) + int(offset)
        except (TypeError, ValueError):
            return None
        if start < 0 or end > len(text) or start >= end:
            return None
        slice_text = text[start:end]
        if not slice_text.strip():
            return None
        if not any(
            c.start is not None and c.end is not None and start < c.end and c.start < end
            for c in existing
        ):
            return None
        verdict = str(row.get("verdict") or "UNSURE").strip().upper().replace(" ", "_")
        if verdict not in {"PII", "NOT_PII", "UNSURE"}:
            verdict = "UNSURE"
        try:
            confidence = float(row.get("confidence") if row.get("confidence") is not None else 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        entity_type = str(row.get("entity_type") or "").upper().replace(" ", "_")
        reason = scrub_pii_text(str(row.get("reason") or ""))[:200]
        return LlmReview(
            start=start,
            end=end,
            verdict=verdict,
            entity_type=entity_type,
            confidence=confidence,
            reason=reason,
        )
