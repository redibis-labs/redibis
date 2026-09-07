"""
redibis.pii.contract_writer
============================
PIIContractWriter — produces a PII-only ODCS v3 partial contract using
the official OpenDataContractStandard Pydantic models.

This class builds a partial ODCS contract from PIIDetection objects,
with NO quality blocks. Quality blocks come from QualityContractWriter.
ContractStore.upsert() merges them.

PII policy lives in a first-class ``privacy`` block per column
(``classification`` + ``masking_policy``). Durable ``entity_type`` is set on
the property. Run-specific evidence is returned via ``build_column_telemetry()``
for ``ContractMetadataStore`` — never embedded in the active spec.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from redibis.models import PIIDetection
from redibis.pii.sensitivity import classify_sensitivity


def _safe_import_odcs():
    try:
        from open_data_contract_standard.model import (
            OpenDataContractStandard, SchemaObject, SchemaProperty,
            CustomProperty,
        )
        return OpenDataContractStandard, SchemaObject, SchemaProperty, CustomProperty
    except ImportError:
        return None, None, None, None


class PIIContractWriter:
    """
    Builds a PII-only ODCS partial contract.

    Uses ODCS Pydantic models if available (validated, lintable),
    falls back to plain dict construction otherwise.

    Usage:
        writer = PIIContractWriter(
            database_name = "telecom",
            table_name    = "customers",
            equation_used = "balanced",
            run_id        = "2026-05-09_14-32-18",
        )
        for d in detections:
            writer.add_detection(d)
        partial = writer.build()
        store.upsert(partial, table="telecom.customers", workflow="pii")

        # Export to other formats:
        from redibis.contracts.exporter import export_contract
        soda_yaml = export_contract(partial, "sodacl")
    """

    def __init__(
        self,
        database_name:      str,
        table_name:         str,
        equation_used:      str = "independent",
        run_id:             str = "",
        physical_table_name: str = "",
        thresholds=None,
        masking_roles=None,
        column_dtypes: Optional[dict] = None,
    ):
        self.database_name       = database_name
        self.table_name          = table_name
        self.physical_table_name = physical_table_name or f"{database_name}.{table_name}"
        self.equation_used       = equation_used
        self.run_id              = run_id
        self.thresholds          = thresholds
        from redibis.contracts.masking_policy import resolve_role_config
        self._role_config        = resolve_role_config(masking_roles)
        self._column_dtypes = column_dtypes or {}
        self._detections: List[PIIDetection] = []
        self._enhancement_status: Dict[str, bool] = {
            "llm_refiner_run":     False,
            "llm_refiner_planned": True,
        }

    def add_detection(self, detection: PIIDetection) -> "PIIContractWriter":
        self._detections.append(detection)
        return self

    def add_detections(self, detections: List[PIIDetection]) -> "PIIContractWriter":
        self._detections.extend(detections)
        return self

    def set_enhancement_status(self, **kwargs) -> "PIIContractWriter":
        self._enhancement_status.update(kwargs)
        return self

    def build(self) -> dict:
        """
        Build the PII-only ODCS partial contract.

        Transient keys ``_scan_metadata`` and ``_column_telemetry`` are included
        for ``ContractStore.upsert()`` to route into the metadata sidecar; they
        are stripped from the active contract spec.
        """
        ODCS, SchemaObj, SchemaProp, CustomProp = _safe_import_odcs()

        if ODCS is not None:
            result = self._build_with_pydantic(ODCS, SchemaObj, SchemaProp, CustomProp)
        else:
            result = self._build_as_dict()

        result["_scan_metadata"] = self._build_pii_summary()
        telemetry = self.build_column_telemetry()
        if telemetry:
            result["_column_telemetry"] = telemetry
        return result

    def build_column_telemetry(self) -> dict[str, dict]:
        """Per-column discovery evidence for ``ContractMetadataStore``."""
        from redibis.pii.equations import build_column_report

        out: dict[str, dict] = {}
        for det in self._detections:
            if not det.detected:
                continue
            report = build_column_report(
                det, thresholds=self.thresholds, equation=self.equation_used,
            )
            engines = det.detection_engines()
            entry: dict[str, Any] = {
                "entity_type": det.entity_type,
                "confidence": det.confidence,
                "discovery_engines": engines,
                "run_id": self.run_id,
            }
            if report.decision_rule:
                entry["decision_rule"] = report.decision_rule
            if report.advisory:
                entry["advisory"] = report.advisory
            if det.regex_hits:
                entry["regex_hits"] = list(det.regex_hits[:10])
            if det.ner_hits:
                entry["ner_hits"] = list(det.ner_hits[:10])
            if det.presidio_score is not None:
                entry["presidio_score"] = det.presidio_score
            if det.gliner_score is not None:
                entry["gliner_score"] = det.gliner_score
            if det.phone_score is not None:
                entry["phone_score"] = det.phone_score
            if det.arabic_aware:
                entry["arabic_aware"] = True
            out[det.column] = entry
        return out

    def _property_for_detection(self, det: PIIDetection) -> dict:
        from redibis.contracts.privacy import build_privacy_block
        from redibis.contracts.type_inference import infer_types_from_dtype

        prop: dict[str, Any] = {"name": det.column}
        if det.column in self._column_dtypes:
            physical, logical = infer_types_from_dtype(self._column_dtypes[det.column])
            prop["logicalType"] = logical
            if physical and physical != "string":
                prop["physicalType"] = physical
        if det.detected:
            prop["classification"] = classify_sensitivity(det.entity_type)
            prop["entity_type"] = det.entity_type
            prop["tags"] = self._build_tags(det)
            privacy = build_privacy_block(
                masking_policy=self._build_masking_policy(det),
                classification=prop["classification"],
            )
            if privacy:
                prop["privacy"] = privacy
        return prop

    # ── Pydantic construction (preferred) ─────────────────────────────────

    def _build_with_pydantic(self, ODCS, SchemaObj, SchemaProp, CustomProp) -> dict:
        properties = []
        for det in self._detections:
            raw = self._property_for_detection(det)
            sp_kw = {k: v for k, v in raw.items() if k != "privacy"}
            sp = SchemaProp(**sp_kw)
            properties.append(sp)

        model_name = f"{self.database_name}_{self.table_name}"
        contract = ODCS(
            apiVersion="v3.0.1",
            kind="DataContract",
            name=f"{model_name}_contract",
            version="1.0.0",
            status="active",
            schema=[SchemaObj(
                name=model_name,
                physicalName=self.physical_table_name,
                properties=properties,
            )],
        )

        result = contract.model_dump(exclude_none=True, by_alias=True)
        result["database_name"] = self.database_name
        result["table_name"] = self.table_name

        det_by_col = {d.column: d for d in self._detections}
        for schema_obj in result.get("schema", []):
            for prop in schema_obj.get("properties", []):
                det = det_by_col.get(prop.get("name"))
                if not det:
                    continue
                built = self._property_for_detection(det)
                for key in ("privacy", "entity_type", "tags", "classification",
                            "logicalType", "physicalType"):
                    if built.get(key) is not None:
                        prop[key] = built[key]
        return result

    # ── Dict construction (fallback) ──────────────────────────────────────

    def _build_as_dict(self) -> dict:
        properties = [self._property_for_detection(det) for det in self._detections]
        model_name = f"{self.database_name}_{self.table_name}"
        return {
            "apiVersion": "v3.0.1",
            "kind": "DataContract",
            "name": f"{model_name}_contract",
            "database_name": self.database_name,
            "table_name": self.table_name,
            "version": "1.0.0",
            "status": "active",
            "schema": [{
                "name": model_name,
                "physicalName": self.physical_table_name,
                "logicalType": "object",
                "properties": properties,
            }],
        }

    # ── Shared helpers ────────────────────────────────────────────────────

    def _build_tags(self, det: PIIDetection) -> list[str]:
        pii_class = classify_sensitivity(det.entity_type)
        if pii_class == "security_sensitive":
            tags = ["security_sensitive"]
        elif pii_class == "pii_indirect":
            tags = ["pii_indirect", "gdpr_personal_data"]
        elif pii_class == "internal":
            # Non-personal evidence (e.g. ORGANIZATION, NETWORK_ID) -- detected
            # for telemetry/audit, but never a PII/GDPR tag.
            tags = []
        else:
            tags = ["pii", "gdpr_personal_data"]
        if det.arabic_aware:
            tags.append("contains_arabic")
        return tags

    def _build_masking_policy(self, det: PIIDetection) -> Optional[dict]:
        if not det.detected:
            return None
        pii_class = classify_sensitivity(det.entity_type)
        if pii_class == "internal":
            # Non-personal entity (e.g. ORGANIZATION) -- nothing to mask.
            return None
        from redibis.contracts.masking_policy import build_masking_policy
        return build_masking_policy(
            det.entity_type, detected=True,
            classification=pii_class,
            role_config=self._role_config,
        )

    def _decision_rule(self, det: PIIDetection) -> str:
        from redibis.pii.equations import build_column_report
        report = build_column_report(det, thresholds=self.thresholds,
                                     equation=self.equation_used)
        return report.decision_rule

    def _build_pii_summary(self) -> dict:
        from redibis.pii.sensitivity import highest_sensitivity_level

        flagged = [d for d in self._detections if d.detected]
        pii_confirmed = [
            d for d in flagged
            if classify_sensitivity(d.entity_type) not in ("security_sensitive", "internal")
        ]
        security_confirmed = [
            d for d in flagged
            if classify_sensitivity(d.entity_type) == "security_sensitive"
        ]
        uncertain = [d for d in self._detections if d.llm_verdict == "UNCERTAIN"]
        clean = [d for d in self._detections
                 if not d.detected and d.llm_verdict != "UNCERTAIN"]
        arabic = [d.column for d in self._detections if d.arabic_aware]

        return {
            "scan_run_id":           self.run_id,
            "equation_used":         self.equation_used,
            "total_columns":         len(self._detections),
            "pii_confirmed":         len(pii_confirmed),
            "pii_uncertain":         len(uncertain),
            "pii_clean":             len(clean),
            "pii_columns":           [d.column for d in pii_confirmed],
            "security_sensitive_columns": [d.column for d in security_confirmed],
            "uncertain_columns":     [d.column for d in uncertain],
            "arabic_aware_columns":  arabic,
            "highest_sensitivity":   highest_sensitivity_level(
                [classify_sensitivity(d.entity_type) for d in flagged],
            ),
            "enhancement_status":    self._enhancement_status,
        }
