"""wgea-mcp — MCP server for Workplace Gender Equality Agency public data.

Five tools mirror the sibling MCPs (abs-mcp, rba-mcp, ato-mcp, apra-mcp,
aihw-mcp, asic-mcp): `search_datasets`, `describe_dataset`, `get_data`,
`latest`, `list_curated`. Six curated datasets cover the 2025 WGEA public
data file: workforce composition, workforce management movements, gender
equality policy actions, parental leave + flexible work, harm prevention,
and employee support.

Backend: data.gov.au CKAN package `wgea-dataset`. Licence: CC-BY 3.0
Australia. Annual cadence (~Jan release).
"""
__all__ = ["__version__"]

try:
    from importlib.metadata import version

    __version__ = version("wgea-mcp")
except Exception:
    __version__ = "0.0.0+unknown"
