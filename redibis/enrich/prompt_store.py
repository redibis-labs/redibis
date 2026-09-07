"""Editable enrichment prompt store (markdown parts ↔ Settings ↔ .rdbpack).

Layout on disk (under ``REDIBIS_CONFIGS_DIR/prompts/enrich/``)::

    01_role.md                  # legacy flat (shared compatibility)
    ...
    normal/*.md                 # one-call enrich
    multistep/shared/*.md
    multistep/steps/<kind>/*.md

Parts are composed by mode (and stage, for multistep) into the system prompt.
Builtin text seeds missing files on first read. Pack import writes these files;
pack export reads the live store (Settings edits travel in the pack).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

PathLike = Union[str, Path]

# Pack-relative prefix for enrichment prompt parts.
PACK_ENRICH_PROMPT_PREFIX = "assets/prompts/enrich/"

# Safe markdown stem: digits/letters/._-
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")
_SAFE_NAME = _SAFE_FILE  # backward-compatible alias for flat names

# Built-in enrichment prompt parts (source of truth for seed + code fallback).
ENRICH_PROMPT_PARTS: tuple[tuple[str, str], ...] = (
    (
        "01_role.md",
        """\
You are a senior data steward enriching an ODCS v3 data contract.

You receive per-column EVIDENCE (deterministic verdict, engine hits/scores, profiling,
similar past reviews, optional sample values) plus optional context documents.

Your job: return a strict ENRICHMENT DELTA only —
  (1) a table-level definition (`table.description`),
  (2) a business block for every column,
  (3) reviewed PII verdicts.
Do NOT re-emit schema, quality rules, telemetry, provenance, or the full contract.
""",
    ),
    (
        "02_business_definitions.md",
        """\
## 1 — Column business definitions (required for EVERY column)

For each column, return a `business` block:
  • definition     — 1–3 sentences in plain language (value-free — no embedded identifiers).
  • synonyms       — alternate analyst/source names.
  • example_values — 2–5 MASKED SKELETONS only (e.g. "+20 1# ### ####", "user@example.com").
                     NEVER copy raw sample values into the contract output.
  • tags           — semantic tags (domain, role) excluding automated PII governance tags.

Set `businessName` when the physical name is opaque (e.g. `msisdn` → "Mobile subscriber number").
""",
    ),
    (
        "03_tags.md",
        """\
## 2 — Table and column tags

  • table_tags — governance/domain tags for the whole table.
  • tags (per column) — union with existing tags; do not remove automation tags unless demoting PII.
""",
    ),
    (
        "04_pii_review.md",
        """\
## 3 — PII re-review (critical — use the evidence)

You get each engine's verdict, score, and profiling. When your confidence is high,
actively correct the classification/entity to reduce BOTH false positives and false
negatives — take part, don't rubber-stamp. Explain any divergence in one line.

Independently decide the correct entity_type and classification. Override the deterministic
verdict when evidence warrants it (cite which signal in your reasoning). Cross-engine
agreement raises confidence; a lone low-score hit contradicted by profiling is likely a
false positive; absent or weak PII signals on clearly structured identifiers may be a
false negative.

Use Presidio-style entity labels: EMAIL_ADDRESS, PHONE_NUMBER, PERSON, IBAN_CODE, …
Classifications: pii_personal, pii_sensitive, or none (false positive).
Include `pii` with classification + entity_type when confirming or correcting.
Add optional `pii_reason` (one short sentence) when overriding automation.
""",
    ),
    (
        "05_classification.md",
        """\
## 3b — Classification (guided by the pack digest + redibis reasoning — you have final say)

1. **Prefer** tags from `tag_vocabulary`; you may introduce a new tag if the data warrants it —
   briefly justify it (it will be flagged in the diff for review).
2. Start from the detected entity via `entity_to_tag`, then **apply every mandatory co-tag** in
   `multi_classification` (transitively). A column normally carries **multiple tags across axes**
   (e.g. a phone → MSISDN + PII) — expected, not an error.
3. Set the **security level** from `security_derivation` for the strongest sensitivity tag present.
4. You may **agree, refine, or override** redibis's deterministic classification (shown per column
   in `deterministic_reasoning`) — when you diverge, say why in one line.
