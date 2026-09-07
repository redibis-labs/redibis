"""
redibis.quality.contract_writer
================================
QualityContractWriter — builds a quality-only ODCS v3 partial contract
using the official OpenDataContractStandard Pydantic models.

Produces quality blocks ONLY. No pii block. No classification tags.
Those come from PIIContractWriter and the ContractStore merger combines them.

Uses redibis.contracts.mapper to convert GE expectations into ODCS-native
DataQuality rules where possible (portable across Soda/dbt/GE), falling
back to engine-specific blocks for GE-only expectations.
"""

from __future__ import annotations

from typing import Any, List, Optional

from redibis.contracts.mapper import ge_expectation_to_odcs, sql_rule_to_odcs


def _safe_import_odcs():
    """Import ODCS models if available, return None-tuple if not."""
    try:
        from open_data_contract_standard.model import (
            OpenDataContractStandard, SchemaObject, SchemaProperty, DataQuality,
        )
        return OpenDataContractStandard, SchemaObject, SchemaProperty, DataQuality
    except ImportError:
        return None, None, None, None


class QualityContractWriter:
    """
    Builds a quality-only ODCS partial contract from GE expectations.

    Two construction paths:
      1. If open-data-contract-standard is installed: uses official Pydantic
         models with validation. The output is lintable via datacontract-cli.
      2. Fallback: builds the same dict structure manually (backward compat).

    Usage:
        writer = QualityContractWriter(
            database_name = "telecom",
            table_name    = "customers",
            run_id        = "2026-05-09_10-00-00",
        )
        writer.add_expectations(profiler.expectations)
        contract = writer.build()
        # contract is a dict — pass to ContractStore.upsert(workflow="quality")

        # Export to other engines:
        from redibis.contracts.exporter import export_contract
        soda_yaml = export_contract(contract, "sodacl")
    """

    def __init__(
        self,
        database_name:        str,
        table_name:           str,
        physical_table_name:  str = "",
        run_id:               str = "",
        column_dtypes:        Optional[dict] = None,
    ) -> None:
        self.database_name       = database_name
        self.table_name          = table_name
        self.physical_table_name = physical_table_name or f"{database_name}.{table_name}"
        self.run_id              = run_id
        self._column_dtypes      = column_dtypes or {}
        self._expectations: list[Any] = []
        self._sql_rules: list[dict] = []

    def add_expectation(self, expectation: Any) -> "QualityContractWriter":
        """Add a single GE ExpectationConfiguration."""
        self._expectations.append(expectation)
        return self

    def add_expectations(self, expectations: List[Any]) -> "QualityContractWriter":
        """Add a batch of GE ExpectationConfigurations."""
        self._expectations.extend(expectations)
        return self

    def add_sql_rule(
        self,
        query: str,
        max_failures: int = 0,
        description: Optional[str] = None,
    ) -> "QualityContractWriter":
        """Add a custom SQL quality rule."""
        self._sql_rules.append(sql_rule_to_odcs(query, max_failures, description))
        return self

    def build(self) -> dict:
        """
        Build the quality-only ODCS partial contract.

        Returns a dict (model_dump output) that:
          - Validates against the ODCS JSON schema
          - Is exportable via datacontract-cli to Soda/GE/dbt
          - Is mergeable via ContractStore.upsert()
        """
        # Group expectations by column
        table_level_exps = []
        col_exps: dict[str, list] = {}

        for exp in self._expectations:
            kwargs = getattr(exp, "kwargs", {})
            if isinstance(kwargs, dict):
                col = kwargs.get("column")
            else:
                col = None

            if col is None:
                table_level_exps.append(exp)
            else:
                col_exps.setdefault(col, []).append(exp)

        # Convert GE expectations to ODCS DataQuality dicts via the mapper
        table_quality = [ge_expectation_to_odcs(exp) for exp in table_level_exps]
        table_quality.extend(self._sql_rules)

        # Build per-column property dicts
        properties = []
        for col in sorted(col_exps.keys()):
            expectations = col_exps[col]
            quality_rules = [ge_expectation_to_odcs(exp) for exp in expectations]

            from redibis.contracts.type_inference import infer_types_from_dtype

            prop: dict = {"name": col, "quality": quality_rules}
            if col in (self._column_dtypes or {}):
                physical, logical = infer_types_from_dtype(self._column_dtypes[col])
                prop["logicalType"] = logical
                prop["physicalType"] = physical
            else:
                prop["logicalType"] = "string"

            # Infer schema attributes from expectations (GE type expectation wins)
            for exp in expectations:
                exp_type = getattr(exp, "expectation_type", "")
                kwargs = getattr(exp, "kwargs", {})
                if exp_type == "expect_column_values_to_not_be_null":
                    if kwargs.get("mostly", 1.0) > 0:
                        prop["required"] = True
                elif exp_type == "expect_column_values_to_be_unique":
                    prop["unique"] = True
                elif exp_type == "expect_column_values_to_be_of_type":
                    physical_type = kwargs.get("type_")
                    if physical_type:
                        prop["physicalType"] = physical_type
                        prop["logicalType"] = self._infer_logical_type(physical_type)

            properties.append(prop)

        # Build the contract using ODCS Pydantic models if available
        ODCS, SchemaObj, SchemaProp, DQ = _safe_import_odcs()

        if ODCS is not None:
            return self._build_with_pydantic(
                ODCS, SchemaObj, SchemaProp, DQ,
                properties, table_quality,
            )
        else:
            return self._build_as_dict(properties, table_quality)

    def _build_with_pydantic(
        self, ODCS, SchemaObj, SchemaProp, DQ,
        properties: list[dict], table_quality: list[dict],
    ) -> dict:
        """Build using official ODCS Pydantic models (with validation)."""
        schema_properties = []
        for prop in properties:
            quality_objs = [DQ(**q) for q in prop.get("quality", [])]
            sp = SchemaProp(
                name=prop["name"],
                logicalType=prop.get("logicalType", "string"),
                physicalType=prop.get("physicalType"),
                required=prop.get("required"),
                unique=prop.get("unique"),
                quality=quality_objs if quality_objs else None,
            )
            schema_properties.append(sp)

        table_quality_objs = [DQ(**q) for q in table_quality] if table_quality else None

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
                properties=schema_properties,
                quality=table_quality_objs,
            )],
        )

        result = contract.model_dump(exclude_none=True, by_alias=True)
        # Add routing fields for ContractStore.upsert()
        result["database_name"] = self.database_name
        result["table_name"] = self.table_name
        return result

    def _build_as_dict(
        self, properties: list[dict], table_quality: list[dict],
    ) -> dict:
        """Fallback: build as a plain dict (no ODCS validation)."""
        model_name = f"{self.database_name}_{self.table_name}"
        schema_obj = {
            "name": model_name,
            "physicalName": self.physical_table_name,
            "logicalType": "object",
            "properties": properties,
        }
        if table_quality:
            schema_obj["quality"] = table_quality

        return {
            "apiVersion": "v3.0.1",
            "kind": "DataContract",
            "name": f"{model_name}_contract",
            "database_name": self.database_name,
            "table_name": self.table_name,
            "version": "1.0.0",
            "status": "active",
            "schema": [schema_obj],
        }

    @staticmethod
    def _infer_logical_type(physical_type: str) -> str:
        """Infer ODCS logical type from a physical type string."""
        pt = str(physical_type).lower()
        if any(x in pt for x in ("int", "long", "short")):
            return "integer"
        if any(x in pt for x in ("float", "double", "dec", "numeric")):
            return "number"
        if any(x in pt for x in ("date", "time", "timestamp")):
            return "date"
        if "bool" in pt:
            return "boolean"
        return "string"