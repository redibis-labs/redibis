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
    "render_notebook_code",
    "CODE_STYLES",
]

# "program": the classic self-contained program (RULES list, CLI main) the monitor
# package ships. "notebook": complete Jupyter code — read the data, build a Spark
# (or pandas) DataFrame, write the rules with QualityDraft, validate. "rules": only
# the rules and a validate() call, to apply to any DataFrame you already have.
CODE_STYLES = ("program", "notebook", "rules")


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
    ``{"rule", "column", "kwargs", "meta"}`` shape the renderers consume.
    SQL rules keep their query in ``kwargs["sql"]`` (see ``quality.sql_rules``)."""
    from redibis.quality.sql_rules import is_sql_rule, normalize_sql_rule

    rule = dict(rule)
    if rule.get("column") in _TABLE_LEVEL:
        rule["column"] = None
    if is_sql_rule(rule):
        return normalize_sql_rule(rule)
    odcs = _from_contract_form(rule)
    if odcs is not None:
        return odcs
    etype = rule.get("rule") or rule.get("expectation_type") or rule.get("expectation_name")
    kwargs = dict(rule.get("kwargs") or {})
    if kwargs.get("column") in _TABLE_LEVEL:
        kwargs.pop("column")
    out: dict[str, Any] = {"rule": etype, "column": rule.get("column"), "kwargs": kwargs}
    meta = rule.get("meta")
    if isinstance(meta, dict) and meta:
        out["meta"] = dict(meta)
    return out


_TABLE_LEVEL = ("Table-Level", "__table__", "")


def _from_contract_form(rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A rule as the contract / Approved basket stores it (``{"rule": "duplicateCount",
    "mustBe": 0}`` or ``{"engine": "greatExpectations", "implementation": {…}}``) → GE shape."""
    from redibis.contracts.rules import _odcs_quality_to_rule, _rule_to_ge

    name = str(rule.get("rule") or "")
    if "implementation" not in rule and (not name or name.startswith("expect_")):
        return None
    column = rule.get("column")
    entry = {k: v for k, v in rule.items() if k != "column"}
    ge = _rule_to_ge(_odcs_quality_to_rule("", entry, column))
    if not ge or not ge.get("expectation_type"):
        return None
    kwargs = dict(ge.get("kwargs") or {})
    kwargs.pop("column", None)
    out: dict[str, Any] = {"rule": ge["expectation_type"], "column": column, "kwargs": kwargs}
    meta = dict(rule.get("meta") or {})
    if rule.get("severity") in ("P1", "P2", "P3"):
        meta["severity"] = rule["severity"]
    if meta:
        out["meta"] = meta
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


def _draft_for(table: str, curated: CuratedRules):
    """The curated rules as a ``QualityDraft`` (severity from each rule's meta kept)."""
    from redibis.quality.authoring import QualityDraft

    draft = QualityDraft.new(table)
    for rule in curated.effective_rules:
        rule = dict(rule)
        severity = (rule.pop("meta", None) or {}).get("severity")
        added = draft.add_rules([rule])
        if severity in ("P1", "P2", "P3") and added:
            draft.set_severity(severity, indices=[r["index"] for r in added])
    return draft


def _reader_lines(data_path: str, engine: str) -> list[str]:
    path = data_path or "path/to/your_file.csv"
    parquet = path.lower().endswith((".parquet", ".pq"))
    read = "pd.read_parquet(DATA)" if parquet else "pd.read_csv(DATA)"
    lines = [f"DATA = {path!r}" + ("" if data_path else "   # ← your file"),
             f"pdf = {read}                        # read as the scan read it (same column types)"]
    if engine == "spark":
        lines += [
            "",
            "spark = SparkSession.builder.appName(\"redibis-quality\").getOrCreate()",
            "df = spark.createDataFrame(pdf.astype(object).where(pdf.notna(), None))   # NaN → null",
            "# A real partition instead: df = spark.table(\"lake.db.table\").where(\"dt = '2026-09-26'\")",
        ]
    else:
        lines.append("df = pdf")
    return lines


def render_notebook_code(
    *,
    table: str,
    curated: CuratedRules,
    style: str = "notebook",
    engine: str = "spark",
    data_path: str = "",
) -> str:
    """Jupyter code in the ``QualityDraft`` style (see ``CODE_STYLES``).

    ``notebook``: imports, the data read into a DataFrame (``data_path``, e.g. the
    file uploaded in the web app; Spark by default), one ``qa.expect_…(…)`` /
    ``qa.add_sql(…)`` line per rule, and the validation (``spark_mode="fused"``).
    ``rules``: the rule lines and ``qa.validate(df)`` only. Both paste back into
    the Quality page (the parser reads these calls; nothing is executed).
    """
    engine = (engine or "spark").strip().lower()
    if engine not in ("spark", "pandas"):
        raise ValueError(f"unsupported codegen engine {engine!r} (use 'spark' or 'pandas')")
    if style not in ("notebook", "rules"):
        raise ValueError(f"unsupported code style {style!r} (use one of {', '.join(CODE_STYLES)})")
    draft = _draft_for(table, curated)
    rules = draft.rule_lines("qa")
    count = len(rules)
    head = [f"# Data quality rules for {table} — {count} rule(s) from {curated.rule_source}, generated by redibis.",
            "# Edit freely: add qa.expect_…(…) lines, delete lines, change severity=\"P1\" | \"P2\" | \"P3\"."]
    rule_block = [f"qa = QualityDraft.new({table!r})", *rules]
    if style == "rules":
        return "\n".join([
            *head,
            "from redibis.quality import QualityDraft",
            "",
            *rule_block,
            "",
            "# Validate on any pandas or Spark DataFrame you already have (Spark: spark_mode=\"fused\").",
            "result = qa.validate(df)",
            "print(result)",
            "result.to_frame(only_failed=True)          # every rule: result.to_frame()",
        ]) + "\n"
    imports = ["import pandas as pd"]
    if engine == "spark":
        imports.append("from pyspark.sql import SparkSession")
    imports.append("from redibis.quality import QualityDraft")
    validate = ('result = qa.validate(df, spark_mode="fused")   # aggregate rules in one Spark job'
                if engine == "spark" else "result = qa.validate(df)")
    return "\n".join([
        *head,
        *imports,
        "",
        "# 1. Data",
        *_reader_lines(data_path, engine),
        "",
        "# 2. Rules",
        *rule_block,
        "",
        "# 3. Validate — nothing is written anywhere",
        validate,
        "print(result)",
        "result.to_frame(only_failed=True)          # every rule: result.to_frame()",
        "",
        "# Next: save as a quality run for review  →  from redibis.quality.authoring import QualityAuthor",
        f"#       QualityAuthor({table!r}).save(qa, note=\"from Jupyter\")",
    ]) + "\n"
