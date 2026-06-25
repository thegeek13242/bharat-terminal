"""
Bharat Terminal grounding MCP server.

Exposes our NSE-sourced financials and prices as MCP tools over streamable HTTP,
so any MCP-capable agent (the dossier agent uses the same functions in-process)
can ground on our own data before reaching for web search. Read-only.

Run:  python -m bharat_terminal.mcp_server.server
Tools backed by `bharat_terminal.grounding` — the single source of truth shared
with the in-process dossier-agent tools.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from bharat_terminal import grounding

HOST = os.getenv("MCP_SERVER_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_SERVER_PORT", "8002"))

mcp = FastMCP("bharat-nse-grounding", host=HOST, port=PORT)


@mcp.tool()
async def get_financials(
    symbol: str, periods: int = 8, consolidated: bool = True
) -> Dict[str, Any]:
    """Most-recent quarterly financials for an NSE symbol, newest first.

    Authoritative figures from our `financial_results` table (current through the
    latest reported quarter): full cost structure — revenue, cost of materials,
    employee/finance/depreciation costs, total expenses, EBITDA + margin, PAT,
    EPS, debt/equity, interest coverage, plus segment_data. Prefer this over web
    search for any historical financial figure.

    Each period carries a `taxonomy` (indas / nbfc / banking / life_insurance /
    general_insurance / nonindas). For banking & insurance the generic cost
    columns are not applicable — `revenue_cr` is interest earned (banks) or
    premium (insurers), and the sector's real operating lines (interest expended,
    net interest income, provisions; premium / claims_incurred / net_commission)
    are in the `sector_financials` object. Read `taxonomy` first, then interpret
    the figures accordingly.
    """
    return await grounding.get_financials(
        symbol, periods=periods, consolidated=consolidated
    )


@mcp.tool()
async def get_price_returns(
    symbol: str, windows: Optional[List[int]] = None
) -> Dict[str, Any]:
    """Trailing returns, annualized volatility and 1y max drawdown for a symbol.

    Computed from our NSE daily `price_points` (current to the last trading day).
    `windows` are calendar-day lookbacks (default [30, 90, 180, 365]). No index
    beta is available — we hold no index series.
    """
    return await grounding.get_price_returns(
        symbol, windows=windows or [30, 90, 180, 365]
    )


@mcp.tool()
async def get_shareholding(symbol: str, quarters: int = 4) -> Dict[str, Any]:
    """Quarterly shareholding pattern for an NSE symbol, newest first.

    From our `shareholding_patterns` table (parsed from the authoritative NSE
    shareholding XBRL): promoter / FII / DII / public %, promoter pledge %, total
    shareholder count. A NULL promoter_pct means the company has no promoter
    (e.g. HDFCBANK, ITC), not missing data.
    """
    return await grounding.get_shareholding(symbol, quarters=quarters)


@mcp.tool()
async def get_corporate_actions(
    symbol: str, limit: int = 20, action_type: Optional[str] = None
) -> Dict[str, Any]:
    """Recent corporate actions for an NSE symbol, newest ex_date first.

    From our `corporate_actions` table (NSE-sourced): dividends (with ₹/share
    value), bonuses, splits, buybacks, rights, restructurings. Optional
    action_type filter (DIVIDEND/BUYBACK/BONUS/SPLIT/RIGHTS/RESTRUCTURE).
    """
    return await grounding.get_corporate_actions(symbol, limit=limit, action_type=action_type)


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
