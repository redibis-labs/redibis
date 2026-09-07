"""
Human-readable contract formatters.

Exports ODCS contracts to various readable formats (Markdown, HTML, etc.)
using the datacontract-cli exporter.
"""

from typing import Optional
from redibis.contracts.exporter import export_contract


def format_contract_markdown(contract: dict) -> str:
    """
    Format an ODCS contract as Markdown.

    Args:
        contract: ODCS contract dict

    Returns:
        Markdown string
    """
    return export_contract(contract, "markdown")


def format_contract_html(contract: dict) -> str:
    """
    Format an ODCS contract as HTML.

    Args:
        contract: ODCS contract dict

    Returns:
        HTML string
    """
    return export_contract(contract, "html")


def format_contract_yaml(contract: dict) -> str:
    """
    Format an ODCS contract as human-readable YAML.

    Args:
        contract: ODCS contract dict

    Returns:
        YAML string (formatted)
    """
    import yaml

    # Strip non-ODCS fields
    clean = {k: v for k, v in contract.items()
             if k not in ("database_name", "table_name", "pii_summary")}

    # Also strip 'pii' from property-level dicts
    if "schema" in clean:
        import copy
        clean = copy.deepcopy(clean)
        for schema_obj in clean.get("schema", []):
            for prop in schema_obj.get("properties", []):
                prop.pop("pii", None)

    return yaml.dump(
        clean,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


def format_contract_json(contract: dict) -> str:
    """
    Format an ODCS contract as pretty-printed JSON.

    Args:
        contract: ODCS contract dict

    Returns:
        JSON string (indented)
    """
    import json

    # Strip non-ODCS fields
    clean = {k: v for k, v in contract.items()
             if k not in ("database_name", "table_name", "pii_summary")}

    # Also strip 'pii' from property-level dicts
    if "schema" in clean:
        import copy
        clean = copy.deepcopy(clean)
        for schema_obj in clean.get("schema", []):
            for prop in schema_obj.get("properties", []):
                prop.pop("pii", None)

    return json.dumps(clean, indent=2, ensure_ascii=False)


def format_contract(
    contract: dict,
    format: str = "markdown",
) -> str:
    """
    Format an ODCS contract to a specified format.

    Args:
        contract: ODCS contract dict
        format: Format name ("markdown", "html", "yaml", "json", or any datacontract-cli format)

    Returns:
        Formatted contract as string

    Raises:
        ValueError: If format is not recognized
    """
    if format == "markdown":
        return format_contract_markdown(contract)
    elif format == "html":
        return format_contract_html(contract)
    elif format == "yaml":
        return format_contract_yaml(contract)
    elif format == "json":
        return format_contract_json(contract)
    else:
        # Try datacontract-cli formats
        try:
            return export_contract(contract, format)
        except Exception as e:
            raise ValueError(f"Could not format contract as {format}: {e}")
