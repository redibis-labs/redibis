"""
Per-column detection evidence for enrichment context (G2).

Merges metadata telemetry, run-bucket PII detections, profiling stats,
similar-column memory, and optional sample values into a single LLM context
payload per column.
"""

from __future__ import annotations

from typing import Any, Optional

_MAX_HITS = 10
_MAX_SAMPLE = 5


def load_run_detection_index(
    backend,
    bucket: str,
    table: str,
    *,
    workflows: tuple[str, ...] = ("scan", "pii", "ge"),
) -> dict[str, dict]:
    """Load regex/ner hits from the newest run's ``pii_detections.json``."""
    table_safe = table.replace(".", "_")
    best_key: Optional[str] = None
    for workflow in workflows:
        prefix = f"{workflow}/{table_safe}/"
        for key in backend.list_keys(bucket, prefix=prefix):
            if key.endswith("/pii_detections.json"):
                if best_key is None or key > best_key:
                    best_key = key
    if not best_key:
        return {}
    try:
        rows = backend.get_json(bucket, best_key)
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    index: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        col = row.get("column")
        if not col:
            continue
        index[str(col)] = {
            "regex_hits": _cap_hits(row.get("regex_hits")),
            "ner_hits": _cap_hits(row.get("ner_hits")),
            "presidio_score": row.get("presidio_score"),
            "gliner_score": row.get("gliner_score"),
            "phone_score": row.get("phone_score"),
            "phone_valid_rate": row.get("phone_valid_rate") or row.get("msisdn_valid_rate"),
            "entity_type": row.get("entity_type"),
            "detected": row.get("detected"),
            "confidence": row.get("confidence"),
            "decision_rule": row.get("decision_rule"),
            "run_artifact": best_key,
        }
    return index


def assemble_column_evidence(
    column: str,
    prop: dict,
    telemetry: dict,
    *,
    run_detection: Optional[dict] = None,
) -> dict[str, Any]:
    """Build enrichment evidence: detections, profile stats, edge-rule verdict."""
    from redibis.contracts.privacy import col_pii_engine

    ce = col_pii_engine(prop) if prop else {}
    evidence: dict[str, Any] = {}

    if telemetry:
        evidence["telemetry"] = {
            k: telemetry[k]
            for k in (
                "entity_type", "confidence", "discovery_engines", "decision_rule",
                "edge_rule", "arabic_aware", "run_id",
            )
            if k in telemetry
        }

    det = run_detection or {}
    regex_hits = det.get("regex_hits") or telemetry.get("regex_hits") or []
    ner_hits = det.get("ner_hits") or telemetry.get("ner_hits") or []
    if regex_hits:
        evidence["regex_hits"] = _cap_hits(regex_hits)
    if ner_hits:
        evidence["ner_hits"] = _cap_hits(ner_hits)

    scores = {
        k: det.get(k) or telemetry.get(k)
        for k in ("presidio_score", "gliner_score", "phone_score", "confidence")
        if (det.get(k) is not None or telemetry.get(k) is not None)
    }
    if scores:
        evidence["scores"] = scores

    profile: dict[str, Any] = {}
    if prop.get("logicalType"):
        profile["logicalType"] = prop["logicalType"]
    if prop.get("physicalType"):
        profile["physicalType"] = prop["physicalType"]
    quality = prop.get("quality")
    if isinstance(quality, list) and quality:
        profile["quality_rule_count"] = len(quality)
    if ce.get("entity_type"):
        profile["entity_type"] = ce.get("entity_type")
    if profile:
        evidence["profile"] = profile

    edge = (
        telemetry.get("edge_rule")
        or telemetry.get("decision_rule")
        or det.get("edge_rule_verdict")
        or det.get("decision_rule")
    )
    if edge:
        evidence["edge_rule_verdict"] = edge

    if det.get("run_artifact"):
        evidence["source_run"] = det["run_artifact"]

    return evidence


