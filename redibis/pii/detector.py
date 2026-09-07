from __future__ import annotations

import logging
import re
from typing import Callable, Optional, Any

import pandas as pd

from redibis.models import PIIDetection, ARABIC_UNICODE_RE
from redibis.pii.ner_backend import NERBackend, NERReport, model_slug
from redibis.pii.regex_catalog import (
    CATALOG, COLLISION_RESOLUTION_TABLE, catalog_validators,
)
from redibis.pii.regex_overrides import RegexOverrides

try:
    # Suppress benign ONNX Runtime GPU discovery warnings on CPU-only machines globally
    import onnxruntime as ort
    ort.set_default_logger_severity(3)  # 3 = ERROR (hides WARNINGs)
except ImportError:
    pass

logger = logging.getLogger("pii.layer3")

_NER_ENGINE_ALIASES = frozenset({"gliner", "ner"})

# Score added to a regex match when the column name contains one of the
# pattern's context_hints. Sized so a deliberately sub-floor pattern
# (e.g. generic date, base 0.50) clears the ≈0.80 regex floor only on a
# column-name context hit. See _run_presidio's column-name context boost.
_CONTEXT_BOOST = 0.40


def _engines_run_ner(engines: str) -> bool:
    normalized = (engines or "both").lower()
    return normalized in _NER_ENGINE_ALIASES or normalized == "both"


def _engines_run_regex(engines: str) -> bool:
    normalized = (engines or "both").lower()
    return normalized in ("regex", "both")


def _resolve_collision_suppressions(
    column_name: str,
) -> dict[str, str]:
    """
    For each collision group, pick the best-matching pattern key
    based on column name tokens. Returns {collision_group: winner_key}.
    """
    col_lower = column_name.lower()
    suppressions: dict[str, str] = {}

    for group_key, entries in COLLISION_RESOLUTION_TABLE.items():
        best_key = None
        best_score = 0
        for pattern_key, disambig_tokens in entries:
            if pattern_key.startswith("_suppress_"):
                continue
            score = sum(1 for t in disambig_tokens if t in col_lower)
            if score > best_score:
                best_score = score
                best_key = pattern_key
        if best_key and best_score > 0:
            suppressions[group_key] = best_key

    return suppressions


def _apply_context_boost(
    pattern_name: str | None,
    score: float,
    column_name: str,
    effective_catalog: dict,
    *,
    entity_tokens: dict | None = None,
) -> float:
    """Lift score when column name matches pattern + locale context tokens."""
    if score <= 0 or not pattern_name:
        return score
    entry = effective_catalog.get(pattern_name) or CATALOG.get(pattern_name)
    pattern_hints = tuple(getattr(entry, "context_hints", ()) or ()) if entry else ()
    entity_type = getattr(entry, "entity_type", None) if entry else None
    from redibis.pii.context_tokens import (
        builtin_context_tokens,
        column_matches_hints,
        effective_hints_for_pattern,
    )

    tokens_map = entity_tokens if entity_tokens is not None else builtin_context_tokens()
    hints = effective_hints_for_pattern(pattern_hints, entity_type, tokens_map)
    if not hints:
        return score
    if column_matches_hints(column_name, hints):
        return min(1.0, score + _CONTEXT_BOOST)
    return score


def _emit_regex_hit_decisions(
    *,
    table: str,
    column: str,
    regex_hits: list[dict],
    log_regex_hits: bool,
    max_logged: int,
) -> None:
    if not log_regex_hits or not regex_hits:
        return
    from redibis.obs import DecisionRecord, decision

    for hit in regex_hits[:max_logged]:
        decision(DecisionRecord(
            stage="pii",
            fn="detector._run_presidio",
            table=table,
            column=column,
            verdict=hit.get("entity_type") or "",
            confidence=float(hit.get("score") or 0.0),
            rule=(
                f'regex {hit["pattern_name"]} matched '
                f'{hit["match_rate"]:.0%} of sampled values'
            ),
            inputs={
                "pattern_name": hit["pattern_name"],
                "regex": hit.get("regex", ""),
                "match_rate": hit["match_rate"],
                "group": hit.get("group", ""),
                "collision_group": hit.get("collision_group"),
                "validator": hit.get("validator"),
            },
        ))