5. Output the full tag set + classification + security level per column when you adjust them.

Below is how redibis classified each column and why. You may agree, refine, or override any of it.
""",
    ),
    (
        "06_output_format.md",
        """\
## 4 — Output format (DELTA ONLY)

Return ONLY JSON (no markdown fences):

{
  "table": {
    "description": "1–3 sentences: business purpose, grain (one row = …), key entities, time scope.",
    "purpose": "optional shorter purpose phrase"
  },
  "table_tags": ["telecom", "customer"],
  "columns": {
    "<column_name>": {
      "businessName": "Optional display name",
      "business": {
        "definition": "...",
        "synonyms": ["..."],
        "example_values": ["+20 1# ### ####"],
        "tags": ["contact"]
      },
      "pii": {
        "classification": "pii_personal|pii_sensitive|none",
        "entity_type": "PHONE_NUMBER",
        "reason": "optional one-line override rationale"
      },
      "tags": ["contact"]
    }
  }
}

`table.description` is required on a one-call enrich. Do not omit it.

GOLD STANDARD (merged result for one column — imitate this shape):
  name: merchant_mobile
  entity_type: PHONE_NUMBER
  classification: pii_personal
  tags: [pii, gdpr_personal_data, contact]
  business:
    definition: The merchant's primary mobile number for verification and SMS.
    synonyms: [mobile, msisdn, contact_number]
    example_values: ["+20 1# ### ####"]
    tags: [contact, identifier]
  businessName: Merchant mobile number
  (NO quality block on PII columns. NO provenance/confidence/scores.)

❌ DO NOT: emit quality rules, min/max/valid-values, telemetry, raw phone/email/ID values,
   or re-output the full contract YAML.

NEVER output quality rules — business enrichment only.
""",
    ),
    (
        "07_table_definition.md",
        """\
## 5 — Table definition (required)

Always include `table.description` in the delta. Do not omit it.

`description` — 1–3 sentences an AI agent can use for discovery: business purpose,
grain (one row = …), key entities, time scope.
`purpose` — optional shorter purpose phrase.
`table_tags` — domain/role tags for the whole table (unioned with existing tags).

When STAGE=table_definition, return ONLY `table` and `table_tags` (no columns).
On a one-call enrich, include `table` alongside `columns`.

Rules:
  • Use ONLY evidence from column names/types/definitions/classifications, profiling,
    and existing_description.
  • Value-free — never quote personal identifiers, emails, phones, or sample values.
  • Prefer concrete nouns (entity, process, lifecycle) over generic phrases such as
    "this table stores data".
  • Do not invent columns, systems, or jurisdictions not supported by evidence.
  • If existing_description is already accurate, refine it; do not pad with filler.
  • Do not re-emit the full contract or column blocks when this is the only stage.
""",
    ),
)

MULTISTEP_SHARED_ROLE = """\
You are a senior data steward enriching an ODCS v3 data contract.

You receive per-column EVIDENCE (deterministic verdict, engine hits/scores, profiling,
similar past reviews, optional sample values) plus optional context documents.

Your job: return a strict ENRICHMENT DELTA containing ONLY the fields this stage
requests (see STAGE=… below). Do NOT re-emit schema, quality rules, telemetry,
provenance, or the full contract.
"""

MULTISTEP_SHARED_OUTPUT = """\
## 4 — Output format (DELTA ONLY)

Return ONLY JSON (no markdown fences). Emit only the keys this STAGE allows:

{
  "table": {
    "description": "1–3 sentences: business purpose, grain (one row = …), key entities, time scope.",
    "purpose": "optional shorter purpose phrase"
  },
  "table_tags": ["telecom", "customer"],
  "columns": {
    "<column_name>": {
      "businessName": "Optional display name",
      "business": {
        "definition": "...",
        "synonyms": ["..."],
        "example_values": ["+20 1# ### ####"],
        "tags": ["contact"]
      },
      "pii": {
        "classification": "pii_personal|pii_sensitive|none",
        "entity_type": "PHONE_NUMBER",
        "reason": "optional one-line override rationale"
      },
      "tags": ["contact"]
    }
  }
}