def build_profiling_context(
    prop: dict,
    telemetry: dict,
    *,
    sample: Optional[list] = None,
) -> dict[str, Any]:
    """Structural profiling stats safe for LLM context (no raw min/max values)."""
    profiling: dict[str, Any] = {}
    fp = telemetry.get("fingerprint") if isinstance(telemetry.get("fingerprint"), dict) else {}

    for key in (
        "null_rate", "unique_ratio", "distinct_count", "min_len", "max_len",
        "top_formats", "cardinality_ratio", "avg_value_length", "avg_len",
        "constant", "sample_count",
    ):
        if key in telemetry and telemetry[key] is not None:
            profiling[key] = telemetry[key]

    if fp:
        if "min_len" not in profiling and fp.get("len_min") is not None:
            profiling["min_len"] = int(fp["len_min"])
        if "max_len" not in profiling and fp.get("len_max") is not None:
            profiling["max_len"] = int(fp["len_max"])
        if "avg_len" not in profiling and fp.get("len_mean") is not None:
            profiling["avg_len"] = round(float(fp["len_mean"]), 2)
        if "null_rate" not in profiling and fp.get("null_rate") is not None:
            profiling["null_rate"] = round(float(fp["null_rate"]), 4)
        if "unique_ratio" not in profiling and fp.get("distinct_ratio") is not None:
            profiling["unique_ratio"] = round(float(fp["distinct_ratio"]), 4)
        if "distinct_count" not in profiling and fp.get("distinct_ratio") is not None:
            profiling["distinct_count"] = int(round(float(fp["distinct_ratio"]) * max(fp.get("sample_size") or 1, 1)))
        if "top_formats" not in profiling and fp.get("pattern_masks"):
            profiling["top_formats"] = _formats_from_pattern_masks(fp["pattern_masks"])
        if "char_classes" not in profiling:
            cc = _char_classes_from_fingerprint(fp)
            if cc:
                profiling["char_classes"] = cc

    if "char_classes" not in profiling:
        cc = _char_classes_from_telemetry(telemetry)
        if cc:
            profiling["char_classes"] = cc

    if "avg_len" not in profiling and profiling.get("avg_value_length") is not None:
        profiling["avg_len"] = profiling.pop("avg_value_length")

    sample_vals = sample if sample else telemetry.get("sample")
    if isinstance(sample_vals, list) and sample_vals:
        _merge_sample_profiling(profiling, sample_vals)
        profiling.setdefault("sample_count", len(sample_vals))

    quality = prop.get("quality") or []
    if isinstance(quality, list):
        if quality:
            profiling.setdefault("quality_rule_count", len(quality))
        for rule in quality:
            if not isinstance(rule, dict):
                continue
            impl = rule.get("implementation") or {}
            etype = str(impl.get("expectation_type") or "")
            kwargs = impl.get("kwargs") or {}
            if "missing" in etype.lower() or rule.get("rule") == "missingCount":
                if "mostly" in kwargs and "null_rate" not in profiling:
                    profiling["null_rate"] = round(1.0 - float(kwargs["mostly"]), 4)
            if "unique" in etype.lower():
                if "mostly" in kwargs and "unique_ratio" not in profiling:
                    profiling["unique_ratio"] = round(float(kwargs["mostly"]), 4)
            if "value_lengths" in etype.lower():
                mn = kwargs.get("min_value")
                mx = kwargs.get("max_value")
                if mn is not None and "min_len" not in profiling:
                    profiling["min_len"] = int(mn)
                if mx is not None and "max_len" not in profiling:
                    profiling["max_len"] = int(mx)

    if prop.get("logicalType"):
        profiling.setdefault("logicalType", prop["logicalType"])
    if prop.get("physicalType"):
        profiling.setdefault("physicalType", prop["physicalType"])

    return profiling