def _emit_ner_hit_decision(
    *,
    table: str,
    column: str,
    hit: dict,
) -> None:
    from redibis.obs import DecisionRecord, decision

    decision(DecisionRecord(
        stage="pii",
        fn="detector._run_ner",
        table=table,
        column=column,
        verdict=hit.get("label") or "",
        confidence=float(hit.get("score") or 0.0),
        rule=(
            f'NER {hit.get("model", "")} labels={hit.get("labels")} '
            f'matched {hit.get("match_rate", 0):.0%} of sampled values'
        ),
        inputs={
            "label": hit.get("label"),
            "labels": hit.get("labels"),
            "match_rate": hit.get("match_rate"),
            "score": hit.get("score"),
        },
    ))


def _resolve_ruleset(
    ruleset: object | None,
    regex_overrides: Optional[RegexOverrides] = None,
):
    """Pick the RuleSet whose patterns match the effective regex overrides."""
    from redibis.pii.rules.ruleset import RuleSet, RuleSetCompiler

    if isinstance(ruleset, RuleSet):
        if regex_overrides is None or regex_overrides == ruleset.regex_overrides:
            return ruleset
    return RuleSetCompiler.default(regex_overrides=regex_overrides)


def _run_presidio(
    values: list[str],
    group: str,
    arabic: bool,
    column_name: str,
    *,
    regex_overrides: Optional[RegexOverrides] = None,
    ruleset: object | None = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    msisdn_valid_rate: float = 0.0,
    geo_partner: Optional[str] = None,
    partner_values: Optional[list] = None,
    geo_require_pair: bool = True,
    geo_egypt_geofence: bool = False,
    entity_tokens: Optional[dict] = None,
    suppressed_entities: frozenset[str] | None = None,
) -> dict:
    """
    Run column regex detection via shared ``RegexRecognizer.recognize_values``.

    Returns {score, pattern, match_rate, entity_type, regex_hits}.
    """
    empty = {
        "score": None, "pattern": None, "match_rate": None,
        "entity_type": None, "regex_hits": [],
    }
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        logger.warning("presidio-analyzer not available; skipping Presidio path")
        return empty

    from redibis.pii.rules.recognizers import RecognizeContext, RegexRecognizer
    from redibis.pii.telecom_signals import postprocess_regex_hits

    rs = _resolve_ruleset(ruleset, regex_overrides)
    recognizer = RegexRecognizer(rs)
    suppressions = _resolve_collision_suppressions(column_name)
    ctx = RecognizeContext(group=group, arabic=arabic, column_name=column_name)

    n_recs = len(rs.patterns_for_group(group, arabic=arabic))
    msg = f"     ↳ Initialized Regex engine ({n_recs} patterns active)"
    logger.info(msg)
    if progress_callback:
        progress_callback(msg)

    aggregate = recognizer.recognize_values(
        values,
        ctx,
        collision_suppressions=suppressions,
        progress_callback=progress_callback,
    )

    regex_hits = list(aggregate.regex_hits)
    match_count = aggregate.match_count
    total = aggregate.total
    effective_catalog = dict(rs.patterns)

    # C1: negative token suppression — drop entities before postprocess.
    blocked = suppressed_entities or frozenset()
    if blocked:
        regex_hits = [
            h for h in regex_hits
            if (h.get("entity_type") or "") not in blocked
        ]

    regex_hits = postprocess_regex_hits(
        regex_hits,
        values,
        column_name,
        msisdn_valid_rate=msisdn_valid_rate,
        geo_partner=geo_partner,
        partner_values=partner_values,
        geo_require_pair=geo_require_pair,
        geo_egypt_geofence=geo_egypt_geofence,
    )

    best_pattern = regex_hits[0]["pattern_name"] if regex_hits else None
    best_score = regex_hits[0]["score"] if regex_hits else 0.0
    best_entity = regex_hits[0]["entity_type"] if regex_hits else None
    best_match_rate = regex_hits[0]["match_rate"] if regex_hits else match_count / total

    boosted = _apply_context_boost(
        best_pattern,
        best_score,
        column_name,
        effective_catalog,
        entity_tokens=entity_tokens,
    )
    if boosted != best_score and regex_hits:
        regex_hits[0]["score"] = round(boosted, 4)
        best_score = boosted
        regex_hits.sort(key=lambda x: x["score"], reverse=True)

    if best_score > 0:
        msg_match = (
            f"       ✅ Regex Matched: {best_pattern} "
            f"(Entity: {best_entity}, Score: {best_score:.2f})"
        )
        logger.info(msg_match)
        if progress_callback:
            progress_callback(msg_match)

    return {
        "score": best_score if best_score > 0 else None,
        "pattern": best_pattern,
        "match_rate": best_match_rate if best_score > 0 else match_count / total,
        "entity_type": best_entity,
        "regex_hits": regex_hits,
        "msisdn_valid_rate": msisdn_valid_rate,
        "geo_confidence": (
            regex_hits[0].get("geo_confidence") if regex_hits else None
        ),
    }


