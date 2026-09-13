"""Corpus patches from 'accept as expected' / mark-advisory UI actions."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def apply_corpus_patch(dataset: Mapping[str, Any], actions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a new dataset with accepted spans rewritten. Production rules untouched."""
    cases = [dict(c) for c in (dataset.get("cases") or [])]
    by_id = {str(c.get("id") or ""): i for i, c in enumerate(cases)}
    applied: list[dict[str, Any]] = []
    for raw in actions:
        action = str(raw.get("action") or "").strip()
        case_id = str(raw.get("case_id") or "").strip()
        if case_id not in by_id:
            continue
        case = dict(cases[by_id[case_id]])
        spans = [dict(s) for s in (case.get("expected_spans") or [])]
        if action == "accept_as_expected":
            span = dict(raw.get("span") or {})
            start, end = int(span["start"]), int(span["end"])
            entity = str(span.get("entity_type") or "").upper()
            replaced = False
            target_id = str(raw.get("expected_id") or span.get("id") or "")
            for item in spans:
                if target_id and str(item.get("id") or "") == target_id:
                    item["start"] = start
                    item["end"] = end
                    if entity:
                        item["entity_type"] = entity
                    replaced = True
                    break
            if not replaced:
                span_out = {
                    "id": target_id or f"s{len(spans) + 1}",
                    "start": start,
                    "end": end,
                    "entity_type": entity or "PERSON",
                    "grade": "strict",
                }
                spans.append(span_out)
            case["expected_spans"] = spans
            cases[by_id[case_id]] = case
            applied.append({"action": action, "case_id": case_id})
        elif action == "mark_advisory":
            target_id = str(raw.get("expected_id") or "")
            note = str(raw.get("note") or raw.get("defect") or "marked advisory")
            for item in spans:
                if not target_id or str(item.get("id") or "") == target_id:
                    item["grade"] = "advisory"
                    item["defect"] = note
                    if target_id:
                        break
            case["expected_spans"] = spans
            cases[by_id[case_id]] = case
            applied.append({"action": action, "case_id": case_id})
        elif action in {"exclude_term", "context_cue", "number_rule"}:
            applied.append({"action": action, "draft": True, **dict(raw)})
    out = dict(dataset)
    out["cases"] = cases
    return {"dataset": out, "applied": applied, "kind": "redibis.text_span_eval_corpus_patch"}


def draft_rules_from_actions(actions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a draft TextRuleOverlay mapping. Never writes production rules."""
    exclude: list[str] = []
    cues: dict[str, dict[str, Any]] = {}
    patterns: dict[str, Any] = {}
    for raw in actions:
        action = str(raw.get("action") or "")
        if action == "exclude_term" and raw.get("term"):
            exclude.append(str(raw["term"]))
        elif action == "context_cue":
            entity = str(raw.get("entity_type") or "").upper()
            trigger = str(raw.get("trigger") or raw.get("term") or "")
            if entity and trigger:
                bucket = cues.setdefault(entity, {"triggers": []})
                bucket["triggers"].append(trigger)
                if raw.get("extend"):
                    bucket["extend"] = str(raw["extend"])
        elif action == "number_rule" and raw.get("pattern"):
            name = str(raw.get("name") or f"draft_{len(patterns) + 1}")
            patterns[name] = {
                "pattern": str(raw["pattern"]),
                "entity_type": str(raw.get("entity_type") or "PHONE_NUMBER").upper(),
                "recognizer_group": "free_text",
                "presidio_score": float(raw.get("score") or 0.8),
            }
    return {
        "kind": "redibis.text_rules_draft",
        "exclude_terms": exclude,
        "context_cues": cues,
        "patterns": {"add": patterns, "remove": [], "replace_all": False},
    }