def build_engine_evidence(
    prop: dict,
    telemetry: dict,
    *,
    run_detection: Optional[dict] = None,
) -> dict[str, Any]:
    """Per-engine signals from PIIDetection / telemetry (no raw cell values)."""
    det = run_detection or {}
    out: dict[str, Any] = {}

    regex_hits = det.get("regex_hits") or telemetry.get("regex_hits") or []
    ner_hits = det.get("ner_hits") or telemetry.get("ner_hits") or []
    if regex_hits:
        out["regex_hits"] = _cap_hits(regex_hits)
    if ner_hits:
        out["ner_hits"] = _cap_hits(ner_hits)

    phone_rate = (
        det.get("phone_valid_rate")
        or det.get("msisdn_valid_rate")
        or telemetry.get("phone_valid_rate")
        or telemetry.get("msisdn_valid_rate")
    )
    mobile_rate = det.get("phone_mobile_rate") or telemetry.get("phone_mobile_rate")
    regions = det.get("phone_regions") or telemetry.get("phone_regions")
    if phone_rate is not None or mobile_rate is not None or regions:
        phone_ev: dict[str, Any] = {}
        if phone_rate is not None:
            phone_ev["valid_rate"] = phone_rate
        if mobile_rate is not None:
            phone_ev["mobile_rate"] = mobile_rate
        if regions:
            phone_ev["regions"] = regions
        out["phone"] = phone_ev
    elif det.get("phone_score") is not None or telemetry.get("phone_score") is not None:
        score = det.get("phone_score") if det.get("phone_score") is not None else telemetry.get("phone_score")
        out["phone"] = {"score": score}

    scores = {}
    for key in ("presidio_score", "gliner_score", "phone_score", "confidence"):
        val = det.get(key) if det.get(key) is not None else telemetry.get(key)
        if val is not None:
            scores[key] = val
    if scores:
        out["scores"] = scores

    return out


def build_deterministic_verdict(prop: dict, telemetry: dict) -> dict[str, Any]:
    """What redibis decided + why."""
    from redibis.contracts.privacy import col_pii_engine

    ce = col_pii_engine(prop)
    verdict: dict[str, Any] = {
        "entity_type": ce.get("entity_type") or prop.get("entity_type"),
        "classification": prop.get("classification"),
        "tags": list(prop.get("tags") or []),
        "decision_rule": (
            telemetry.get("decision_rule")
            or ce.get("decision_rule")
            or telemetry.get("edge_rule")
        ),
        "confidence": ce.get("confidence") or telemetry.get("confidence"),
    }
    return {k: v for k, v in verdict.items() if v is not None and v != []}


def build_full_column_context(
    prop: dict,
    telemetry: dict,
    *,
    run_detection: Optional[dict] = None,
    similar_columns: Optional[list[dict]] = None,
    sample: Optional[list] = None,
    sample_policy: str = "raw",
    policy: Any = None,
    table: str = "",
) -> dict[str, Any]:
    """Single column payload for the enrichment LLM (R2 schema)."""
    name = prop.get("name")
    deterministic_reasoning = None
    if policy is not None and table and name:
        from redibis.classification.reasoning import build_deterministic_reasoning

        deterministic_reasoning = build_deterministic_reasoning(
            prop,
            telemetry,
            policy,
            table,
            run_detection=run_detection,
        )
    ctx: dict[str, Any] = {
        "name": name,
        "logicalType": prop.get("logicalType"),
        "physicalType": prop.get("physicalType"),
        "deterministic_verdict": build_deterministic_verdict(prop, telemetry),
        "engine_evidence": build_engine_evidence(prop, telemetry, run_detection=run_detection),
        "profiling": build_profiling_context(prop, telemetry, sample=sample),
        "similar_columns": list(similar_columns or []),
    }
    if deterministic_reasoning:
        ctx["deterministic_reasoning"] = deterministic_reasoning
    if sample_policy != "none" and sample:
        ctx["sample"] = list(sample)[:_MAX_SAMPLE]
        ctx["sample_policy"] = sample_policy
    elif sample_policy != "none":
        ctx["sample_policy"] = sample_policy
    return ctx


def group_similar_columns_by_name(flat_neighbors: list[dict]) -> dict[str, list[dict]]:
    """Index table-level similar-column hits by source column name."""
    grouped: dict[str, list[dict]] = {}
    for row in flat_neighbors:
        source_col = row.get("column")
        if not source_col:
            continue
        tc = str(row.get("table_column") or "")
        if "." in tc:
            parts = tc.split(".")
            neighbor_col = parts[-1]
            neighbor_table = ".".join(parts[:-1]) if len(parts) > 1 else ""
        else:
            neighbor_table, neighbor_col = "", tc
        entry = {
            "table": neighbor_table,
            "column": neighbor_col,
            "score": row.get("score"),
            "prior_decision": row.get("prior_decision") or row.get("prior_pii_verdict"),
            "entity": (
                (row.get("fingerprint") or {}).get("entity_type")
                or row.get("prior_classification")
            ),
        }
        grouped.setdefault(str(source_col), []).append(entry)
    return grouped


