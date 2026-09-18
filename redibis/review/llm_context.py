"""A3 — LLM context pack derived from steward verdicts (no LLM, no samples)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from redibis.review.rationale import RATIONALE_BY_CODE
from redibis.store.review_store import ReviewState


def write_llm_context(svc, table: str, state: ReviewState, digest: str, a1: dict, a2: dict) -> dict[str, Any]:
    active = svc.store.get_active(table) or {}
    taxonomy_lines = ["# Taxonomy", ""]
    used_class: dict[str, str] = {}
    used_entity: dict[str, str] = {}
    glossary_lines = ["# Glossary", ""]
    for schema_obj in active.get("schema") or []:
        for prop in schema_obj.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            name = prop.get("name")
            cls = str((prop.get("privacy") or {}).get("classification") or prop.get("classification") or "")
            entity = str(prop.get("entity_type") or "")
            definition = str(prop.get("description") or "")
            if cls:
                used_class.setdefault(cls, definition or name)
            if entity:
                used_entity.setdefault(entity, definition or name)
            if definition:
                glossary_lines.append(f"- `{name}`: {definition}")
    taxonomy_lines.append("## Classification levels")
    for cls, desc in sorted(used_class.items()):
        taxonomy_lines.append(f"- **{cls}** — {desc}")
    taxonomy_lines.append("")
    taxonomy_lines.append("## Entity types")
    for ent, desc in sorted(used_entity.items()):
        taxonomy_lines.append(f"- **{ent}** — {desc}")

    by_code: dict[str, list[str]] = defaultdict(list)
    for entry in a1.get("entries") or []:
        code = entry.get("rationale_code") or "other"
        col = entry.get("column") or entry.get("item")
        src = entry.get("chosen_source") or ""
        decision = entry.get("decision")
        line = (
            f"column `{col}` — steward: {decision} "
            f"(source {src or 'n/a'}) — {code}"
        )
        if len(by_code[code]) < 8:
            by_code[code].append(line)

    decisions_lines = ["# Decisions (few-shot)", ""]
    for code, examples in sorted(by_code.items()):
        label = RATIONALE_BY_CODE.get(code)
        title = label.label if label else code
        decisions_lines.append(f"## {title} (`{code}`)")
        for ex in examples:
            decisions_lines.append(f"- {ex}")
        decisions_lines.append("")

    inferred: dict[str, int] = defaultdict(int)
    for entry in a1.get("entries") or []:
        col = str(entry.get("column") or "")
        code = entry.get("rationale_code") or ""
        if col.endswith("_ref") and code in (
            "false_positive_identifier_not_personal", "engine_wrong_type",
        ):
            inferred["columns named *_ref are internal identifiers, not personal"] += 1

    rules_lines = ["# Inferred steward rules", ""]
    rules_lines.append("These rules are *inferred* from repeated rationale codes + name patterns.")
    if inferred:
        for rule, n in inferred.items():
            rules_lines.append(f"- {rule} (n={n}, inferred)")
    else:
        rules_lines.append("- (none inferred)")

    context = {
        "review_digest": digest,
        "table": table,
        "residency": "portable_names",
        "taxonomy": {"classification": used_class, "entity_types": used_entity},
        "decisions_by_code": {k: v for k, v in by_code.items()},
        "inferred_rules": list(inferred),
    }
    return {
        "00_taxonomy.md": "\n".join(taxonomy_lines) + "\n",
        "10_glossary.md": "\n".join(glossary_lines) + "\n",
        "20_decisions.md": "\n".join(decisions_lines) + "\n",
        "30_rules.md": "\n".join(rules_lines) + "\n",
        "context.json": context,
    }


def load_steward_context_markdown(files: dict[str, Any]) -> str:
    parts = []
    for name in ("00_taxonomy.md", "10_glossary.md", "20_decisions.md", "30_rules.md"):
        body = files.get(name)
        if body:
            parts.append(body if isinstance(body, str) else str(body))
    return "\n\n".join(parts)
