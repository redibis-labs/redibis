"""Shared curated-rule resolution + full-program rendering for quality codegen.

One resolver, one renderer — so the Quality review page, the results page, the
package download, and the CLI all emit code for the *same* rules with the same
recorded provenance. Every origin is explicit: there is no silent semantic
fallback from "what the user curated" to "whatever the contract says".

Rule origins (``CuratedRules.rule_source``):

``pasted_code``      rules recovered from full Python or a paste fragment,
                     via the AST-only parser (never executed)
``session_draft``    the session's curated draft rule set, minus dropped rules
``session_run``      the last evaluated quality run on this session
``active_contract``  the effective active contract (suppressed rules removed,
                     approved manual rules included)
``none``             nothing resolvable — callers must decide, not guess
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

__all__ = [
    "CuratedRules",
    "resolve_curated_rules",
    "rules_from_python_source",
    "render_quality_program",
]


@dataclass
class CuratedRules:
    """Resolved rules plus where they came from."""

    rules: Optional[list[dict[str, Any]]] = None
    contract: Optional[dict[str, Any]] = None
    rule_source: str = "none"
    dropped_rule_ids: list[str] = field(default_factory=list)
    parse_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def effective_rules(self) -> list[dict[str, Any]]:
        """Rules in the GE-shaped dict form the renderers and package expect."""
        if self.rules is not None:
            return list(self.rules)
        if self.contract is not None:
            from redibis.quality.contract_validate import quality_rules_from_contract

            return list(quality_rules_from_contract(self.contract).rules)
        return []


def _normalize_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Normalize any of the rule shapes flowing through the app into the
    ``{"rule", "column", "kwargs", "meta"}`` shape the renderers consume."""
    etype = rule.get("rule") or rule.get("expectation_type") or rule.get("expectation_name")
    out: dict[str, Any] = {
        "rule": etype,
        "column": rule.get("column"),
        "kwargs": dict(rule.get("kwargs") or {}),
    }
    meta = rule.get("meta")
    if isinstance(meta, dict) and meta:
        out["meta"] = dict(meta)
    return out


def rules_from_python_source(code: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recover proposed rules from generated/edited Python — parse only.

    Delegates to the AST-safe parser, so an uploaded or pasted program is never
    imported, compiled, or executed; only literal rule definitions survive.
    """
    from redibis.contracts.rule_code_parser import parse_ge_rules

    parsed = parse_ge_rules(code or "")
    return [_normalize_rule(r) for r in parsed.get("rules", [])], list(parsed.get("errors", []))


def resolve_curated_rules(
    *,
    session: Any = None,
    table: str = "",
    pasted_code: Optional[str] = None,
    explicit_rules: Optional[Sequence[dict[str, Any]]] = None,
    dropped_indices: Optional[Sequence[int]] = None,
    store: Any = None,
    allow_contract_fallback: bool = True,
) -> CuratedRules:
    """Resolve the one rule set a code/package export should be built from.

    Priority is explicit and ordered; the first available origin wins and is
    recorded, rather than being blended with the next one:

    1. ``pasted_code`` — full Python or a fragment the user just edited
    2. ``explicit_rules`` — structured rules the caller already curated
    3. the session's curated draft rule set
    4. the session's last evaluated quality run
    5. the effective active contract (only when ``allow_contract_fallback``)
    """
    dropped = {int(i) for i in (dropped_indices or [])}
    dropped_ids = [str(i) for i in sorted(dropped)]

    def _curate(rules: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [r for i, r in enumerate(rules) if i not in dropped]
        return [_normalize_rule(r) for r in kept]

    if pasted_code and pasted_code.strip():
        rules, errors = rules_from_python_source(pasted_code)
        return CuratedRules(
            rules=_curate(rules),
            rule_source="pasted_code",
            dropped_rule_ids=dropped_ids,
            parse_errors=errors,
        )

    if explicit_rules is not None:
        return CuratedRules(
            rules=_curate(explicit_rules),
            rule_source="session_draft" if session is not None else "rules_file",
            dropped_rule_ids=dropped_ids,
        )

    if session is not None:
        draft = getattr(session, "quality_rules_draft", None)
        if draft:
            return CuratedRules(
                rules=_curate(draft),
                rule_source="session_draft",
                dropped_rule_ids=dropped_ids,
            )
        runs = getattr(session, "runs", None) or []
        if runs:
            last_results = getattr(runs[-1], "quality_results", None) or []
            evaluated = [r for r in last_results if r.get("rule") or r.get("expectation_type")]
            if evaluated:
                return CuratedRules(
                    rules=_curate(evaluated),
                    rule_source="session_run",
                    dropped_rule_ids=dropped_ids,
                )

    if allow_contract_fallback:
        table = table or getattr(session, "table_name", "") or ""
        contract = _effective_active_contract(table, store)
        if contract is not None:
            return CuratedRules(
                contract=contract,
                rule_source="active_contract",
                dropped_rule_ids=dropped_ids,
            )

    return CuratedRules(rule_source="none", dropped_rule_ids=dropped_ids)


def _effective_active_contract(table: str, store: Any) -> Optional[dict[str, Any]]:
    """Active contract with the quality-decision overlay applied in memory.

    ``store`` is supplied by the caller (web accessor or CLI) — this module
    never reaches into the webapp layer.
    """
    if not table or store is None:
        return None
    try:
        from redibis.store.quality_decisions import effective_contract_quality

        active = store.get_active(table)
        if active is None:
            return None
        return effective_contract_quality(active, store.quality_decisions.get(table))
    except Exception:  # noqa: BLE001 — a missing store must not break codegen
        return None


def render_quality_program(
    *,
    table: str,
    curated: CuratedRules,
    engine: str = "spark",
) -> str:
    """Render the complete, self-contained program for ``curated``'s rules.

    This is the single path every UI/CLI "copy Jupyter code" action goes
    through, so the copied program and the downloaded package always agree.
    """
    from redibis.quality.ge_codegen import detect_runtime_versions, render_full_quality_program
    from redibis.quality.rule_set import QualityRuleSet
    from redibis.quality.schema import rule_set_from_contract, rule_set_from_ge_rules

    rules = curated.effective_rules
    slug = table.replace(".", "_").replace("-", "_")

    if curated.rules is None and curated.contract is not None:
        canonical = rule_set_from_contract(curated.contract, table=table)
    else:
        canonical = rule_set_from_ge_rules(
            rules, table=table, source=curated.rule_source or "session_draft"
        )

    redibis_version, ge_version = detect_runtime_versions()
    return render_full_quality_program(
        table=table,
        rule_set=QualityRuleSet(name=slug, rules=rules),
        rule_set_id=canonical.rule_set_id,
        rule_set_digest=canonical.semantic_digest,
        redibis_version=redibis_version,
        ge_version=ge_version,
        engine=engine,
    )