STAGE=column_definitions → columns[].businessName/business/tags only (no table, no pii).
STAGE=classification_pii → columns[].pii/tags only (no table, no business rewrite).
STAGE=table_definition → table + table_tags only (no columns). `table.description` is required.
STAGE=contract_review → corrections only; empty columns/table is valid when there are no findings.

GOLD STANDARD (merged result for one column — imitate this shape):
  name: merchant_mobile
  entity_type: PHONE_NUMBER
  classification: pii_personal
  tags: [pii, gdpr_personal_data, contact]
  business:
    definition: The merchant's primary mobile number for verification and SMS.
    synonyms: [mobile, msisdn, contact_number]
    example_values: ["+20 1# ### ####"]
    tags: [contact, identifier]
  businessName: Merchant mobile number
  (NO quality block on PII columns. NO provenance/confidence/scores.)

❌ DO NOT: emit quality rules, min/max/valid-values, telemetry, raw phone/email/ID values,
   or re-output the full contract YAML.

NEVER output quality rules — business enrichment only.
"""

CONTRACT_REVIEW_PROMPT = """\
## 8 — Whole-contract review (after all stages)

You receive the fully enriched contract. Review for consistency and return ONLY
corrections plus an optional findings list.

Checks:
  • missing or placeholder column definitions
  • definitions that contradict the assigned PII classification
  • table description inconsistent with the column set or grain
  • tag vocabulary drift across columns
  • leftover raw sample values in definitions or example_values

Return JSON (no markdown fences):

{
  "review": {
    "findings": [
      {"severity": "warning", "target": "column:msisdn", "message": "one-line reason"}
    ]
  },
  "table": {"description": "optional corrected description"},
  "table_tags": ["optional"],
  "columns": {
    "<column_name>": {
      "businessName": "optional",
      "business": {"definition": "..."},
      "pii": {"classification": "pii_personal|pii_sensitive|none", "reason": "..."},
      "tags": ["optional"]
    }
  }
}