def resolve_column_sample(
    *,
    column: str,
    telemetry: dict,
    bundle_sample: Optional[list] = None,
    masked_samples: Optional[dict[str, list]] = None,
    sample_policy: str,
) -> Optional[list]:
    """Pick input sample values per sample_policy (masking is caller's job)."""
    if sample_policy == "none":
        return None
    if sample_policy == "masked":
        if masked_samples and column in masked_samples:
            return masked_samples[column]
        return None
    # raw — local LLM default
    if bundle_sample:
        return bundle_sample
    raw = telemetry.get("sample")
    if isinstance(raw, list):
        return raw
    return None


def _cap_hits(hits: Any) -> list:
    if not isinstance(hits, list):
        return []
    return hits[:_MAX_HITS]


def _formats_from_pattern_masks(masks: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(masks, list):
        return out
    for item in masks[:5]:
        if isinstance(item, (list, tuple)) and item:
            out.append(str(item[0]))
        elif isinstance(item, str):
            out.append(item)
    return out


def _char_classes_from_fingerprint(fp: dict) -> dict[str, float]:
    keys = (
        ("digit_share", "frac_digit"),
        ("alpha_share", "frac_alpha"),
        ("punct_share", "frac_punct"),
        ("space_share", "frac_space"),
        ("arabic_share", "frac_arabic"),
    )
    cc: dict[str, float] = {}
    for out_key, fp_key in keys:
        val = fp.get(fp_key)
        if val is not None:
            cc[out_key] = round(float(val), 4)
    return cc


def _char_classes_from_telemetry(telemetry: dict) -> dict[str, float]:
    cc = telemetry.get("char_classes")
    if isinstance(cc, dict) and cc:
        return {k: round(float(v), 4) for k, v in cc.items() if v is not None}
    mapping = (
        ("digit_share", "frac_digit"),
        ("alpha_share", "frac_alpha"),
        ("punct_share", "frac_punct"),
    )
    out: dict[str, float] = {}
    for out_key, tel_key in mapping:
        if tel_key in telemetry and telemetry[tel_key] is not None:
            out[out_key] = round(float(telemetry[tel_key]), 4)
    return out


def _value_format_mask(val: str) -> str:
    out: list[str] = []
    for ch in val:
        if ch.isdigit():
            out.append("#")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        else:
            out.append(ch)
    return "".join(out)


def _merge_sample_profiling(profiling: dict[str, Any], sample: list) -> None:
    str_vals = [str(v) for v in sample if v is not None and str(v).strip()]
    if not str_vals:
        return
    lengths = [len(v) for v in str_vals]
    profiling.setdefault("min_len", min(lengths))
    profiling.setdefault("max_len", max(lengths))
    profiling.setdefault("avg_len", round(sum(lengths) / len(lengths), 2))
    if "char_classes" not in profiling:
        total = sum(len(v) for v in str_vals) or 1
        digits = sum(sum(c.isdigit() for c in v) for v in str_vals)
        alpha = sum(sum(c.isalpha() for c in v) for v in str_vals)
        punct = sum(sum(not c.isalnum() and not c.isspace() for c in v) for v in str_vals)
        profiling["char_classes"] = {
            "digit_share": round(digits / total, 4),
            "alpha_share": round(alpha / total, 4),
            "punct_share": round(punct / total, 4),
        }
    if "top_formats" not in profiling:
        from collections import Counter

        fmt_counts = Counter(_value_format_mask(v) for v in str_vals)
        profiling["top_formats"] = [fmt for fmt, _ in fmt_counts.most_common(3)]
    profiling.setdefault("distinct_count", len(set(str_vals)))
    if "unique_ratio" not in profiling:
        profiling["unique_ratio"] = round(len(set(str_vals)) / len(str_vals), 4)
