"""PII regex/NER tuning reports and LLM agent bundles."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from redibis.models import PIIDetection
from redibis.pii.export import export_ner_models, export_regex_catalog
from redibis.pii.regex_catalog import build_effective_catalog
from redibis.pii.regex_overrides import RegexOverrides


_TUNING_PROMPT = """# PII tuning request

Given the attached catalogue, NER inventory, and firing statistics, propose:

1. **Regex to add** — name, pattern, entity_type, recognizer_group, presidio_score, validator?
2. **Patterns to disable** — over-firing or false-positive pattern names (via RegexOverrides.remove)
3. **NER entities to add** — new label phrases for GLiNER
4. **Label groups** — multiple NER passes for multi-entity columns, e.g. [["person","address"],["phone number"]]

Return a JSON object shaped like:
```json
{"add": {}, "remove": [], "ner_label_groups": []}
```

Do **not** include raw data values or sample cell contents.
"""


def build_regex_tuning_report(
    detections: list[PIIDetection],
    *,
    overrides: RegexOverrides | None = None,
    over_fire_column_threshold: int = 5,
) -> dict[str, Any]:
    """Aggregate regex_hits across detections for LLM/agent tuning signal."""
    catalog = build_effective_catalog(overrides)
    pattern_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "fire_count": 0,
            "columns_hit": set(),
            "scores": [],
            "match_rates": [],
            "entity_type": "",
        }
    )

    for det in detections:
        for hit in det.regex_hits or []:
            name = hit.get("pattern_name") or ""
            if not name:
                continue
            ps = pattern_stats[name]
            ps["fire_count"] += 1
            ps["columns_hit"].add(det.column)
            ps["scores"].append(float(hit.get("score") or 0))
            ps["match_rates"].append(float(hit.get("match_rate") or 0))
            ps["entity_type"] = hit.get("entity_type") or ps["entity_type"]

    fired: dict[str, Any] = {}
    for name, ps in pattern_stats.items():
        cols = ps["columns_hit"]
        fired[name] = {
            "fire_count": ps["fire_count"],
            "columns_hit": sorted(cols),
            "column_count": len(cols),
            "avg_score": round(sum(ps["scores"]) / max(len(ps["scores"]), 1), 4),
            "avg_match_rate": round(
                sum(ps["match_rates"]) / max(len(ps["match_rates"]), 1), 4
            ),
            "entity_type": ps["entity_type"],
            "collision_group": getattr(catalog.get(name), "collision_group", None),
        }

    never_fired = [
        name for name, entry in catalog.items()
        if entry.active and name not in fired
    ]
    over_firing = [
        name for name, stats in fired.items()
        if stats["column_count"] >= over_fire_column_threshold
        and stats["avg_score"] < 0.85
    ]
    missing_pattern_candidates = [
        {
            "column": det.column,
            "entity_type": det.entity_type,
            "regex_hits": len(det.regex_hits or []),
            "presidio_score": det.presidio_score,
            "detected": det.detected,
        }
        for det in detections
        if (det.regex_hits or det.presidio_score) and not det.detected
    ]

    return {
        "fired": fired,
        "never_fired": never_fired,
        "over_firing": over_firing,
        "missing_pattern_candidates": missing_pattern_candidates,
        "columns_scanned": len(detections),
    }


def build_pii_tuning_bundle(
    detections: list[PIIDetection],
    out_dir: str | Path,
    *,
    table: str = "",
    overrides: RegexOverrides | None = None,
    models_dir: str | None = None,
) -> Path:
    """Write catalog + NER inventory + firing report + LLM prompt scaffold."""
    root = Path(out_dir) / "pii_tuning_bundle"
    root.mkdir(parents=True, exist_ok=True)

    catalog_json = export_regex_catalog(overrides=overrides, fmt="json")
    (root / "catalog.json").write_text(
        catalog_json if isinstance(catalog_json, str) else json.dumps(catalog_json),
        encoding="utf-8",
    )
    (root / "ner_models.json").write_text(
        json.dumps(export_ner_models(models_dir), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report = build_regex_tuning_report(detections, overrides=overrides)
    if table:
        report["table"] = table
    (root / "firing_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=list),
        encoding="utf-8",
    )
    (root / "tuning_prompt.md").write_text(_TUNING_PROMPT, encoding="utf-8")
    (root / "apply_template.json").write_text(
        json.dumps({"add": {}, "remove": [], "ner_label_groups": []}, indent=2),
        encoding="utf-8",
    )
    return root