If the contract is consistent, return {"review": {"findings": []}} with no table/columns.
Every change MUST have a one-line reason (pii.reason or a finding message).
Do not invent columns. Do not re-emit schema, quality rules, or the full contract.
"""

_FLAT_PARTS = dict(ENRICH_PROMPT_PARTS)

ENRICH_NESTED_PROMPT_PARTS: tuple[tuple[str, str], ...] = (
    *[(f"normal/{name}", text) for name, text in ENRICH_PROMPT_PARTS],
    ("multistep/shared/01_role.md", MULTISTEP_SHARED_ROLE),
    ("multistep/shared/03_tags.md", _FLAT_PARTS["03_tags.md"]),
    ("multistep/shared/06_output_format.md", MULTISTEP_SHARED_OUTPUT),
    (
        "multistep/steps/column_definitions/02_business_definitions.md",
        _FLAT_PARTS["02_business_definitions.md"],
    ),
    (
        "multistep/steps/classification_pii/04_pii_review.md",
        _FLAT_PARTS["04_pii_review.md"],
    ),
    (
        "multistep/steps/classification_pii/05_classification.md",
        _FLAT_PARTS["05_classification.md"],
    ),
    (
        "multistep/steps/table_definition/07_table_definition.md",
        _FLAT_PARTS["07_table_definition.md"],
    ),
    ("multistep/steps/contract_review/08_contract_review.md", CONTRACT_REVIEW_PROMPT),
)

_ALL_BUILTIN_PARTS: dict[str, str] = {
    **_FLAT_PARTS,
    **dict(ENRICH_NESTED_PROMPT_PARTS),
}


def builtin_enrichment_system_prompt() -> str:
    """Compose the shipped enrichment system prompt (no disk I/O)."""
    return "\n\n".join(text.strip() + "\n" for _, text in ENRICH_PROMPT_PARTS).rstrip() + "\n"


def default_prompts_dir() -> Path:
    env = os.environ.get("REDIBIS_PROMPTS_DIR")
    if env:
        return Path(env).expanduser()
    configs = os.environ.get("REDIBIS_CONFIGS_DIR", "./configs")
    return Path(configs).expanduser() / "prompts"


def _validate_name(name: str) -> str:
    """Validate a prompt relative path (flat ``01_role.md`` or nested)."""
    n = (name or "").strip().replace("\\", "/")
    if n.startswith("/") or n.startswith("../") or "/../" in f"/{n}/" or n.endswith("/.."):
        raise ValueError(f"invalid prompt filename: {name!r}")
    parts = [p for p in n.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"invalid prompt filename: {name!r}")
    for seg in parts[:-1]:
        if not _SAFE_SEGMENT.match(seg):
            raise ValueError(f"invalid prompt filename: {name!r}")
    if not _SAFE_FILE.match(parts[-1]):
        raise ValueError(f"invalid prompt filename: {name!r}")
    return "/".join(parts)


def _layer_for(name: str) -> str:
    if name.startswith("normal/"):
        return "normal"
    if name.startswith("multistep/shared/"):
        return "multistep_shared"
    if name.startswith("multistep/steps/"):
        bits = name.split("/")
        if len(bits) >= 4:
            return f"multistep_step:{bits[2]}"
        return "multistep"
    return "shared"


class EnrichPromptStore:
    """Disk-backed editable enrichment prompt parts."""

    def __init__(self, root: Optional[PathLike] = None) -> None:
        self.root = Path(root).expanduser() if root else default_prompts_dir()
        self.enrich_dir = self.root / "enrich"

    def ensure_seeded(self) -> None:
        """Create missing builtin parts under the live prompts dir.

        Flat files are always filled in when missing. Nested mode/stage files are
        seeded only for a brand-new store or when nested directories already
        exist, so an upgraded install with operator-edited flat files is left
        alone.
        """
        self.enrich_dir.mkdir(parents=True, exist_ok=True)
        had_flat = any(self.enrich_dir.glob("*.md"))
        had_nested = (self.enrich_dir / "normal").is_dir() or (
            self.enrich_dir / "multistep"
        ).is_dir()
        for name, text in ENRICH_PROMPT_PARTS:
            path = self.enrich_dir / name
            if not path.is_file():
                path.write_text(text.strip() + "\n", encoding="utf-8")
        if had_nested or not had_flat:
            for rel, text in ENRICH_NESTED_PROMPT_PARTS:
                path = self.enrich_dir / rel
                if not path.is_file():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text.strip() + "\n", encoding="utf-8")

    def list_files(self) -> list[str]:
        self.ensure_seeded()
        names: list[str] = []
        if not self.enrich_dir.is_dir():
            return names
        for p in self.enrich_dir.rglob("*.md"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.enrich_dir).as_posix()
            try:
                _validate_name(rel)
            except ValueError:
                continue
            names.append(rel)
        return sorted(names)

    def read(self, name: str) -> str:
        self.ensure_seeded()
        path = self.enrich_dir / _validate_name(name)
        if not path.is_file():
            raise FileNotFoundError(f"prompt not found: {name}")
        return path.read_text(encoding="utf-8")

    def write(self, name: str, content: str) -> dict[str, Any]:
        self.ensure_seeded()
        safe = _validate_name(name)
        path = self.enrich_dir / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (content or "").replace("\r\n", "\n")
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text, encoding="utf-8")
        return {"name": safe, "chars": len(text), "path": str(path)}

    def delete(self, name: str) -> bool:
        safe = _validate_name(name)
        path = self.enrich_dir / safe
        if not path.is_file():
            return False
        remaining = [n for n in self.list_files() if n != safe]
        if not remaining:
            raise ValueError("cannot delete the last enrichment prompt file")
        path.unlink()
        return True

    def reset_to_builtin(self, name: Optional[str] = None) -> list[str]:
        """Rewrite one or all parts from the shipped builtins."""
        self.enrich_dir.mkdir(parents=True, exist_ok=True)
        if name:
            safe = _validate_name(name)
            if safe not in _ALL_BUILTIN_PARTS:
                raise ValueError(f"no builtin prompt named {safe!r}")
            dest = self.enrich_dir / safe
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_ALL_BUILTIN_PARTS[safe].strip() + "\n", encoding="utf-8")
            return [safe]
        written: list[str] = []
        for part_name, text in ENRICH_PROMPT_PARTS:
            (self.enrich_dir / part_name).write_text(text.strip() + "\n", encoding="utf-8")
            written.append(part_name)
        for rel, text in ENRICH_NESTED_PROMPT_PARTS:
            dest = self.enrich_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text.strip() + "\n", encoding="utf-8")
            written.append(rel)
        return written

    def _files_for_mode(self, mode: str = "normal", stage_kind: Optional[str] = None) -> list[str]:
        names = self.list_files()
        nested_normal = [n for n in names if n.startswith("normal/")]
        nested_ms = [n for n in names if n.startswith("multistep/")]
        flat = [n for n in names if "/" not in n]
        mode_n = (mode or "normal").strip().lower()
        if mode_n == "multistep":
            if nested_ms:
                selected = [n for n in nested_ms if n.startswith("multistep/shared/")]
                kind = (stage_kind or "").strip().lower()
                if kind:
                    prefix = f"multistep/steps/{kind}/"
                    selected.extend(n for n in nested_ms if n.startswith(prefix))
                return sorted(selected)
            return sorted(flat)
        if nested_normal:
            return sorted(nested_normal)
        return sorted(flat)

    def compose(self, *, mode: str = "normal", stage_kind: Optional[str] = None) -> str:
        """Join enrichment MD parts for ``mode`` (and optional multistep stage)."""
        parts = []
        for name in self._files_for_mode(mode, stage_kind):
            text = self.read(name).strip()
            if text:
                parts.append(text)
        if not parts:
            return builtin_enrichment_system_prompt()
        return "\n\n".join(parts).rstrip() + "\n"

    def snapshot(self) -> dict[str, Any]:
        files = []
        for name in self.list_files():
            content = self.read(name)
            files.append(
                {
                    "name": name,
                    "chars": len(content),
                    "content": content,
                    "builtin": name in _ALL_BUILTIN_PARTS,
                    "layer": _layer_for(name),
                }
            )
        composed = self.compose(mode="normal")
        return {
            "root": str(self.enrich_dir),
            "files": files,
            "composed_chars": len(composed),
            "composed": composed,
        }

    def export_pack_sections(self) -> dict[str, str]:
        """Map pack-relative paths → markdown text for ``.rdbpack`` export."""
        out: dict[str, str] = {}
        for name in self.list_files():
            out[f"{PACK_ENRICH_PROMPT_PREFIX}{name}"] = self.read(name)
        out["assets/prompts/enrichment_system.md"] = self.compose(mode="normal")
        return out

    def import_pack_files(
        self,
        files: Mapping[str, bytes | str],
        *,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Apply ``assets/prompts/enrich/**/*.md`` (and legacy single-file) from a pack.

        ``replace=True`` (default) clears existing enrich parts before writing
        pack files so Settings mirrors the pack exactly.
        """
        enrich_parts: dict[str, str] = {}
        for rel, raw in files.items():
            rel_n = str(rel).replace("\\", "/")
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if rel_n.startswith(PACK_ENRICH_PROMPT_PREFIX) and rel_n.endswith(".md"):
                rest = rel_n[len(PACK_ENRICH_PROMPT_PREFIX) :]
                try:
                    name = _validate_name(rest)
                except ValueError:
                    continue
                enrich_parts[name] = text
            elif rel_n == "assets/prompts/enrichment_system.md" and not enrich_parts:
                enrich_parts["01_role.md"] = text

        if not enrich_parts:
            return {"updated": [], "replaced": False, "count": 0}

        self.enrich_dir.mkdir(parents=True, exist_ok=True)
        if replace:
            for existing in self.enrich_dir.rglob("*.md"):
                if existing.is_file():
                    existing.unlink()
        updated: list[str] = []
        for name, text in sorted(enrich_parts.items()):
            self.write(name, text)
            updated.append(name)
        return {
            "updated": updated,
            "replaced": bool(replace),
            "count": len(updated),
            "root": str(self.enrich_dir),
        }


def get_enrich_prompt_store(root: Optional[PathLike] = None) -> EnrichPromptStore:
    return EnrichPromptStore(root=root)


def resolve_enrichment_system_prompt(
    *,
    root: Optional[PathLike] = None,
    mode: str = "normal",
    stage_kind: Optional[str] = None,
) -> str:
    """Live enrichment system prompt (Settings-backed, seeded from builtins)."""
    try:
        return get_enrich_prompt_store(root).compose(mode=mode, stage_kind=stage_kind)
    except OSError:
        return builtin_enrichment_system_prompt()
