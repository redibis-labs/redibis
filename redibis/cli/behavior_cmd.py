"""CLI for Behavior Policy Runtime (Phases 5–9).

Commands mirror the REST lifecycle without writing contracts:

  redibis behavior catalog|validate|list|get|create|simulate|
                   approve|activate|deactivate|rollback|
                   promote|history|audit|metrics|status|
                   plugins|outcomes|signals|suggest|draft-from
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

from redibis.services.behavior_service import (
    BehaviorPolicyService,
    BehaviorServiceError,
    default_policy_store_dir,
)


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    elif isinstance(payload, str):
        print(payload)
    else:
        print(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def _load_document(path: str | Path) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise BehaviorServiceError(f"policy document must be a mapping: {path}")
    return data


def _svc(args) -> BehaviorPolicyService:
    base = getattr(args, "store_dir", None) or os.environ.get("BEHAVIOR_POLICIES_DIR")
    kwargs: dict[str, Any] = {}
    if base:
        kwargs["base_dir"] = base
    if getattr(args, "no_approval_gate", False):
        kwargs["require_approval_for_activation"] = False
    if getattr(args, "no_simulation_gate", False):
        kwargs["require_simulation_for_activation"] = False
    return BehaviorPolicyService(**kwargs)


def _common(p) -> None:
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument(
        "--store-dir",
        help="policy store root (default: CONFIGS_DIR/behavior-policies or BEHAVIOR_POLICIES_DIR)",
    )
    p.add_argument("--config", help="redibis.yaml; or set REDIBIS_CONFIG")


def register_behavior_commands(sub) -> None:
    """Attach ``redibis behavior …`` parsers to the root subparsers."""
    p = sub.add_parser(
        "behavior",
        help="behavior policy lifecycle (catalogue, simulate, activate, learning)",
    )
    bsub = p.add_subparsers(dest="behavior_action", required=True)

    # ── catalogue / status ───────────────────────────────────────────────
    p_cat = bsub.add_parser("catalog", help="print fact/operator/effect catalogue")
    _common(p_cat)

    p_status = bsub.add_parser("status", help="show BehaviorConfig + store summary")
    _common(p_status)

    p_metrics = bsub.add_parser("metrics", help="aggregate policy/simulation/learning counters")
    _common(p_metrics)

    # ── validate / CRUD ──────────────────────────────────────────────────
    p_val = bsub.add_parser("validate", help="compile + lint a policy document")
    p_val.add_argument("file", help="policy JSON/YAML document")
    _common(p_val)

    p_list = bsub.add_parser("list", help="list stored policy versions")
    _common(p_list)

    p_get = bsub.add_parser("get", help="get one stored policy version")
    p_get.add_argument("policy_id")
    p_get.add_argument("version")
    _common(p_get)

    p_create = bsub.add_parser("create", help="save an immutable draft policy version")
    p_create.add_argument("file", help="policy JSON/YAML document")
    p_create.add_argument("--actor", default=os.environ.get("USER", "cli"))
    p_create.add_argument("--note", default="")
    _common(p_create)

    # ── simulate ─────────────────────────────────────────────────────────
    p_sim = bsub.add_parser("simulate", help="evaluate policy against sanitized contexts (write-free)")
    p_sim.add_argument("--file", help="inline policy document (JSON/YAML)")
    p_sim.add_argument("--policy-id")
    p_sim.add_argument("--version")
    p_sim.add_argument(
        "--context-file",
        help="JSON file: one context object or {\"contexts\": [...]}",
    )
    p_sim.add_argument("--column", help="shortcut: column name for a single context")
    p_sim.add_argument(
        "--facts",
        help='shortcut: JSON object of facts, e.g. \'{"column.name":"lac","verdict.entity":"PHONE_NUMBER"}\'',
    )
    p_sim.add_argument("--table", default="")
    _common(p_sim)

    # ── lifecycle ────────────────────────────────────────────────────────
    def _actor(p_):
        p_.add_argument("--actor", default=os.environ.get("USER", "cli"), required=False)
        p_.add_argument("--role", default="steward")
        p_.add_argument("--note", default="")

    p_appr = bsub.add_parser("approve", help="approve a version after simulation")
    p_appr.add_argument("policy_id")
    p_appr.add_argument("version")
    p_appr.add_argument("--simulation-id", default=None)
    _actor(p_appr)
    _common(p_appr)

    p_act = bsub.add_parser("activate", help="activate an approved version")
    p_act.add_argument("policy_id")
    p_act.add_argument("version")
    p_act.add_argument("--simulation-id", default=None)
    p_act.add_argument("--no-approval-gate", action="store_true",
                       help="skip approval check (dev only)")
    p_act.add_argument("--role", default="admin")
    p_act.add_argument("--actor", default=os.environ.get("USER", "cli"))
    p_act.add_argument("--note", default="")
    _common(p_act)

    p_deact = bsub.add_parser("deactivate", help="clear the active pointer")
    p_deact.add_argument("policy_id")
    p_deact.add_argument("version", nargs="?", default="")
    p_deact.add_argument("--role", default="admin")
    p_deact.add_argument("--actor", default=os.environ.get("USER", "cli"))
    p_deact.add_argument("--note", default="")
    _common(p_deact)

    p_rb = bsub.add_parser("rollback", help="re-point active to a prior version")
    p_rb.add_argument("policy_id")
    p_rb.add_argument("--to-version", default=None)
    p_rb.add_argument("--role", default="admin")
    p_rb.add_argument("--actor", default=os.environ.get("USER", "cli"))
    p_rb.add_argument("--note", default="")
    _common(p_rb)

    p_promo = bsub.add_parser(
        "promote",
        help="promote a human correction to a draft behavior policy (never auto-activates)",
    )
    p_promo.add_argument("--column", required=True)
    p_promo.add_argument("--from-entity", required=True, dest="from_entity")
    p_promo.add_argument("--to-entity", required=True, dest="to_entity")
    p_promo.add_argument("--table", default="")
    p_promo.add_argument("--run-id", default="")
    p_promo.add_argument("--policy-id", default=None)
    p_promo.add_argument("--version", default="0.1.0")
    p_promo.add_argument("--actor", default=os.environ.get("USER", "cli"))
    p_promo.add_argument("--note", default="")
    _common(p_promo)

    p_hist = bsub.add_parser("history", help="lifecycle event log for one policy id")
    p_hist.add_argument("policy_id")
    _common(p_hist)

    p_audit = bsub.add_parser("audit", help="lifecycle events across policies")
    p_audit.add_argument("--policy-id", default=None)
    p_audit.add_argument("--limit", type=int, default=100)
    _common(p_audit)

    # ── plugins (Phase 7) ────────────────────────────────────────────────
    p_plug = bsub.add_parser("plugins", help="discover/load allow-listed behavior plug-ins")
    p_plug.add_argument(
        "--apply",
        action="store_true",
        help="register discovered plug-ins onto a fresh registry (report only by default)",
    )
    _common(p_plug)

    # ── learning (Phase 9) ───────────────────────────────────────────────
    p_out = bsub.add_parser("outcomes", help="record or list value-free outcome labels")
    out_sub = p_out.add_subparsers(dest="outcomes_action", required=True)

    p_out_list = out_sub.add_parser("list", help="list stored outcome labels")
    p_out_list.add_argument("--policy-id", default=None)
    p_out_list.add_argument("--rule-id", default=None)
    _common(p_out_list)

    p_out_rec = out_sub.add_parser("record", help="append one value-free outcome label")
    p_out_rec.add_argument("--policy-id", required=True)
    p_out_rec.add_argument("--rule-id", required=True)
    p_out_rec.add_argument("--policy-sha", required=True, dest="policy_sha")
    p_out_rec.add_argument("--original", required=True, help="original verdict class/label")
    p_out_rec.add_argument("--policy-verdict", required=True, dest="policy_verdict")
    p_out_rec.add_argument(
        "--human-result",
        required=True,
        choices=["confirmed", "reversed", "unknown"],
        dest="human_result",
    )
    p_out_rec.add_argument("--context-hash", required=True, dest="context_hash")
    p_out_rec.add_argument("--engine", default="pii")
    p_out_rec.add_argument("--table", default="")
    p_out_rec.add_argument("--column", default=None)
    _common(p_out_rec)

    p_sig = bsub.add_parser("signals", help="per-rule precision signal estimates")
    p_sig.add_argument("--policy-id", default=None)
    _common(p_sig)

    p_sug = bsub.add_parser("suggest", help="narrow/retire suggestions (never auto-disables)")
    p_sug.add_argument("--policy-id", default=None)
    _common(p_sug)

    p_draft = bsub.add_parser(
        "draft-from",
        help="build a draft policy from repeated corrections JSON (never auto-activates)",
    )
    p_draft.add_argument("file", help="JSON list of {column, original_verdict, desired_verdict, reason}")
    p_draft.add_argument("--engine", default="pii")
    p_draft.add_argument("--stage", default="post_verdict")
    p_draft.add_argument("--min-repetitions", type=int, default=3)
    p_draft.add_argument("--policy-id", default=None)
    p_draft.add_argument("--save", action="store_true", help="also save draft via create()")
    p_draft.add_argument("--actor", default=os.environ.get("USER", "cli"))
    _common(p_draft)


def run_behavior(args) -> int:
    """Dispatch ``redibis behavior <action>``."""
    action = args.behavior_action
    as_json = bool(getattr(args, "json", False))

    try:
        return _dispatch(args, action, as_json)
    except BehaviorServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"error: invalid document: {exc}", file=sys.stderr)
        return 1


def _dispatch(args, action: str, as_json: bool) -> int:
    if action == "catalog":
        payload = _svc(args).catalog()
        _emit(payload, as_json)
        return 0

    if action == "status":
        from redibis.config import RedibisConfig
        from redibis.behavior.config import BehaviorConfig

        path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
        cfg = RedibisConfig.from_yaml(path) if path else RedibisConfig.default()
        bcfg: BehaviorConfig = getattr(cfg, "behavior", None) or BehaviorConfig()
        svc = _svc(args)
        policies = svc.list_policies()
        payload = {
            "enabled": bcfg.enabled,
            "mode": bcfg.mode,
            "effective_mode_pii": bcfg.effective_mode("pii").value,
            "fail_mode": bcfg.fail_mode,
            "plugin_allowlist": list(bcfg.plugin_allowlist or []),
            "policies_path": str(getattr(args, "store_dir", None) or default_policy_store_dir()),
            "policies_total": len(policies),
            "policies_active": sum(1 for p in policies if p.get("active")),
        }
        _emit(payload, as_json)
        return 0

    if action == "metrics":
        svc = _svc(args)
        policies = svc.list_policies()
        sims = 0
        for p in policies:
            data = svc.store.get(p["id"], p["version"]) or {}
            if (data.get("lifecycle") or {}).get("last_simulation_id"):
                sims += 1
        payload: dict[str, Any] = {
            "policies_total": len(policies),
            "policies_active": sum(1 for p in policies if p.get("active")),
            "policies_approved": sum(1 for p in policies if p.get("approved")),
            "versions_with_simulation": sims,
        }
        try:
            from redibis.behavior.learning import compute_rule_signals

            signals = compute_rule_signals(svc.store._base)
            payload["learning"] = {
                "rules_with_labels": len(signals),
                "total_applications": sum(int(s.applications or 0) for s in signals),
                "total_confirmations": sum(int(s.confirmations or 0) for s in signals),
                "total_reversals": sum(int(s.reversals or 0) for s in signals),
            }
        except Exception:
            payload["learning"] = {"rules_with_labels": 0}
        _emit(payload, as_json)
        return 0

    if action == "validate":
        result = _svc(args).validate(_load_document(args.file))
        _emit(result.to_dict(), as_json or True)
        return 0 if result.ok or result.content_sha256 else 1

    if action == "list":
        items = _svc(args).list_policies()
        if as_json:
            _emit({"policies": items}, True)
        else:
            if not items:
                print("(no policies)")
            for p in items:
                flag = "*" if p.get("active") else " "
                print(
                    f"{flag} {p['id']:40s}  {p['version']:10s}  "
                    f"{p.get('status',''):10s}  {(p.get('content_sha256') or '')[:12]}"
                )
        return 0

    if action == "get":
        data = _svc(args).get(args.policy_id, args.version)
        _emit(data, as_json or True)
        return 0

    if action == "create":
        out = _svc(args).create(
            _load_document(args.file),
            actor=args.actor,
            note=args.note,
        )
        _emit(out, as_json or True)
        return 0

    if action == "simulate":
        document = _load_document(args.file) if getattr(args, "file", None) else None
        contexts: list[dict] = []
        if getattr(args, "context_file", None):
            raw = json.loads(Path(args.context_file).read_text(encoding="utf-8"))
            if isinstance(raw, list):
                contexts = raw
            elif isinstance(raw, dict) and "contexts" in raw:
                contexts = list(raw["contexts"])
            elif isinstance(raw, dict):
                contexts = [raw]
            else:
                raise BehaviorServiceError("context file must be an object or list")
        elif getattr(args, "column", None) or getattr(args, "facts", None):
            facts = json.loads(args.facts) if args.facts else {}
            if args.column and "column.name" not in facts:
                facts["column.name"] = args.column
            contexts = [{
                "column": args.column or facts.get("column.name", ""),
                "table": getattr(args, "table", "") or "",
                "facts": facts,
            }]
        result = _svc(args).simulate(
            document=document,
            policy_id=getattr(args, "policy_id", None),
            version=getattr(args, "version", None),
            contexts=contexts or None,
        )
        _emit(result.to_dict(), as_json or True)
        return 0

    if action == "approve":
        out = _svc(args).approve(
            args.policy_id,
            args.version,
            actor=args.actor,
            role=args.role,
            note=args.note,
            simulation_id=getattr(args, "simulation_id", None),
        )
        _emit(out, as_json or True)
        return 0

    if action == "activate":
        out = _svc(args).activate(
            args.policy_id,
            args.version,
            actor=args.actor,
            role=getattr(args, "role", "admin") or "admin",
            note=args.note,
            simulation_id=getattr(args, "simulation_id", None),
            skip_approval_check=bool(getattr(args, "no_approval_gate", False)),
        )
        _emit(out, as_json or True)
        return 0

    if action == "deactivate":
        version = args.version or ""
        if not version:
            ptr = _svc(args).store.get_active_pointer(args.policy_id) or {}
            version = ptr.get("version") or ""
        if not version:
            raise BehaviorServiceError("version required (or an active pointer must exist)")
        out = _svc(args).deactivate(
            args.policy_id,
            version,
            actor=args.actor,
            role=getattr(args, "role", "admin") or "admin",
            note=args.note,
        )
        _emit(out, as_json or True)
        return 0

    if action == "rollback":
        out = _svc(args).rollback(
            args.policy_id,
            actor=args.actor,
            role=getattr(args, "role", "admin") or "admin",
            note=args.note,
            to_version=getattr(args, "to_version", None),
        )
        _emit(out, as_json or True)
        return 0

    if action == "promote":
        out = _svc(args).promote_correction_to_draft(
            column=args.column,
            from_entity=args.from_entity,
            to_entity=args.to_entity,
            note=args.note,
            run_id=args.run_id,
            table=args.table,
            actor=args.actor,
            policy_id=args.policy_id,
            version=args.version,
        )
        _emit(out, as_json or True)
        return 0

    if action == "history":
        events = _svc(args).store.list_lifecycle_events(args.policy_id)
        _emit({"events": events}, as_json or True)
        return 0

    if action == "audit":
        svc = _svc(args)
        events: list[dict] = []
        for item in svc.list_policies():
            pid = item["id"]
            if args.policy_id and pid != args.policy_id:
                continue
            for ev in svc.store.list_lifecycle_events(pid):
                events.append({"policy_id": pid, **ev})
        events.sort(key=lambda e: e.get("at") or "", reverse=True)
        _emit({"events": events[: int(args.limit)]}, as_json or True)
        return 0

    if action == "plugins":
        from redibis.behavior.config import BehaviorConfig
        from redibis.behavior.plugins import load_plugins, plugin_manifest
        from redibis.behavior.registry import BehaviorRegistry, get_builtin_registry
        from redibis.config import RedibisConfig

        path = getattr(args, "config", None) or os.environ.get("REDIBIS_CONFIG")
        cfg = RedibisConfig.from_yaml(path) if path else RedibisConfig.default()
        bcfg = getattr(cfg, "behavior", None) or BehaviorConfig()
        # Always use a throwaway registry so CLI never mutates the process singleton.
        reg = BehaviorRegistry()
        builtin = get_builtin_registry()
        for desc in builtin._facts.values():
            reg.register_fact(desc, builtin=True)
        for desc in builtin._effects.values():
            reg.register_effect(desc, builtin=True)
        for desc in builtin._operators.values():
            reg.register_operator(desc)
        reports = load_plugins(bcfg, reg)
        payload = {
            "allowlist": list(bcfg.plugin_allowlist or []),
            "applied": bool(getattr(args, "apply", False)),
            "registered_effect_count": len(reg._effects),
            "registered_fact_count": len(reg._facts),
            "reports": reports,
            "manifest": plugin_manifest(reports) if reports else {},
        }
        _emit(payload, as_json or True)
        return 0

    if action == "outcomes":
        return _run_outcomes(args, as_json)

    if action == "signals":
        from dataclasses import asdict
        from redibis.behavior.learning import compute_rule_signals

        svc = _svc(args)
        signals = compute_rule_signals(svc.store._base, policy_id=args.policy_id)
        _emit({"signals": [asdict(s) for s in signals]}, as_json or True)
        return 0

    if action == "suggest":
        from dataclasses import asdict
        from redibis.behavior.learning import compute_rule_signals, suggest_narrow_or_retire

        svc = _svc(args)
        signals = compute_rule_signals(svc.store._base, policy_id=args.policy_id)
        suggestions = suggest_narrow_or_retire(signals)
        _emit({"suggestions": [asdict(s) for s in suggestions]}, as_json or True)
        return 0

    if action == "draft-from":
        from redibis.behavior.learning import draft_from_repeated_corrections

        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise BehaviorServiceError("draft-from file must be a JSON list of corrections")
        draft = draft_from_repeated_corrections(
            raw,
            engine=args.engine,
            stage=args.stage,
            min_repetitions=args.min_repetitions,
            policy_id=args.policy_id,
        )
        if draft is None:
            print("No pattern met the repetition threshold — no draft produced.", file=sys.stderr)
            return 1
        if getattr(args, "save", False):
            # Strip lifecycle helper fields that are not part of the schema
            document = {k: v for k, v in draft.items() if k != "lifecycle"}
            if "apiVersion" not in document:
                _emit(draft, as_json or True)
                print("warning: draft is not a full v1 document; not saved", file=sys.stderr)
                return 0
            out = _svc(args).create(document, actor=args.actor, note="draft-from corrections")
            _emit(out, as_json or True)
            return 0
        _emit(draft, as_json or True)
        return 0

    print(f"unknown behavior action: {action}", file=sys.stderr)
    return 2


def _run_outcomes(args, as_json: bool) -> int:
    from dataclasses import asdict
    from redibis.behavior.learning import OutcomeLabel, load_outcome_labels, record_outcome_label

    svc = _svc(args)
    base = svc.store._base
    sub = args.outcomes_action

    if sub == "list":
        labels = load_outcome_labels(
            base,
            policy_id=getattr(args, "policy_id", None),
            rule_id=getattr(args, "rule_id", None),
        )
        _emit({"outcomes": [asdict(x) for x in labels]}, as_json or True)
        return 0

    if sub == "record":
        label = OutcomeLabel(
            policy_id=args.policy_id,
            rule_id=args.rule_id,
            policy_sha256=args.policy_sha,
            original_verdict=args.original,
            policy_verdict=args.policy_verdict,
            human_result=args.human_result,
            context_hash=args.context_hash,
            engine=args.engine,
            table=args.table,
            column=args.column,
        )
        path = record_outcome_label(base, label)
        _emit({"written": str(path), "label": asdict(label)}, as_json or True)
        return 0

    print(f"unknown outcomes action: {sub}", file=sys.stderr)
    return 2
