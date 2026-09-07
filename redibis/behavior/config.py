"""
redibis.behavior.config
========================
BehaviorConfig — the YAML spine for the behavior policy runtime.

Added to RedibisConfig.behavior (default: disabled).
When disabled, all existing flows are byte-identical to prior behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from redibis.behavior.models import FailMode, RuntimeMode


@dataclass
class BehaviorConfig:
    """
    Configuration for the behavior policy runtime.

    Default: disabled — no behavioral change unless explicitly activated.

    fail_mode
    ---------
    ``baseline_with_warning`` (default)
        Preserve baseline engine result on ordinary evaluator failure and
        surface a visible BehaviorPolicyWarning to all callers.
        This is NOT silent fail-open — the warning reaches library/service
        results, CLI/web progress, run artifacts, and audit.

    ``fail_run``
        Stop the governed run with a policy-specific error when the evaluator
        fails. Use when silently reintroducing a known FP/FN is less
        acceptable than failing the run. Safety/legal guard failures are
        fail-closed regardless of this setting.
    """

    enabled: bool           = False
    mode: str               = RuntimeMode.DISABLED.value   # disabled | shadow | active
    engine_modes: dict      = field(default_factory=dict)  # per-engine override
    policies: list          = field(default_factory=list)  # policy IDs or paths
    allow_per_run_overlay: bool = False
    plugin_allowlist: list  = field(default_factory=list)
    fail_mode: str          = FailMode.BASELINE_WITH_WARNING.value
    max_rules_per_context: int = 200
    max_eval_ms: int        = 50

    def effective_mode(self, engine: Optional[str] = None) -> RuntimeMode:
        """Return the effective RuntimeMode for the given engine."""
        if not self.enabled:
            return RuntimeMode.DISABLED
        if engine and engine in self.engine_modes:
            try:
                return RuntimeMode(self.engine_modes[engine])
            except ValueError:
                pass
        try:
            return RuntimeMode(self.mode)
        except ValueError:
            return RuntimeMode.DISABLED

    def effective_fail_mode(self) -> FailMode:
        try:
            return FailMode(self.fail_mode)
        except ValueError:
            return FailMode.BASELINE_WITH_WARNING
