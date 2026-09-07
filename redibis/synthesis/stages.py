"""Typed Contract Synthesis stages.

Each stage mutates an in-memory ODCS v3.1 candidate and records findings.
Stages never write through ``ContractStore.upsert``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from redibis.synthesis.evidence import EvidenceBundle, EvidenceKind, TraceStatus
from redibis.synthesis.lineage import (
    apply_foreign_key_relationships,
    apply_transform_fields,
    build_lineage_graph,
)

STAGE_REGISTRY: dict[str, "StageSpec"] = {}


@dataclass
class StageResult:
    stage_id: str
    ok: bool = True
    findings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageSpec:
    id: str
    label: str
    run: Callable[[dict, EvidenceBundle, dict], StageResult]
    enabled: bool = True


def register_stage(spec: StageSpec) -> StageSpec:
    STAGE_REGISTRY[spec.id] = spec
    return spec


def default_stage_order() -> list[str]:
    return [
        "fundamentals",
        "servers",
        "schema_fields",
        "transforms",
        "quality",
        "sla",
        "security_roles",
        "team_support",
        "custom_properties",
        "lineage_relationships",
        "traceability",
        "validate",
    ]


def run_stages(
    candidate: dict[str, Any],
    bundle: EvidenceBundle,
    *,
    stage_ids: Optional[list[str]] = None,
    context: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], list[StageResult], dict[str, Any]]:
    ctx = context or {}
    results: list[StageResult] = []
    working = candidate
    for sid in stage_ids or default_stage_order():
        spec = STAGE_REGISTRY.get(sid)
        if not spec or not spec.enabled:
            continue
        result = spec.run(working, bundle, ctx)
        results.append(result)
        if not result.ok and sid == "validate":
            break
    return working, results, ctx


# ── Stage implementations ──────────────────────────────────────────────────


def _stage_fundamentals(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    desc = candidate.setdefault("description", {})
    if not isinstance(desc, dict):
        desc = {}
        candidate["description"] = desc

    for rec in bundle.by_kind(EvidenceKind.REQUIREMENT):
        for rid in rec.requirement_ids:
            text = rec.summary
            if rid == "BR-01" and not desc.get("purpose"):
                # Prefer payload purpose-like content.
                purpose = (rec.payload or {}).get("Purpose") or (rec.payload or {}).get("purpose")
                desc["purpose"] = (purpose or text)[:2000]
                findings.append("filled description.purpose from BR-01")
            if rid == "BR-03" and not desc.get("limitations"):
                desc["limitations"] = text[:2000]
                findings.append("filled description.limitations from BR-03")
            if rid == "BR-04" and not desc.get("usage"):
                desc["usage"] = text[:2000]
                findings.append("filled description.usage from BR-04")

    # Tags from requirement keywords.
    tags = set(candidate.get("tags") or [])
    for rec in bundle.records:
        for rid in rec.requirement_ids:
            if rid.startswith("BR-"):
                tags.add("synthesized")
    if tags:
        candidate["tags"] = sorted(tags)
    return StageResult(stage_id="fundamentals", findings=findings)


def _stage_servers(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    servers = list(candidate.get("servers") or [])
    # From NFR / custom payloads mentioning databricks etc.
    for rec in bundle.records:
        payload = rec.payload or {}
        platform = str(payload.get("Requirement") or payload.get("Property") or rec.summary)
        if "databricks" in platform.lower() and not any(s.get("type") == "databricks" for s in servers):
            servers.append({
                "server": "synthesized-prod",
                "type": "databricks",
                "environment": "prod",
                "description": "Inferred from requirements (NFR platform).",
                "catalog": "",
                "schema": "",
            })
            findings.append("added databricks server stub from NFR evidence")
            break
    if servers:
        candidate["servers"] = servers
    return StageResult(stage_id="servers", findings=findings)


def _logical_type(raw: str) -> str:
    t = (raw or "string").strip().lower()
    mapping = {
        "string": "string",
        "str": "string",
        "varchar": "string",
        "date": "date",
        "timestamp": "date",
        "datetime": "date",
        "integer": "integer",
        "int": "integer",
        "long": "integer",
        "number": "number",
        "decimal": "number",
        "float": "number",
        "double": "number",
        "boolean": "boolean",
        "bool": "boolean",
        "object": "object",
        "array": "array",
    }
    # Handle "string (timestamp)"
    for key, val in mapping.items():
        if t.startswith(key):
            return val
    return "string"


def _stage_schema_fields(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    schemas = candidate.setdefault("schema", [])
    if not schemas:
        schemas.append({"name": candidate.get("name") or "object", "properties": [], "relationships": []})
        candidate["schema"] = schemas
    schema_obj = schemas[0]
    props = schema_obj.setdefault("properties", [])
    by_name = {p.get("name"): p for p in props if p.get("name")}

    for rec in bundle.by_kind(EvidenceKind.SCHEMA_FIELD):
        payload = rec.payload or {}
        name = payload.get("name")
        if not name:
            continue
        prop = by_name.get(name)
        created = False
        if prop is None:
            prop = {"name": name}
            props.append(prop)
            by_name[name] = prop
            created = True
            findings.append(f"added property {name}")

        lt = _logical_type(str(payload.get("logicalType") or ""))
        if payload.get("logicalType"):
            prop["logicalType"] = lt
        if payload.get("physicalType"):
            prop["physicalType"] = payload["physicalType"]
        nullable = str(payload.get("nullable") or "").lower()
        if "no" in nullable or "pk" in nullable:
            prop["required"] = True
        elif "yes" in nullable:
            prop["required"] = False
        if "pk" in nullable:
            prop["primaryKey"] = True
            m = re.search(r"pk\s*(\d+)", nullable)
            if m:
                prop["primaryKeyPosition"] = int(m.group(1))
        if "partition" in nullable:
            prop["partitioned"] = True
            prop["partitionKeyPosition"] = 1
        if payload.get("classification"):
            prop["classification"] = str(payload["classification"]).lower()
        if payload.get("criticalDataElement"):
            prop["criticalDataElement"] = True
        # requirementId custom property
        if rec.requirement_ids:
            cps = list(prop.get("customProperties") or [])
            existing = {c.get("property") for c in cps if isinstance(c, dict)}
            if "requirementId" not in existing:
                val: Any = rec.requirement_ids[0] if len(rec.requirement_ids) == 1 else list(rec.requirement_ids)
                cps.append({"property": "requirementId", "value": val})
                prop["customProperties"] = cps
        prop.setdefault("relationships", [])
        if created:
            pass
    schema_obj["properties"] = props
    return StageResult(stage_id="schema_fields", findings=findings)


def _stage_transforms(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    apply_transform_fields(candidate, bundle)
    return StageResult(stage_id="transforms", findings=["applied transformSourceObjects/transformLogic"])


def _stage_quality(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    schemas = candidate.get("schema") or []
    if not schemas:
        return StageResult(stage_id="quality", findings=findings)
    schema_obj = schemas[0]
    table_quality = list(schema_obj.get("quality") or [])
    props_by = {p.get("name"): p for p in schema_obj.get("properties") or [] if p.get("name")}

    for rec in bundle.by_kind(EvidenceKind.QUALITY_RULE):
        payload = rec.payload or {}
        rid = (rec.requirement_ids or [None])[0]
        name = str(payload.get("Check") or payload.get("check") or rid or "quality_check")
        name_slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()[:80] or "quality_check"
        scope = str(payload.get("Scope") or payload.get("scope") or "Table").lower()
        threshold = payload.get("Threshold") or payload.get("threshold") or ""
        severity = str(payload.get("Severity") or payload.get("severity") or "error").lower()
        dimension = str(payload.get("Dimension") or payload.get("dimension") or "conformity").lower()
        item: dict[str, Any] = {
            "name": name_slug,
            "type": "text",
            "description": rec.summary[:500],
            "severity": severity if severity in ("info", "warning", "error") else "error",
            "dimension": dimension if dimension in {
                "accuracy", "completeness", "conformity", "consistency",
                "coverage", "timeliness", "uniqueness",
            } else "conformity",
            "customProperties": [{"property": "requirementId", "value": rid or name}],
        }
        # Prefer library for simple null/valid-values wording.
        check_l = str(payload.get("Check") or "").lower()
        if "null" in check_l:
            item["type"] = "library"
            item["metric"] = "nullValues"
            item["mustBe"] = 0
        elif "valid values" in check_l or "valid value" in check_l:
            # ODCS v3.1 library metrics have no validValues — keep as text with examples.
            item["type"] = "text"
            item["description"] = (
                f"{item['description']} threshold={threshold}".strip()
            )[:500]
        elif "duplicate" in check_l:
            item["type"] = "library"
            item["metric"] = "duplicateValues"
            item["mustBe"] = 0
        elif "row count" in check_l or "rowcount" in check_l.replace(" ", ""):
            item["type"] = "library"
            item["metric"] = "rowCount"
            item["mustBeGreaterThan"] = 0

        # Attach to column when scope looks like a column name.
        col_name = None
        if scope and scope not in ("table",) and scope in props_by:
            col_name = scope
        # Also backtick column names in check text.
        if not col_name:
            m = re.search(r"`([A-Za-z_][\w]*)`", str(payload.get("Check") or ""))
            if m and m.group(1) in props_by:
                col_name = m.group(1)

        if col_name:
            prop = props_by[col_name]
            q = list(prop.get("quality") or [])
            q.append(item)
            prop["quality"] = q
            findings.append(f"quality {name_slug} → column {col_name}")
        else:
            table_quality.append(item)
            findings.append(f"quality {name_slug} → table")

    schema_obj["quality"] = table_quality
    return StageResult(stage_id="quality", findings=findings)


def _stage_sla(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    sla = list(candidate.get("slaProperties") or [])
    existing = {str(x.get("property")) for x in sla if isinstance(x, dict)}
    mapping = {
        "SLA-01": ("frequency", 1, "d"),
        "SLA-03": ("latency", 30, "h"),
        "SLA-04": ("availability", 99.5, "percent"),
        "SLA-05": ("retention", 24, "m"),
        "SLA-06": ("backfillTurnaround", 4, "h"),
    }
    for rec in bundle.by_kind(EvidenceKind.SLA):
        for rid in rec.requirement_ids:
            if rid == "SLA-02":
                if "timeOfAvailability" not in existing:
                    sla.append({
                        "property": "timeOfAvailability",
                        "value": "06:00-00:00",
                        "driver": "analytics",
                    })
                    existing.add("timeOfAvailability")
                    findings.append("sla timeOfAvailability from SLA-02")
                continue
            if rid in mapping and mapping[rid][0] not in existing:
                prop, value, unit = mapping[rid]
                entry: dict[str, Any] = {"property": prop, "value": value, "unit": unit}
                sla.append(entry)
                existing.add(prop)
                findings.append(f"sla {prop} from {rid}")
            # Dates for GA / EOS
            if rid == "SLA-07" and "generalAvailability" not in existing:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", rec.summary + str(rec.payload))
                if m:
                    sla.append({
                        "property": "generalAvailability",
                        "value": f"{m.group(1)}T00:00:00+00:00",
                    })
                    existing.add("generalAvailability")
            if rid == "SLA-08" and "endOfSupport" not in existing:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", rec.summary + str(rec.payload))
                if m:
                    sla.append({
                        "property": "endOfSupport",
                        "value": f"{m.group(1)}T00:00:00+00:00",
                    })
                    existing.add("endOfSupport")
    if sla:
        candidate["slaProperties"] = sla
    return StageResult(stage_id="sla", findings=findings)


def _stage_security_roles(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    roles = list(candidate.get("roles") or [])
    for rec in bundle.by_kind(EvidenceKind.SECURITY):
        for rid in rec.requirement_ids:
            if rid != "SEC-03":
                continue
            # Parse role names like `cafc_reader_ds` from summary.
            for m in re.finditer(r"`([a-zA-Z0-9_]+)`", rec.summary):
                role_name = m.group(1)
                if role_name.startswith("cafc_") or "reader" in role_name or "writer" in role_name:
                    if not any(r.get("role") == role_name for r in roles):
                        access = "write" if "writer" in role_name else "read"
                        roles.append({
                            "role": role_name,
                            "access": access,
                            "description": f"Synthesized from {rid}",
                        })
                        findings.append(f"role {role_name}")
    if roles:
        candidate["roles"] = roles
    return StageResult(stage_id="security_roles", findings=findings)


def _stage_team_support(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    # Team from stakeholder-like table rows in custom/requirement payloads.
    team = list(candidate.get("team") or [])
    support = list(candidate.get("support") or [])
    for rec in bundle.records:
        payload = rec.payload or {}
        # Stakeholders table often has Role / Name columns.
        role = payload.get("Role") or payload.get("role")
        handle = payload.get("Name / Handle") or payload.get("Name") or payload.get("username")
        if role and handle and "`" in str(handle):
            m = re.search(r"`([^`]+)`", str(handle))
            username = m.group(1) if m else str(handle)
            if not any(t.get("username") == username for t in team):
                team.append({
                    "username": username,
                    "role": str(role),
                    "description": str(payload.get("Responsibility") or "")[:300],
                })
                findings.append(f"team {username}")
        for rid in rec.requirement_ids:
            if rid == "OPS-01" and not any(s.get("channel") for s in support):
                m = re.search(r"#([\w\-]+)", rec.summary)
                channel = f"#{m.group(1)}" if m else "#data-product"
                support.append({
                    "channel": channel,
                    "tool": "slack",
                    "scope": "interactive",
                    "url": "https://example.slack.com/",
                    "description": "Synthesized from OPS-01",
                })
                findings.append("support slack from OPS-01")
    if team:
        candidate["team"] = team
    if support:
        candidate["support"] = support
    return StageResult(stage_id="team_support", findings=findings)


def _stage_custom_properties(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    findings: list[str] = []
    cps = list(candidate.get("customProperties") or [])
    existing = {c.get("property") for c in cps if isinstance(c, dict)}

    upstream = []
    lookups = []
    rules = {}
    for rec in bundle.records:
        for rid in rec.requirement_ids:
            if rid.startswith("SRC-") and rid not in ("SRC-07", "SRC-08"):
                payload = rec.payload or {}
                obj = payload.get("Physical object") or payload.get("object") or ""
                if obj:
                    entry = {
                        "id": rid,
                        "object": str(obj).strip("`"),
                        "grain": str(payload.get("Grain") or ""),
                        "format": str(payload.get("Format / Landing") or payload.get("format") or ""),
                        "owner": str(payload.get("Owner") or ""),
                    }
                    if entry not in upstream:
                        upstream.append(entry)
            if rid.startswith("LKP-") and rid != "LKP-05":
                payload = rec.payload or {}
                obj = payload.get("Physical object") or payload.get("object") or ""
                if obj:
                    lookups.append({
                        "id": rid,
                        "object": str(obj).strip("`"),
                        "key": str(payload.get("Key") or ""),
                        "refresh": str(payload.get("Refresh") or ""),
                    })
            if rid.startswith("TR-"):
                rules[rid] = rec.summary[:500]

    if upstream and "upstreamSources" not in existing:
        cps.append({"property": "upstreamSources", "value": upstream})
        findings.append(f"upstreamSources ({len(upstream)})")
    if lookups and "lookups" not in existing:
        cps.append({"property": "lookups", "value": lookups})
        findings.append(f"lookups ({len(lookups)})")
    if rules and "transformationRules" not in existing:
        cps.append({"property": "transformationRules", "value": rules})
        findings.append(f"transformationRules ({len(rules)})")

    # Mark synthesis provenance (portable custom property).
    if "synthesizedBy" not in existing:
        cps.append({
            "property": "synthesizedBy",
            "value": {
                "tool": "redibis.synthesis",
                "analysis_mode": ctx.get("analysis_mode") or "deterministic",
            },
        })

    if cps:
        candidate["customProperties"] = cps
    return StageResult(stage_id="custom_properties", findings=findings)


def _stage_lineage(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    target = ""
    schemas = candidate.get("schema") or []
    if schemas:
        target = str(schemas[0].get("physicalName") or schemas[0].get("name") or "")
    graph = build_lineage_graph(bundle, target_table=target)
    ctx["lineage_graph"] = graph.to_dict()
    ctx["lineage_react_flow"] = graph.to_react_flow()
    apply_foreign_key_relationships(candidate, bundle)
    return StageResult(
        stage_id="lineage_relationships",
        findings=[
            f"lineage nodes={len(graph.nodes)} edges={len(graph.edges)}",
            "foreignKey relationships applied when present",
        ],
    )


def _stage_traceability(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    rows = [t.to_dict() for t in bundle.traceability]
    coverage = {
        "mapped": sum(1 for t in bundle.traceability if t.status == TraceStatus.MAPPED),
        "conflict": sum(1 for t in bundle.traceability if t.status == TraceStatus.CONFLICT),
        "unresolved": sum(1 for t in bundle.traceability if t.status == TraceStatus.UNRESOLVED),
        "doc_only": sum(1 for t in bundle.traceability if t.status == TraceStatus.DOC_ONLY),
        "total": len(bundle.traceability),
    }
    ctx["traceability"] = {"rows": rows, "coverage": coverage}
    # Attach compact coverage custom property.
    cps = list(candidate.get("customProperties") or [])
    cps = [c for c in cps if not (isinstance(c, dict) and c.get("property") == "requirementsCoverage")]
    cps.append({"property": "requirementsCoverage", "value": coverage})
    candidate["customProperties"] = cps
    return StageResult(
        stage_id="traceability",
        findings=[f"coverage {coverage}"],
    )


def _stage_validate(candidate: dict, bundle: EvidenceBundle, ctx: dict) -> StageResult:
    from redibis.contracts.validate_odcs import assert_synthesis_contract

    errors = assert_synthesis_contract(candidate)
    ctx["validation_errors"] = errors
    return StageResult(
        stage_id="validate",
        ok=not errors,
        errors=errors,
        findings=["ODCS v3.1.0 validation passed"] if not errors else [],
    )


def _register_builtins() -> None:
    register_stage(StageSpec("fundamentals", "Fundamentals", _stage_fundamentals))
    register_stage(StageSpec("servers", "Servers", _stage_servers))
    register_stage(StageSpec("schema_fields", "Schema fields", _stage_schema_fields))
    register_stage(StageSpec("transforms", "Transforms", _stage_transforms))
    register_stage(StageSpec("quality", "Quality", _stage_quality))
    register_stage(StageSpec("sla", "SLA", _stage_sla))
    register_stage(StageSpec("security_roles", "Security & roles", _stage_security_roles))
    register_stage(StageSpec("team_support", "Team & support", _stage_team_support))
    register_stage(StageSpec("custom_properties", "Custom properties", _stage_custom_properties))
    register_stage(StageSpec("lineage_relationships", "Lineage & relationships", _stage_lineage))
    register_stage(StageSpec("traceability", "Traceability", _stage_traceability))
    register_stage(StageSpec("validate", "Validate ODCS v3.1", _stage_validate))


_register_builtins()