def _ner_hits_from_report(model: str, labels: list[str], report: NERReport) -> list[dict]:
    slug = model_slug(model)
    return [
        {
            "model": model,
            "model_slug": slug,
            "labels": list(labels),
            "label": h.label,
            "score": h.score,
            "match_rate": h.match_rate,
        }
        for h in report.hits
    ]


def _run_ner(
    backend: NERBackend,
    values: list[str],
    column_name: str,
    *,
    labels: list[str] | None = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> tuple[dict, list[dict]]:
    """Run one NER backend. Returns ({score,label,match_rate}, ner_hits)."""
    label_desc = f" labels={labels}" if labels else ""
    msg = f"     ↳ Running NER model ({backend.name}){label_desc} on '{column_name}'..."
    logger.info(msg)
    if progress_callback:
        progress_callback(msg)
    report = backend.analyze(values, column_name, labels=labels)
    result = report.to_result()
    hits = _ner_hits_from_report(backend.name, report.labels_requested, report)
    return {
        "score": result.get("score"),
        "label": result.get("label"),
        "match_rate": result.get("match_rate"),
    }, hits


def _run_ner_ensemble(
    ensemble,
    values: list[str],
    column_name: str,
    *,
    label_groups: Optional[list[list[str]]] = None,
    table: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> tuple[dict, list[dict]]:
    """Run model×label-group matrix (deep-scan / agentic)."""
    msg = f"     ↳ Running NER ensemble ({len(ensemble.backends)} models) on '{column_name}'..."
    logger.info(msg)
    if progress_callback:
        progress_callback(msg)
    passes = ensemble.run_column(values, column_name, label_groups=label_groups)
    ner_hits: list[dict] = []
    ner_result: dict = {"score": None, "label": None, "match_rate": None}
    primary_name = ensemble.backends[0].name if ensemble.backends else None

    for p in passes:
        if p.status != "ok" or p.report is None:
            continue
        for h in p.report.hits:
            hit = {
                "model": p.model,
                "model_slug": model_slug(p.model),
                "labels": list(p.labels),
                "label": h.label,
                "score": h.score,
                "match_rate": h.match_rate,
            }
            ner_hits.append(hit)
            _emit_ner_hit_decision(table=table, column=column_name, hit=hit)

    if primary_name:
        primary_best_score = 0.0
        for p in passes:
            if p.model != primary_name or p.status != "ok" or p.report is None:
                continue
            best = p.report.best()
            if best and best.score > primary_best_score:
                primary_best_score = best.score
                ner_result = {
                    "score": best.score,
                    "label": best.label,
                    "match_rate": best.match_rate,
                }
    return ner_result, ner_hits


def _detect_pii_impl(
    df: Any,
    columns: Optional[list[Any]] = None,
    engines: str = "both",
    gliner_always_run: bool = False,
    ner_always_run: bool = False,
    ner_backend: Optional[NERBackend] = None,
    ensemble=None,
    regex_overrides: Optional[RegexOverrides] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    table: str = "",
    log_samples: bool = False,
    log_regex_hits: bool = True,
    max_regex_hits_logged: int = 20,
    ner_label_groups: Optional[list[list[str]]] = None,
    entity_tokens: Optional[dict] = None,
    **kwargs,
) -> list[PIIDetection]:
    """Column evidence path (Plan B). Prefer ``ColumnScanner.detect`` / ``detect_pii``.

    Args:
        df: DataFrame (Pandas or Spark)
        columns: List of columns to scan (default: all)
        engines: "regex" | "gliner" | "ner" | "both" (default: "both")
        gliner_always_run: Deprecated alias for ``ner_always_run``
        ner_always_run: If True, always run NER even if regex score >= 0.80
        ner_backend: Loaded NER backend (from ``NERModelRegistry.try_load``)
        ensemble: ``NEREnsemble`` for deep-scan multi-model runs (optional)
        regex_overrides: Per-run regex catalog overrides
        ner_label_groups: Used only with ``ensemble`` (deep-scan / agentic)
        entity_tokens: Pack locale context tokens (entity → hints)
        table: Table name for structured decision logging.
        log_samples: When True, include truncated sample values in logs (default off).
        log_regex_hits: Emit per-pattern decision lines (default on).
        max_regex_hits_logged: Cap decision lines per column.
    """
    # Safely handle PySpark DataFrames
    if type(df).__name__ == "DataFrame" and hasattr(df, "limit"):
        try:
            df = df.limit(5000).toPandas()
        except Exception as e:
            logger.error(f"Failed to convert Spark DataFrame to Pandas: {e}")
            return []

    always_run_ner = ner_always_run or gliner_always_run
    arabic_pat = re.compile(ARABIC_UNICODE_RE)
    detections: list[PIIDetection] = []
    target_cols = columns if columns else list(df.columns)

    run_regex = _engines_run_regex(engines)
    run_ner = _engines_run_ner(engines)

    from redibis.config import resolve_pii_config
    from redibis.pii.telecom_signals import (
        apply_phone_phonenumbers_gate,
        compute_msisdn_valid_rate,
        compute_phone_plan_score,
        find_geo_partner_columns,
        presidio_phone_signal,
        resolve_entity_type,
    )
    from redibis.pii.phone_engine import phone_rates, should_run_phone
    from redibis.pii.national_id_egypt import (
        is_nid_entity,
        name_suggests_timestamp,
        nid_rates,
    )
    from redibis.pii.device_id import imei_rates
    from redibis.pii.subscriber_id import imsi_rates
    from redibis.pii.context_tokens import (
        merge_negative_tokens,
        suppress_entities_for_column,
    )

    pii_cfg = resolve_pii_config(kwargs.get("pii_config"))
    sample_size = max(1, int(getattr(pii_cfg, "sample_size", 100) or 100))
    msisdn_min = float(getattr(pii_cfg, "msisdn_valid_rate_min", 0.80) or 0.80)
    use_phonenumbers = bool(getattr(pii_cfg, "use_phonenumbers", True))
    default_region = str(getattr(pii_cfg, "default_region", "EG") or "EG")
    phone_gate_conf = float(getattr(pii_cfg, "phone_gate_conf", 0.10) or 0.10)
    geo_require_pair = bool(getattr(pii_cfg, "geo_require_pair", True))
    geo_egypt_geofence = bool(getattr(pii_cfg, "geo_egypt_geofence", False))
    msisdn_prefixes = tuple(getattr(pii_cfg, "msisdn_prefixes", None) or ("010", "011", "012", "015"))
    presidio_min = float(getattr(pii_cfg.thresholds, "presidio_min", 0.80) or 0.80)
    ner_min = float(getattr(pii_cfg.thresholds, "gliner_min", 0.70) or 0.70)
    nid_cfg = getattr(pii_cfg, "national_id_egypt", None)
    nid_enabled = bool(getattr(nid_cfg, "enabled", True)) if nid_cfg is not None else True
    nid_exclude = list(getattr(nid_cfg, "exclude_name_patterns", None) or []) if nid_cfg else []
    nid_max_age = int(getattr(nid_cfg, "max_age", 110) or 110) if nid_cfg else 110
    nid_min_birth = int(getattr(nid_cfg, "min_birth_year", 1900) or 1900) if nid_cfg else 1900
    nid_verify_cd = bool(getattr(nid_cfg, "verify_check_digit", False)) if nid_cfg else False
    device_cfg = getattr(pii_cfg, "device_id", None)
    verify_rbi = bool(getattr(device_cfg, "verify_rbi", True)) if device_cfg else True
    sub_cfg = getattr(pii_cfg, "subscriber_id", None)
    home_mcc = str(getattr(sub_cfg, "home_mcc", "602") or "602") if sub_cfg else "602"
    roaming_mncs = dict(getattr(sub_cfg, "roaming_mncs", None) or {}) if sub_cfg else {}
    neg_tokens = merge_negative_tokens(
        overlay=getattr(pii_cfg, "negative_context_tokens", None) or {},
    )
    geo_partners = find_geo_partner_columns(list(df.columns))

    for col_item in target_cols:
        col_name = col_item.column if hasattr(col_item, "column") else col_item
        triage_score = getattr(col_item, "triage_score", 0.0)
        send_to_detector = getattr(col_item, "send_to_detector", True)

        if not send_to_detector:
            msg = f"  ⏭️  Skipping column '{col_name}' (skipped by triage)"
            logger.info(msg)
            if progress_callback:
                progress_callback(msg)

            detections.append(PIIDetection(
                column=col_name,
                detected=False,
                decision_path="skipped_by_triage",
                triage_score=triage_score,
            ))
            continue

        series = df[col_name].dropna().astype(str)

        n = min(len(series), sample_size)
        if len(series) > n:
            series = series.sample(n=n, random_state=42)

        values = series.tolist()
        if not values:
            msg = f"  ⏭️  Skipping column '{col_name}' (column is empty or all nulls)"
            logger.info(msg)
            if progress_callback:
                progress_callback(msg)

            detections.append(PIIDetection(
                column=col_name,
                detected=False,
                decision_path="empty_column",
                triage_score=triage_score,
            ))
            continue

        # Calculate structural characteristics independently
        avg_len = float(series.str.len().mean())
        group = "structured" if avg_len < 60 else "free_text"

        matches = series.apply(lambda v: bool(arabic_pat.search(v)))
        arabic_fraction = float(matches.mean())
        is_arabic = arabic_fraction >= 0.05

        msg1 = f"  🔍 Scanning column '{col_name}' [group={group}, arabic={is_arabic}]..."
        logger.info(msg1)
        if progress_callback:
            progress_callback(msg1)

        msisdn_valid_rate = compute_msisdn_valid_rate(values, msisdn_prefixes)
        skip_nid_by_name = name_suggests_timestamp(
            col_name, extra_patterns=nid_exclude,
        )
        run_nid_rates = nid_enabled and not skip_nid_by_name
        nid_stats = (
            nid_rates(
                values,
                max_age=nid_max_age,
                min_birth_year=nid_min_birth,
                verify_check_digit=nid_verify_cd,
            )
            if run_nid_rates
            else {}
        )
        run_phone_engine = use_phonenumbers and should_run_phone(
            col_name,
            values,
            conf_thr=phone_gate_conf,
        )
        phone_stats = (
            phone_rates(values, region=default_region)
            if run_phone_engine
            else {"available": False}
        )
        regex_state = {
            "enabled": run_regex,
            "ran": False,
            "status": "disabled" if not run_regex else "pending",
        }
        ner_state = {
            "enabled": run_ner,
            "available": run_ner and (ensemble is not None or ner_backend is not None),
            "ran": False,
            "status": "disabled" if not run_ner else "pending",
        }
        phone_state = {
            "enabled": use_phonenumbers,
            "ran": run_phone_engine,
            "status": (
                "disabled"
                if not use_phonenumbers
                else ("pending" if run_phone_engine else "gated_off")
            ),
        }
        nid_state = {
            "enabled": nid_enabled,
            "ran": run_nid_rates,
            "status": (
                "disabled"
                if not nid_enabled
                else ("skipped_timestamp_name" if skip_nid_by_name else (
                    "matched" if nid_stats.get("nid_valid", 0) else "no_match"
                ))
            ),
            "valid_rate": nid_stats.get("nid_valid_rate"),
            "checked": nid_stats.get("nid_checked", 0),
        }
        imei_stats = imei_rates(values, verify_rbi=verify_rbi)
        imsi_stats = imsi_rates(
            values, home_mcc=home_mcc, roaming_mncs=roaming_mncs,
        )
        imei_checked = int(imei_stats.get("imei_checked", 0) or 0)
        imsi_checked = int(imsi_stats.get("imsi_checked", 0) or 0)
        imei_state = {
            "enabled": True,
            "ran": imei_checked > 0,
            "status": "matched" if imei_stats.get("imei_valid", 0) else "no_match",
            "valid_rate": imei_stats.get("imei_valid_rate") if imei_checked else None,
            "checked": imei_checked,
        }
        imsi_state = {
            "enabled": True,
            "ran": imsi_checked > 0,
            "status": "matched" if imsi_stats.get("imsi_valid", 0) else "no_match",
            "valid_rate": imsi_stats.get("imsi_valid_rate") if imsi_checked else None,
            "checked": imsi_checked,
        }
        partner_col = geo_partners.get(col_name)
        partner_values = None
        if partner_col and partner_col in df.columns:
            pseries = df[partner_col].dropna().astype(str)
            pn = min(len(pseries), sample_size)
            if len(pseries) > pn:
                pseries = pseries.sample(n=pn, random_state=42)
            partner_values = pseries.tolist()
        blocked_entities = suppress_entities_for_column(
            col_name, negative_tokens=neg_tokens,
        )

        if run_regex:
            presidio_result = _run_presidio(
                values,
                group,
                is_arabic,
                col_name,
                regex_overrides=regex_overrides,
                ruleset=kwargs.get("ruleset"),
                progress_callback=progress_callback,
                msisdn_valid_rate=msisdn_valid_rate,
                geo_partner=partner_col,
                partner_values=partner_values,
                geo_require_pair=geo_require_pair,
                geo_egypt_geofence=geo_egypt_geofence,
                entity_tokens=entity_tokens,
                suppressed_entities=blocked_entities,
            )
            presidio_score = presidio_result.get("score")
            regex_state.update({
                "ran": True,
                "status": "matched" if presidio_score is not None else "no_match",
            })
        else:
            presidio_result = {}
            presidio_score = None
            regex_state["status"] = "disabled"

        ner_result: dict = {}
        ner_hits: list[dict] = []
        has_ner = run_ner and (ensemble is not None or ner_backend is not None)
        if has_ner:
            skip_ner = (
                not always_run_ner
                and presidio_score is not None
                and presidio_score >= 0.80
            )
            if skip_ner:
                ner_result = {"score": None, "label": None, "match_rate": None}
                ner_state.update({
                    "available": True,
                    "ran": False,
                    "status": "skipped_high_regex",
                })
            elif ensemble is not None:
                ner_result, ner_hits = _run_ner_ensemble(
                    ensemble,
                    values,
                    col_name,
                    label_groups=ner_label_groups,
                    table=table,
                    progress_callback=progress_callback,
                )
                ner_state.update({
                    "available": True,
                    "ran": True,
                    "status": "matched" if ner_result.get("score") is not None else "no_match",
                })
            else:
                assert ner_backend is not None
                ner_result, ner_hits = _run_ner(
                    ner_backend,
                    values,
                    col_name,
                    progress_callback=progress_callback,
                )
                ner_state.update({
                    "available": True,
                    "ran": True,
                    "status": "matched" if ner_result.get("score") is not None else "no_match",
                })
                for hit in ner_hits:
                    _emit_ner_hit_decision(table=table, column=col_name, hit=hit)
        elif run_ner and ner_backend is None and ensemble is None:
            ner_state.update({
                "available": False,
                "ran": False,
                "status": "backend_unavailable",
            })
            logger.debug("NER requested but no backend loaded for column %s", col_name)
        elif not run_ner:
            ner_state["status"] = "disabled"

        regex_hits = presidio_result.get("regex_hits") or []
        if skip_nid_by_name:
            regex_hits = [
                h for h in regex_hits if not is_nid_entity(h.get("entity_type"))
            ]
        phone_valid_rate = (
            float(phone_stats.get("valid_rate"))
            if phone_stats.get("available")
            else None
        )
        regex_hits = apply_phone_phonenumbers_gate(
            regex_hits,
            use_phonenumbers=use_phonenumbers,
            phone_valid_rate=phone_valid_rate,
            phone_min=msisdn_min,
            phonenumbers_ran=run_phone_engine,
            column_name=col_name,
        )
        if regex_hits != (presidio_result.get("regex_hits") or []) or skip_nid_by_name:
            presidio_result = dict(presidio_result)
            presidio_result["regex_hits"] = regex_hits
            if regex_hits:
                presidio_result["score"] = regex_hits[0].get("score")
                presidio_result["pattern"] = regex_hits[0].get("pattern_name")
                presidio_result["entity_type"] = regex_hits[0].get("entity_type")
                presidio_result["match_rate"] = regex_hits[0].get("match_rate")
            else:
                presidio_result["score"] = None
                presidio_result["pattern"] = None
                presidio_result["entity_type"] = None
                presidio_result["match_rate"] = None
            presidio_score = presidio_result.get("score")

        _emit_regex_hit_decisions(
            table=table,
            column=col_name,
            regex_hits=regex_hits,
            log_regex_hits=log_regex_hits,
            max_logged=max_regex_hits_logged,
        )

        ner_score = ner_result.get("score")

        pres_entity, _ = presidio_phone_signal(regex_hits=regex_hits)
        phone_score, phone_entity = compute_phone_plan_score(
            msisdn_valid_rate,
            phone_stats,
            column_name=col_name,
            msisdn_valid_rate_min=msisdn_min,
            presidio_entity=pres_entity or presidio_result.get("entity_type"),
            presidio_score=presidio_score,
            ner_label=ner_result.get("label"),
            ner_score=ner_score,
            presidio_min=presidio_min,
            ner_min=ner_min,
            use_phonenumbers=use_phonenumbers,
        )
        phone_mobile_rate = (
            float(phone_stats.get("mobile_rate"))
            if phone_stats.get("available")
            else None
        )
        phone_regions = phone_stats.get("regions") if phone_stats.get("available") else None
        if not use_phonenumbers:
            phone_state["status"] = "disabled"
        elif run_phone_engine:
            phone_state["status"] = (
                "matched"
                if phone_score is not None
                else "no_match"
            )

        # Prefer the entity type from the strongest engine signal (phonenumbers-gated).
        entity_type = resolve_entity_type(
            presidio_score=presidio_score,
            presidio_entity=presidio_result.get("entity_type"),
            ner_score=ner_score,
            ner_entity=ner_result.get("label"),
            phone_score=phone_score,
            phone_entity=phone_entity,
            use_phonenumbers=use_phonenumbers,
            phone_valid_rate=phone_valid_rate,
            phone_min=msisdn_min,
            phonenumbers_ran=run_phone_engine,
            column_name=col_name,
        )
        if skip_nid_by_name and is_nid_entity(entity_type):
            entity_type = None
        if entity_type and entity_type.upper().replace(" ", "_") in blocked_entities:
            entity_type = None

        if entity_type:
            entity_type = entity_type.upper().replace(" ", "_")
            # Normalize engine-specific aliases (GLiNER vs regex) to one canonical
            # vocabulary: EMAIL->EMAIL_ADDRESS, ADDRESS->LOCATION, etc.
            from redibis.models import canonical_entity
            entity_type = canonical_entity(entity_type)

        if log_samples:
            samples_preview = " | ".join(
                v[:50].replace("\n", " ") for v in values[:2]
            )
            sample_suffix = f" | Samples: [{samples_preview}]"
        else:
            sample_suffix = f" | n={len(values)} avg_len={avg_len:.0f}"
        msg2 = (
            f"     ↳ Scores for '{col_name}' - Regex: {presidio_score} "
            f"| NER Model: {ner_score} | Phone: {phone_score}{sample_suffix}"
        )
        logger.info(msg2)
        if progress_callback:
            progress_callback(msg2)

        geo_conf = presidio_result.get("geo_confidence")
        if geo_conf is None and regex_hits:
            geo_conf = regex_hits[0].get("geo_confidence")
        geo_state = {
            "enabled": True,
            "ran": partner_col is not None or bool(geo_conf),
            "status": "matched" if (geo_conf or 0) >= 0.75 else "no_match",
            "confidence": geo_conf,
            "partner": partner_col,
        }

        detection = PIIDetection(
            column               = col_name,
            detected             = False,
            entity_type          = entity_type,
            confidence           = 0.0,
            presidio_score       = presidio_result.get("score"),
            presidio_pattern     = presidio_result.get("pattern"),
            presidio_match_rate  = presidio_result.get("match_rate"),
            gliner_score         = ner_result.get("score"),
            gliner_label         = ner_result.get("label"),
            gliner_match_rate    = ner_result.get("match_rate"),
            ner_engine           = (
                ensemble.backends[0].name if ensemble and ensemble.backends
                else (ner_backend.name if (ner_backend and run_ner) else None)
            ),
            arabic_aware         = is_arabic,
            arabic_fraction      = arabic_fraction,
            triage_score         = triage_score,
            regex_pattern        = presidio_result.get("pattern"),
            regex_hits           = regex_hits,
            ner_hits             = ner_hits,
            phone_score          = phone_score,
            phone_entity         = phone_entity,
            msisdn_valid_rate    = msisdn_valid_rate,
            phone_valid_rate     = phone_valid_rate,
            phone_mobile_rate    = phone_mobile_rate,
            phone_regions        = phone_regions,
            nid_valid_rate       = (
                float(nid_stats["nid_valid_rate"])
                if run_nid_rates and "nid_valid_rate" in nid_stats
                else None
            ),
            nid_checked          = int(nid_stats.get("nid_checked", 0) or 0),
            nid_age_p05          = nid_stats.get("nid_age_p05"),
            nid_age_p95          = nid_stats.get("nid_age_p95"),
            nid_gov_distinct     = nid_stats.get("nid_gov_distinct"),
            nid_monotonic_rate   = nid_stats.get("nid_monotonic_rate"),
            nid_unique_rate      = nid_stats.get("nid_unique_rate"),
            imei_valid_rate      = (
                float(imei_stats["imei_valid_rate"]) if imei_checked else None
            ),
            imei_checked         = imei_checked,
            imsi_valid_rate      = (
                float(imsi_stats["imsi_valid_rate"]) if imsi_checked else None
            ),
            imsi_checked         = imsi_checked,
            geo_confidence       = float(geo_conf) if geo_conf is not None else None,
            engine_states        = {
                "regex": regex_state,
                "ner": ner_state,
                "phone": phone_state,
                "nid": nid_state,
                "imei": imei_state,
                "imsi": imsi_state,
                "geo": geo_state,
            },
        )
        from redibis.evidence.engines import project_engine_evidence

        detection.engine_evidence = project_engine_evidence(detection)

        detections.append(detection)

    logger.info(
        "Scanned %d columns, %d detections produced",
        len(target_cols),
        len(detections),
    )

    return detections


def detect_pii(
    df: Any,
    columns: Optional[list[Any]] = None,
    engines: str = "both",
    gliner_always_run: bool = False,
    ner_always_run: bool = False,
    ner_backend: Optional[NERBackend] = None,
    ensemble=None,
    regex_overrides: Optional[RegexOverrides] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    table: str = "",
    log_samples: bool = False,
    log_regex_hits: bool = True,
    max_regex_hits_logged: int = 20,
    ner_label_groups: Optional[list[list[str]]] = None,
    **kwargs,
) -> list[PIIDetection]:
    """Compatibility shim — delegates to ``ColumnScanner.detect``.

    Runs Presidio + NER on every column. Returns evidence-only
    ``PIIDetection`` rows (``detected=False``); apply ``decide_pii`` /
    ``VerdictResolver`` for verdicts.
    """
    from redibis.pii.scan.column_scanner import ColumnScanner

    ruleset = kwargs.pop("ruleset", None)
    scanner = ColumnScanner(ruleset=ruleset, ner_backend=ner_backend)
    return scanner.detect(
        df,
        columns=columns,
        engines=engines,
        gliner_always_run=gliner_always_run,
        ner_always_run=ner_always_run,
        ner_backend=ner_backend,
        ensemble=ensemble,
        regex_overrides=regex_overrides,
        progress_callback=progress_callback,
        table=table,
        log_samples=log_samples,
        log_regex_hits=log_regex_hits,
        max_regex_hits_logged=max_regex_hits_logged,
        ner_label_groups=ner_label_groups,
        **kwargs,
    )
