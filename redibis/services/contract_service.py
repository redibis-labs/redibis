"""
redibis.services.contract_service
==================================
ContractService — update saved contracts with business definitions,
descriptions, SQL constraints, tags, and other metadata.

This service handles Workflow C (Business Import) — the workflow where
a business user or data steward enriches an existing contract with:
  - Table-level business description and purpose
  - Column-level business definitions (what each column means)
  - SQL quality constraints (business rules expressed as SQL)
  - Tags and classification overrides
  - Authoritative definitions (links to glossary, wiki, etc.)

The service updates the EXISTING active contract via ContractStore.upsert()
with workflow="business". The merger combines the business contribution with
existing quality and PII blocks without overwriting them.

Pure business logic. No REST/CLI knowledge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from redibis.store.contract_store import ContractStore, UpsertResult
from redibis.store.storage_backend import StorageBackend
from redibis.contracts.mapper import sql_rule_to_odcs

log = logging.getLogger(__name__)


@dataclass
class ColumnDefinition:
    """Business definition for a single column."""
    column:           str
    description:      str                           = ""
    business_name:    str                           = ""
    classification:   Optional[str]                 = None
    tags:             list[str]                     = field(default_factory=list)
    is_pii_override:  Optional[bool]                = None
    critical_data_element: bool                     = False
    examples:         list[str]                     = field(default_factory=list)


@dataclass
class SQLConstraint:
    """A business SQL quality rule."""
    query:           str
    description:     str                            = ""
    max_failures:    int                            = 0
    severity:        str                            = "error"
    dimension:       str                            = "accuracy"


@dataclass
class BusinessUpdate:
    """Complete business update payload for a table."""
    table:              str
    table_description:  str                         = ""
    table_purpose:      str                         = ""
    domain:             str                         = ""
    owner:              str                         = ""
    columns:            list[ColumnDefinition]       = field(default_factory=list)
    sql_constraints:    list[SQLConstraint]          = field(default_factory=list)
    tags:               list[str]                    = field(default_factory=list)


class ContractService:
    """
    Updates saved contracts with business definitions and constraints.

    This service handles Workflow C (Business Import). The REST endpoint
    and CLI command are thin adapters over this class.

    Usage:
        service = ContractService(backend=backend, store=store)

        # Add business definitions for columns
        service.update_column_definitions(
            table   = "telecom.customers",
            columns = [
                ColumnDefinition(
                    column      = "phone",
                    description = "Customer primary contact number in E.164 format",
                    business_name = "Primary Phone",
                    tags        = ["contact", "regulated"],
                ),
                ColumnDefinition(
                    column      = "national_id",
                    description = "Egyptian national ID (14 digits)",
                    business_name = "National ID",
                    classification = "pii_sensitive",
                    critical_data_element = True,
                ),
            ],
        )

        # Add SQL business rules
        service.add_sql_constraints(
            table       = "telecom.customers",
            constraints = [
                SQLConstraint(
                    query       = "SELECT * FROM ${object} WHERE balance < 0 AND status = 'active'",
                    description = "Active customers cannot have negative balance",
                ),
            ],
        )

        # Full business update (from YAML file)
        service.apply_business_yaml("telecom.customers", "business_defs.yaml")

        # Bulk import from YAML
        service.import_business_file("business_glossary.yaml")
    """

    def __init__(
        self,
        backend:  StorageBackend,
        store:    ContractStore,
    ) -> None:
        self.backend = backend
        self.store   = store

    # ── Column definitions ────────────────────────────────────────────────

    def update_column_definitions(
        self,
        table:   str,
        columns: list[ColumnDefinition],
        run_id:  str = "",
    ) -> UpsertResult:
        """
        Update business definitions for specific columns.

        Merges with the existing contract — does NOT overwrite quality or
        PII blocks. Only touches the fields specified in each ColumnDefinition.
        """
        partial = self._build_column_partial(table, columns)
        return self.store.upsert(
            partial  = partial,
            table    = table,
            workflow = "business",
            run_id   = run_id or "business_column_update",
        )

    # ── SQL constraints ───────────────────────────────────────────────────

    def add_sql_constraints(
        self,
        table:       str,
        constraints: list[SQLConstraint],
        run_id:      str = "",
    ) -> UpsertResult:
        """
        Add SQL-based business quality rules to the contract.

        These become ODCS DataQuality entries with type="sql" at the
        schema (table) level.
        """
        partial = self._build_sql_partial(table, constraints)
        return self.store.upsert(
            partial  = partial,
            table    = table,
            workflow = "business",
            run_id   = run_id or "business_sql_rules",
        )

    # ── Full business update ──────────────────────────────────────────────

    def apply_business_update(
        self,
        update: BusinessUpdate,
        run_id: str = "",
    ) -> UpsertResult:
        """
        Apply a complete BusinessUpdate — table description, column
        definitions, SQL constraints, and tags — in one upsert.
        """
        partial = self._build_full_partial(update)
        return self.store.upsert(
            partial  = partial,
            table    = update.table,
            workflow = "business",
            run_id   = run_id or "business_full_update",
        )

    # ── YAML import ───────────────────────────────────────────────────────

    def apply_business_yaml(
        self,
        table:     str,
        yaml_path: str,
        run_id:    str = "",
    ) -> UpsertResult:
        """
        Read a YAML file with business definitions and apply to a table.

        Expected YAML structure:
            description: "Customer master data"
            purpose: "360-degree customer view for billing and support"
            domain: "customer"
            owner: "data-engineering"
            tags: ["regulated", "gdpr"]
            columns:
              phone:
                description: "Primary contact number in E.164 format"
                business_name: "Primary Phone"
                tags: ["contact"]
                critical_data_element: true
              national_id:
                description: "Egyptian national ID (14 digits)"
                classification: "pii_sensitive"
            sql_constraints:
              - query: "SELECT * FROM ${object} WHERE balance < 0 AND status = 'active'"
                description: "Active customers cannot have negative balance"
              - query: "SELECT * FROM ${object} WHERE phone IS NULL AND account_type = 'postpaid'"
                description: "Postpaid accounts must have a phone number"
        """
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        update = self._parse_yaml_to_update(table, data)
        return self.apply_business_update(update, run_id=run_id)

    def import_business_file(
        self,
        yaml_path: str,
        run_id:    str = "",
    ) -> list[UpsertResult]:
        """
        Bulk import business definitions for multiple tables from one YAML.

        Expected YAML structure:
            tables:
              telecom.customers:
                description: "..."
                columns:
                  phone:
                    description: "..."
              telecom.orders:
                description: "..."
                columns:
                  order_id:
                    description: "..."
        """
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        results = []
        tables_data = data.get("tables", {})
        for table, table_data in tables_data.items():
            update = self._parse_yaml_to_update(table, table_data)
            result = self.apply_business_update(update, run_id=run_id)
            results.append(result)
            log.info(f"Business update applied for {table}: v{result.version_after}")

        return results

    # ── Convenience: single-column update ─────────────────────────────────

    def set_column_description(
        self,
        table:       str,
        column:      str,
        description: str,
    ) -> UpsertResult:
        """Quick update: set a single column's business description."""
        return self.update_column_definitions(
            table=table,
            columns=[ColumnDefinition(column=column, description=description)],
        )

    def set_table_description(
        self,
        table:       str,
        description: str,
        purpose:     str = "",
    ) -> UpsertResult:
        """Quick update: set the table-level description."""
        update = BusinessUpdate(
            table=table,
            table_description=description,
            table_purpose=purpose,
        )
        return self.apply_business_update(update)

    def add_tags(
        self,
        table: str,
        tags:  list[str],
    ) -> UpsertResult:
        """Quick update: add tags to the contract (merge as set union)."""
        update = BusinessUpdate(table=table, tags=tags)
        return self.apply_business_update(update)

    def override_classification(
        self,
        table:          str,
        column:         str,
        classification: str,
    ) -> UpsertResult:
        """
        Override the PII classification for a column.

        Use when the automated detection is wrong — e.g. a column was flagged
        as PII but is actually public, or vice versa.
        """
        return self.update_column_definitions(
            table=table,
            columns=[ColumnDefinition(
                column=column,
                classification=classification,
            )],
            run_id="classification_override",
        )

    # ── Private: contract partial builders ────────────────────────────────

    def _build_column_partial(
        self,
        table:   str,
        columns: list[ColumnDefinition],
    ) -> dict:
        """Build a partial contract dict from column definitions."""
        db_name, tbl_name = self._split_table(table)

        properties = []
        for col in columns:
            prop: Dict[str, Any] = {"name": col.column}
            if col.description:
                prop["description"] = col.description
            if col.business_name:
                prop["businessName"] = col.business_name
            if col.classification:
                prop["classification"] = col.classification
            if col.tags:
                prop["tags"] = col.tags
            if col.critical_data_element:
                prop["criticalDataElement"] = True
            if col.examples:
                prop["examples"] = col.examples
            if col.is_pii_override is not None:
                if "pii" not in prop:
                    prop["pii"] = {}
                prop["pii"]["detected"] = col.is_pii_override
                prop["pii"]["override"] = True
            properties.append(prop)

        return {
            "apiVersion":    "v3.0.1",
            "kind":          "DataContract",
            "name":          f"{db_name}_{tbl_name}_contract",
            "database_name": db_name,
            "table_name":    tbl_name,
            "schema": [{
                "name":         f"{db_name}_{tbl_name}",
                "physicalName": table,
                "properties":   properties,
            }],
        }

    def _build_sql_partial(
        self,
        table:       str,
        constraints: list[SQLConstraint],
    ) -> dict:
        """Build a partial contract dict with SQL quality rules."""
        db_name, tbl_name = self._split_table(table)

        quality_rules = []
        for c in constraints:
            rule = sql_rule_to_odcs(c.query, c.max_failures, c.description)
            if c.severity:
                rule["severity"] = c.severity
            if c.dimension:
                rule["dimension"] = c.dimension
            quality_rules.append(rule)

        return {
            "apiVersion":    "v3.0.1",
            "kind":          "DataContract",
            "name":          f"{db_name}_{tbl_name}_contract",
            "database_name": db_name,
            "table_name":    tbl_name,
            "schema": [{
                "name":         f"{db_name}_{tbl_name}",
                "physicalName": table,
                "quality":      quality_rules,
            }],
        }

    def _build_full_partial(self, update: BusinessUpdate) -> dict:
        """Build a partial contract from a full BusinessUpdate."""
        db_name, tbl_name = self._split_table(update.table)

        # Column properties
        properties = []
        for col in update.columns:
            prop: Dict[str, Any] = {"name": col.column}
            if col.description:
                prop["description"] = col.description
            if col.business_name:
                prop["businessName"] = col.business_name
            if col.classification:
                prop["classification"] = col.classification
            if col.tags:
                prop["tags"] = col.tags
            if col.critical_data_element:
                prop["criticalDataElement"] = True
            if col.examples:
                prop["examples"] = col.examples
            properties.append(prop)

        # SQL rules
        quality_rules = []
        for c in update.sql_constraints:
            rule = sql_rule_to_odcs(c.query, c.max_failures, c.description)
            if c.severity:
                rule["severity"] = c.severity
            if c.dimension:
                rule["dimension"] = c.dimension
            quality_rules.append(rule)

        contract: Dict[str, Any] = {
            "apiVersion":    "v3.0.1",
            "kind":          "DataContract",
            "name":          f"{db_name}_{tbl_name}_contract",
            "database_name": db_name,
            "table_name":    tbl_name,
            "schema": [{
                "name":         f"{db_name}_{tbl_name}",
                "physicalName": update.table,
                "properties":   properties,
            }],
        }

        if quality_rules:
            contract["schema"][0]["quality"] = quality_rules

        if update.tags:
            contract["tags"] = update.tags

        if update.table_description or update.table_purpose:
            contract["description"] = {}
            if update.table_description:
                contract["description"]["description"] = update.table_description
            if update.table_purpose:
                contract["description"]["purpose"] = update.table_purpose

        if update.domain:
            contract["domain"] = update.domain

        return contract

    def _parse_yaml_to_update(self, table: str, data: dict) -> BusinessUpdate:
        """Parse a YAML dict into a BusinessUpdate dataclass."""
        columns = []
        for col_name, col_data in data.get("columns", {}).items():
            if isinstance(col_data, str):
                columns.append(ColumnDefinition(
                    column=col_name, description=col_data,
                ))
            elif isinstance(col_data, dict):
                columns.append(ColumnDefinition(
                    column              = col_name,
                    description         = col_data.get("description", ""),
                    business_name       = col_data.get("business_name", ""),
                    classification      = col_data.get("classification"),
                    tags                = col_data.get("tags", []),
                    critical_data_element = col_data.get("critical_data_element", False),
                    examples            = col_data.get("examples", []),
                ))

        constraints = []
        for sc in data.get("sql_constraints", []):
            constraints.append(SQLConstraint(
                query        = sc.get("query", ""),
                description  = sc.get("description", ""),
                max_failures = sc.get("max_failures", 0),
                severity     = sc.get("severity", "error"),
                dimension    = sc.get("dimension", "accuracy"),
            ))

        return BusinessUpdate(
            table              = table,
            table_description  = data.get("description", ""),
            table_purpose      = data.get("purpose", ""),
            domain             = data.get("domain", ""),
            owner              = data.get("owner", ""),
            columns            = columns,
            sql_constraints    = constraints,
            tags               = data.get("tags", []),
        )

    @staticmethod
    def _split_table(table: str) -> tuple[str, str]:
        if "." in table:
            db, tbl = table.split(".", 1)
            return db, tbl
        return "", table
