"""Responsible-AI middleware — enforced in code, not prompts."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from redibis.config import RAIConfig
from redibis.telemetry.models import ModelCallRecord
from redibis.telemetry.otel import TelemetryCollector

logger = logging.getLogger(__name__)

_VALID_RAI_MODES = frozenset({"block", "warn", "report"})


class RAIDecision:
    """Outcome of a single RAI policy check."""

    __slots__ = ("allowed", "reason", "residency", "redacted", "hard")

    def __init__(
        self,
        allowed: bool,
        reason: str = "",
        residency: str = "local",
        redacted: bool = False,
        *,
        hard: bool = False,
    ):
        self.allowed = allowed
        self.reason = reason
        self.residency = residency
        self.redacted = redacted
        self.hard = hard


class RAIMiddleware:
    """Wrap every model call with residency, allow/deny, and decision logging."""

    def __init__(
        self,
        config: Optional[RAIConfig] = None,
        *,
        telemetry: Optional[TelemetryCollector] = None,
    ):
        self.config = config or RAIConfig()
        self.telemetry = telemetry or TelemetryCollector()
        self.decisions: list[dict[str, Any]] = []

    def check_model_allowed(self, model_id: str) -> RAIDecision:
        if self.config.denied_models and model_id in self.config.denied_models:
            return RAIDecision(allowed=False, reason=f"model {model_id!r} denied by policy")
        if self.config.allowed_models and model_id not in self.config.allowed_models:
            return RAIDecision(allowed=False, reason=f"model {model_id!r} not in allow-list")
        return RAIDecision(allowed=True)

    def check_residency(
        self,
        *,
        residency: str,
        contains_raw_pii: bool,
    ) -> RAIDecision:
        residency = (residency or "local").strip().lower()
        if residency == "local":
            return RAIDecision(allowed=True, residency=residency)

        if (
            self.config.block_external_raw_pii
            and contains_raw_pii
        ):
            return RAIDecision(
                allowed=False,
                reason="raw PII blocked for external/public model residency",
                residency=residency,
                hard=bool(self.config.hard_block_external_pii),
            )
        return RAIDecision(allowed=True, residency=residency)

    def wrap_model_call(
        self,
        fn: Callable[..., Any],
        *,
        model_id: str,
        residency: str = "local",
        contains_raw_pii: bool = False,
        pii_columns: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> Any:
        if not self.config.enabled:
            return fn(**kwargs)

        model_check = self.check_model_allowed(model_id)
        if not model_check.allowed:
            self._handle_violation(model_id, model_check)

        residency_check = self.check_residency(
            residency=residency,
            contains_raw_pii=contains_raw_pii,
        )
        if not residency_check.allowed:
            self._handle_violation(model_id, residency_check)

        with self.telemetry.span(
            "rai.model_call",
            model_id=model_id,
            residency=residency,
            contains_raw_pii=contains_raw_pii,
            pii_columns=list(pii_columns or []),
        ):
            result = fn(**kwargs)

        self._log_decision(model_id, residency_check, blocked=False, would_block=False)
        return result

    def report(self) -> dict[str, Any]:
        advisories = [d for d in self.decisions if d.get("would_block")]
        return {
            "enabled": self.config.enabled,
            "mode": self.effective_mode(),
            "enforce": self.config.enforce,
            "hard_block_external_pii": self.config.hard_block_external_pii,
            "decisions": list(self.decisions),
            "advisories": advisories,
            "advisory_count": len(advisories),
        }

    def effective_mode(self) -> str:
        if self.config.enforce:
            return "block"
        mode = (self.config.mode or "report").lower()
        return mode if mode in _VALID_RAI_MODES else "report"

    def _should_block(self, decision: RAIDecision) -> bool:
        if decision.hard and self.config.hard_block_external_pii:
            return True
        if self.config.enforce:
            return True
        return self.effective_mode() == "block"

    def _handle_violation(self, model_id: str, decision: RAIDecision) -> None:
        if self._should_block(decision):
            self._log_decision(model_id, decision, blocked=True, would_block=True)
            logger.warning(
                "RAI blocked model call model_id=%r residency=%r reason=%r hard=%s",
                model_id,
                decision.residency,
                decision.reason,
                decision.hard,
            )
            raise PermissionError(decision.reason)

        if self.effective_mode() == "warn":
            logger.warning("RAI advisory (not blocking): %s", decision.reason)

        self._log_decision(model_id, decision, blocked=False, would_block=True)

    def _log_decision(
        self,
        model_id: str,
        decision: RAIDecision,
        *,
        blocked: bool,
        would_block: bool = False,
    ) -> None:
        record = {
            "model_id": model_id,
            "allowed": decision.allowed,
            "blocked": blocked,
            "would_block": would_block,
            "hard": decision.hard,
            "reason": decision.reason,
            "residency": decision.residency,
            "mode": self.effective_mode(),
        }
        self.decisions.append(record)
        self.telemetry.record_model_call(ModelCallRecord(
            model_id=model_id,
            residency=decision.residency,
            blocked=blocked,
            block_reason=decision.reason if (blocked or would_block) else "",
            decision="blocked" if blocked else ("advisory" if would_block else "ok"),
        ))
