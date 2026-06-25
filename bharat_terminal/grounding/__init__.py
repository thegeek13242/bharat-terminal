"""
Grounding data layer: read-only, DB-backed queries over our NSE-sourced
financials and prices. Single source of truth shared by the in-process dossier
agent tools and the standalone MCP server, so any agent grounds on the same
numbers before reaching for web search.
"""
from bharat_terminal.grounding.queries import (
    get_financials,
    get_price_returns,
    get_shareholding,
    get_corporate_actions,
)

__all__ = [
    "get_financials",
    "get_price_returns",
    "get_shareholding",
    "get_corporate_actions",
]
