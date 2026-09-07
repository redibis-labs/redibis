"""
redibis.contracts.exporter
==========================
Thin wrapper around datacontract-cli export. Converts an ODCS contract dict
to any supported format (Soda, GE, dbt, SQL, HTML, Iceberg, etc.).

Usage:
    from redibis.contracts.exporter import export_contract

    soda_yaml   = export_contract(contract, "sodacl")
    ge_json     = export_contract(contract, "great-expectations")
    dbt_yaml    = export_contract(contract, "dbt-sources")
    sql_ddl     = export_contract(contract, "sql", sql_server_type="postgres")
    html_report = export_contract(contract, "html")

Supported formats (as of datacontract-cli 0.12+):
    sodacl, great-expectations, dbt-sources, dbt-models, dbt-staging-sql,
    sql, sql-query, html, markdown, jsonschema, pydantic-model, avro,
    avro-idl, protobuf, rdf, bigquery, dbml, spark, sqlalchemy, iceberg,
    data-caterer, dcs, excel, dqx, mermaid, go, custom, odcs
"""

from __future__ import annotations

from typing import Any, Optional


# Available formats for quick reference
EXPORT_FORMATS = [
    "sodacl", "great-expectations", "dbt-sources", "dbt-models",
    "sql", "sql-query", "html", "markdown", "jsonschema",
    "pydantic-model", "avro", "avro-idl", "protobuf",
    "bigquery", "spark", "sqlalchemy", "iceberg", "excel",
    "mermaid", "odcs",
]


def export_contract(
    contract: dict,
    format: str,
    **kwargs,
) -> str:
    """
    Export an ODCS contract dict to any supported format.

    Args:
        contract : ODCS contract as a dict (from ContractStore.get_active(),
                   QualityContractWriter.build(), PIIContractWriter.build(), etc.)
        format   : Target format string (e.g. "sodacl", "great-expectations")
        **kwargs : Passed through to datacontract export (e.g. sql_server_type="postgres")

    Returns:
        str : The exported content (YAML, JSON, SQL, HTML, etc.)

    Raises:
        ImportError : If datacontract-cli is not installed.
        ValueError  : If the format is not recognized.
    """
    try:
        from datacontract.data_contract import DataContract
        from datacontract.export.exporter import ExportFormat
    except ImportError:
        raise ImportError(
            "datacontract-cli is required for contract export. "
            "Install with: pip install 'datacontract-cli[all]'"
        )

    try:
        export_format = ExportFormat(format)
    except ValueError:
        available = [f.value for f in ExportFormat]
        raise ValueError(
            f"Unknown export format: {format!r}. "
            f"Available: {available}"
        )

    from redibis.contracts.odcs_compat import build_odcs_model
    odcs = build_odcs_model(contract)
    dc = DataContract(data_contract=odcs)
    return dc.export(export_format, **kwargs)


def list_export_formats() -> list[str]:
    """List all available export formats."""
    try:
        from datacontract.export.exporter import ExportFormat
        return [f.value for f in ExportFormat]
    except ImportError:
        return EXPORT_FORMATS