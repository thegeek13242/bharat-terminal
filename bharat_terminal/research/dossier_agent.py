"""
Dossier agent — produces a full CFA-level structured dossier per company.

This is the heavy preprocessing step that lets the live news pipeline answer
"who is exposed to X" in sub-100ms. The agent:

  1. Reads pre-parsed financials from our KB (financial_results table,
     populated by the existing XBRL pipeline).
  2. Pulls recent BSE filings (annual report, last 4 quarters of results,
     investor presentations) via the existing BSE XBRL tools.
  3. Web-searches for management commentary, peer comparisons, segment
     details, and risk disclosures that don't fit into XBRL.
  4. Submits ONE call to `submit_dossier` with a structured payload
     covering business + financials + sensitivities + linkages + risks +
     catalysts + mgmt_commentary + citations.

The submitted payload is persisted atomically:
  • One row in `company_dossiers` (the rich JSONB source of truth)
  • N rows in `dossier_sensitivities` (one per trigger × channel)
  • M rows in `dossier_linkages` (one per outbound company edge)

Both flat tables are indexed for sub-100ms trigger queries from Stage 4.

Cost: ~$0.30-0.50 per company with Sonnet 4.6 on the initial pass.
Quarterly incremental refresh is lighter (~$0.10) — see `refresh_kind`
arg below.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from bharat_terminal import grounding, llm
from bharat_terminal.llm import provider_for_model

from bharat_terminal.research import db

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DOSSIER_MODEL = os.getenv("DOSSIER_MODEL", "gemini-3.1-pro-preview")
DOSSIER_MAX_TURNS = int(os.getenv("DOSSIER_MAX_TURNS", "20"))
# Gemini sometimes returns a text-only "thinking" turn with no tool call. Rather
# than treat that as terminal, nudge the agent to continue — up to this many
# times before giving up (bounds wasted cost).
DOSSIER_MAX_NUDGES = int(os.getenv("DOSSIER_MAX_NUDGES", "3"))
ENABLE_WEB_SEARCH = os.getenv("RESEARCH_ENABLE_WEB_SEARCH", "1") == "1"


# ── Channel enum (must match migration 007 CHECK constraint) ────────────
ALLOWED_CHANNELS = (
    "OUTPUT_PRICE",
    "INPUT_COST",
    "FX_TRANSLATION",
    "INTEREST_BURDEN",
    "CUSTOMER_DEMAND",
    "FII_FLOW",
    "OTHER",
)

ALLOWED_LINKAGE_KINDS = (
    "competitor",
    "customer",
    "supplier",
    "subsidiary",
    "jv",
    "sector_peer",
)


# ── Tool definitions ────────────────────────────────────────────────────
# We reuse the input-side tools (KB context, BSE filings, BSE XBRL text)
# from the existing agent.py to avoid duplication.

TOOL_GET_KB_CONTEXT = {
    "name": "get_kb_company_context",
    "description": (
        "Get the company's existing KB record — pre-parsed financial_results "
        "for the last ~8 quarters (revenue, EBITDA, PAT, margins, segments, "
        "key XBRL facts). This is your starting point. ALWAYS call this first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    },
}

TOOL_LIST_TRIGGERS = {
    "name": "list_triggers",
    "description": (
        "Return the catalog of triggers (commodity / FX / rate / index "
        "variables) we calibrate sensitivities against. You MUST include "
        "a sensitivity record for every trigger the company has plausible "
        "exposure to (even if beta is small)."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

TOOL_GET_FINANCIALS = {
    "name": "get_financials",
    "description": (
        "Authoritative quarterly financials straight from our NSE-sourced "
        "financial_results table (current through the latest reported quarter). "
        "Returns the full cost structure per quarter — revenue, cost of "
        "materials, employee/finance/depreciation costs, total expenses, "
        "EBITDA + margin, PAT, EPS, debt/equity, interest coverage, segment "
        "data. Each period also carries a `taxonomy` (indas / nbfc / banking / "
        "life_insurance / general_insurance / nonindas): for banks & insurers the "
        "generic cost columns do not apply — `revenue_cr` is interest earned / "
        "premium, and the sector's real lines (interest expended, net interest "
        "income, provisions; premium / claims_incurred / net_commission) are in "
        "`sector_financials`. Read taxonomy first, then interpret accordingly. "
        "This is parsed from NSE filings into our own database and is "
        "AUTHORITATIVE — use it for every reported financial figure and cite it "
        "via the returned `provenance` (source NSE_DB, period_end, nse_filing_id). "
        "Do NOT call web_search to re-source these numbers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "periods": {"type": "integer", "minimum": 1, "maximum": 24, "default": 8,
                         "description": "Number of most-recent quarters, newest first."},
            "consolidated": {"type": "boolean", "default": True,
                              "description": "Prefer consolidated statements (falls back if unavailable)."},
        },
        "required": ["symbol"],
    },
}

TOOL_GET_PRICE_RETURNS = {
    "name": "get_price_returns",
    "description": (
        "Trailing price returns, annualized volatility and 1y max drawdown for "
        "the symbol, computed from our NSE daily price_points (current to the "
        "last trading day). Use to ground market-risk / momentum context. No "
        "index beta is available (we hold no index series)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "windows": {"type": "array", "items": {"type": "integer"},
                         "description": "Calendar-day lookback windows, e.g. [30,90,180,365]."},
        },
        "required": ["symbol"],
    },
}

TOOL_GET_SHAREHOLDING = {
    "name": "get_shareholding",
    "description": (
        "Quarterly shareholding pattern from our database (parsed from the "
        "authoritative NSE shareholding XBRL): promoter %, FII %, DII %, public %, "
        "promoter pledge %, total shareholder count — newest first. AUTHORITATIVE; "
        "use this for ANY holding figure and cite source NSE_DB — do NOT web_search "
        "for promoter/FII/DII holdings. A null promoter_pct means the company has "
        "no promoter (e.g. HDFCBANK, ITC), not missing data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "quarters": {"type": "integer", "minimum": 1, "maximum": 12, "default": 4},
        },
        "required": ["symbol"],
    },
}

TOOL_GET_CORPORATE_ACTIONS = {
    "name": "get_corporate_actions",
    "description": (
        "Recent corporate actions from our database (NSE-sourced): dividends "
        "(with ₹/share value), bonuses, splits, buybacks, rights, restructurings — "
        "newest ex_date first. AUTHORITATIVE; use for dividend/buyback/bonus "
        "history and cite source NSE_DB — do NOT web_search for these. Optional "
        "action_type filter (DIVIDEND/BUYBACK/BONUS/SPLIT/RIGHTS/RESTRUCTURE)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "action_type": {"type": "string"},
        },
        "required": ["symbol"],
    },
}

TOOL_WEB_SEARCH = {
    "name": "web_search",
    "description": (
        "Web-search-grounded lookup. Use ONLY to fill gaps our own data "
        "cannot — peer market shares, customer/supplier concentration, broker "
        "targets, qualitative news. Returns an answer plus source URLs to cite. "
        "Always try get_kb_company_context / get_financials / get_price_returns "
        "FIRST; web_search is the fallback, not the default."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


# ── The submission schema — the heart of the agent's output ────────────

TOOL_SUBMIT_DOSSIER = {
    "name": "submit_dossier",
    "description": (
        "FINAL OUTPUT — call exactly once at the end of your research with "
        "the complete dossier. After this call, STOP. Every numeric claim "
        "must have a corresponding citation entry with a real URL."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "source_quarter": {
                "type": "string",
                "description": "Latest quarter end incorporated, ISO 'YYYY-MM-DD'",
            },
            "data_quality": {
                "type": "number",
                "minimum": 0.0, "maximum": 1.0,
                "description": "Your self-assessed confidence in the dossier (0-1). Below 0.6 means significant gaps.",
            },

            # ── Business profile ────────────────────────────────────────
            "business": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "2-3 sentences on what the company does"},
                    "sector": {"type": "string"},
                    "sub_sector": {"type": "string"},
                    "segments": {
                        "type": "array",
                        "description": "Revenue segments with % share and key inputs/outputs",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "revenue_pct": {"type": "number", "minimum": 0, "maximum": 100},
                                "ebitda_pct": {"type": "number", "minimum": -50, "maximum": 100},
                                "key_inputs": {"type": "array", "items": {"type": "string"}},
                                "key_outputs": {"type": "array", "items": {"type": "string"}},
                                "geography": {"type": "string", "description": "domestic | export | both"},
                            },
                            "required": ["name", "revenue_pct"],
                        },
                    },
                    "geography": {
                        "type": "object",
                        "properties": {
                            "domestic_pct": {"type": "number", "minimum": 0, "maximum": 100},
                            "export_pct": {"type": "number", "minimum": 0, "maximum": 100},
                            "export_regions": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "customer_concentration": {
                        "type": "string",
                        "description": "B2C / B2B-diversified / B2B-concentrated; top-10 if disclosed",
                    },
                    "supplier_concentration": {"type": "string"},
                    "moat": {"type": "string", "description": "Brand / scale / network / regulatory / tech"},
                },
                "required": ["summary", "sector", "segments"],
            },

            # ── Financial snapshot ──────────────────────────────────────
            "financials": {
                "type": "object",
                "properties": {
                    "revenue_5y_cr":         {"type": "array", "items": {"type": "number"}, "description": "5-year revenue trajectory ₹Cr, oldest first"},
                    "ebitda_margin_5y_pct":  {"type": "array", "items": {"type": "number"}},
                    "pat_5y_cr":             {"type": "array", "items": {"type": "number"}},
                    "net_debt_cr":           {"type": "number"},
                    "nd_ebitda_x":           {"type": "number"},
                    "usd_debt_pct":          {"type": "number", "description": "% of gross debt that is USD-denominated"},
                    "floating_debt_pct":     {"type": "number"},
                    "working_capital_days":  {"type": "number"},
                    "capex_intensity_pct":   {"type": "number", "description": "Capex / Revenue"},
                    "fcf_5y_cr":             {"type": "array", "items": {"type": "number"}},
                    "dividend_payout_pct":   {"type": "number"},
                    "promoter_holding_pct":  {"type": "number"},
                    "fii_holding_pct":       {"type": "number"},
                },
            },

            # ── Sensitivities (THE KEY for impact graph) ────────────────
            "sensitivities": {
                "type": "array",
                "description": (
                    "One entry per (trigger, channel) the company is meaningfully "
                    "exposed to. Skip triggers with negligible exposure (|beta|<0.05). "
                    "Multiple channels per trigger are allowed — e.g. RELIANCE has both "
                    "OUTPUT_PRICE (refining margin) and INPUT_COST (petchem feedstock) "
                    "exposure to BRENT."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "trigger_id": {
                            "type": "string",
                            "description": "Must match an id from list_triggers (e.g. BRENT, USDINR, STEEL_HRC)",
                        },
                        "channel": {
                            "type": "string",
                            "enum": list(ALLOWED_CHANNELS),
                            "description": (
                                "OUTPUT_PRICE: revenue indexed to trigger (e.g. oil for ONGC). "
                                "INPUT_COST: raw-material cost rises with trigger (e.g. crude for INDIGO). "
                                "FX_TRANSLATION: USD revenue/cost retranslation. "
                                "INTEREST_BURDEN: finance cost reprices with rate trigger. "
                                "CUSTOMER_DEMAND: end-customer purchasing behaviour shifts. "
                                "FII_FLOW: stock impacted by passive/active foreign flow direction. "
                                "OTHER: rare cases (regulatory, ESG, etc)."
                            ),
                        },
                        "beta": {
                            "type": "number",
                            "description": (
                                "Signed return-elasticity: a +1% move in trigger → beta % "
                                "change in fundamentals (typically EBITDA or PAT). Realistic "
                                "range [-1.0, +1.0]. Pure-play producers can reach ~0.9."
                            ),
                        },
                        "lag_days": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Estimated propagation lag in days. 0 = same-session "
                                "(OUTPUT_PRICE on traded commodities). 30 = next month-end "
                                "(FX translation). 60 = next quarter result (INPUT_COST in "
                                "COGS). 90+ = customer-demand response."
                            ),
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "med", "low"],
                            "description": (
                                "high = filing-disclosed segment %s and concall guidance support the beta. "
                                "med = qualitative + peer comparison. low = inferred from sector, weak evidence."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "description": "1-3 sentences citing the % of revenue/cost driving this beta",
                        },
                        "evidence_url": {"type": "string"},
                    },
                    "required": ["trigger_id", "channel", "beta", "confidence", "rationale"],
                },
            },

            # ── Linkages (for 2nd/3rd-degree propagation) ───────────────
            "linkages": {
                "type": "array",
                "description": (
                    "Outbound edges from this company to other listed companies. "
                    "Stage 4 walks these when news hits the source company to find "
                    "2nd/3rd-degree exposed names. Include top 5-10 per kind."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "target_symbol": {"type": "string", "description": "NSE-style symbol"},
                        "kind": {
                            "type": "string",
                            "enum": list(ALLOWED_LINKAGE_KINDS),
                            "description": (
                                "competitor: direct sector rival. "
                                "sector_peer: same sector but not direct competitor. "
                                "customer: target is the source's customer. "
                                "supplier: target is the source's supplier. "
                                "subsidiary: target is consolidated. "
                                "jv: target is a joint venture."
                            ),
                        },
                        "strength": {
                            "type": "number",
                            "minimum": 0.0, "maximum": 1.0,
                            "description": (
                                "Edge weight for beta propagation. 1.0 = exact mirror "
                                "(rare, e.g. subsidiary 100% consol). 0.5 = strong direct "
                                "competitor. 0.2 = sector peer. Lower for asymmetric pairs "
                                "(small cap → large cap = small upward influence)."
                            ),
                        },
                        "direction": {"type": "string", "enum": ["positive", "negative", "mixed"]},
                        "propagation_lag_days": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Extra delay on top of source's channel lag; competitor pressure ~1d, customer pull-through ~14d.",
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["target_symbol", "kind", "strength", "direction", "rationale"],
                },
            },

            # ── Forward-looking ─────────────────────────────────────────
            "risks": {
                "type": "array",
                "description": "Top 3-5 risks with severity 1-5",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "description": {"type": "string"},
                    },
                    "required": ["label", "severity"],
                },
            },

            "catalysts": {
                "type": "array",
                "description": "Known forward catalysts (capex, IPO, regulatory pending, expansion)",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "expected_timing": {"type": "string"},
                        "impact_direction": {"type": "string", "enum": ["positive", "negative", "uncertain"]},
                        "description": {"type": "string"},
                    },
                    "required": ["label", "impact_direction"],
                },
            },

            "mgmt_commentary": {
                "type": "object",
                "properties": {
                    "latest_quarter": {"type": "string"},
                    "guidance": {"type": "string", "description": "Quoted/paraphrased latest concall guidance"},
                    "tone": {"type": "string", "enum": ["positive", "neutral", "cautious"]},
                },
            },

            # ── Citations — every numeric must trace back to a URL ──────
            "citations": {
                "type": "array",
                "description": "Every numeric / quoted claim must reference one of these.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "source": {"type": "string", "enum": ["BSE_XBRL", "BSE_PDF", "KB", "WEB"]},
                        "period": {"type": "string"},
                        "quote": {"type": "string", "description": "Verbatim ≤200 chars"},
                    },
                    "required": ["url", "source"],
                },
            },
        },
        "required": [
            "symbol", "data_quality", "business", "sensitivities",
            "linkages", "citations",
        ],
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_tools() -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = [
        TOOL_GET_KB_CONTEXT,
        TOOL_GET_FINANCIALS,
        TOOL_GET_PRICE_RETURNS,
        TOOL_GET_SHAREHOLDING,
        TOOL_GET_CORPORATE_ACTIONS,
        TOOL_LIST_TRIGGERS,
        TOOL_SUBMIT_DOSSIER,
    ]
    if ENABLE_WEB_SEARCH:
        # Provider-agnostic web search: a normal function tool whose handler runs
        # a Google-Search-grounded Gemini call (see _execute_tool). Works on both
        # Gemini and Claude, unlike the old Anthropic server-side web_search tool.
        tools.append(TOOL_WEB_SEARCH)
    return tools


def _build_system_prompt(refresh_kind: str) -> str:
    """Build the agent's system prompt. `refresh_kind` adjusts depth."""
    base = (
        "You are a senior sell-side equity research analyst producing a "
        "rigorous, CFA-level dossier on an Indian listed company. The dossier "
        "is consumed by an automated trading-signal pipeline that needs to "
        "answer 'who is exposed to event X' in sub-100ms when news breaks.\n\n"

        "WORKFLOW (ground in OUR NSE data before reaching for the web):\n"
        "1. Call get_kb_company_context FIRST for the company profile.\n"
        "2. Call get_financials for the authoritative quarterly cost structure "
        "(revenue, material/employee/finance/depreciation costs, EBITDA margin, "
        "PAT, EPS, debt/equity) straight from NSE filings — use these numbers "
        "for the financial snapshot and to reason about input-cost vs "
        "output-price sensitivities.\n"
        "3. Call get_price_returns for trailing returns, volatility and drawdown.\n"
        "4. Call get_shareholding for promoter / FII / DII / public % + pledge "
        "(authoritative, from NSE) — use these for the holding figures.\n"
        "5. Call get_corporate_actions for dividend / buyback / bonus / split "
        "history (authoritative, from NSE).\n"
        "6. Call list_triggers to see the catalog of macro/commodity variables.\n"
        "7. Use web_search ONLY to fill remaining gaps our data cannot cover: "
        "segment detail, mgmt commentary, risk factors, peer market shares, "
        "customer/supplier concentration, broker targets, forward guidance. "
        "NEVER use web_search to re-source a REPORTED financial figure that "
        "get_financials already returns — that data is in our database. Our DB "
        "already has financials, prices, SHAREHOLDING (promoter/FII/DII), and "
        "CORPORATE ACTIONS (dividends/buybacks) — do NOT web_search for any of "
        "those. Cite web source URLs it returns.\n"
        "8. End with EXACTLY ONE call to submit_dossier containing the full "
        "structured payload.\n\n"

        "QUALITY RULES (non-negotiable):\n"
        "• EVERY numeric claim must have a `citations` entry. For figures from "
        "  OUR DATABASE (get_financials, get_price_returns, get_kb_company_context) "
        "  set the citation source to \"NSE_DB\" with the period_end + nse_filing_id "
        "  from the tool's `provenance` — do NOT attach an external URL and do NOT "
        "  re-fetch it from BSE/web; our DB is authoritative. Only figures genuinely "
        "  sourced from the web need a real external URL.\n"
        "• DO NOT INVENT numbers. If a figure isn't disclosed, omit it and let "
        "  the field be null. Setting `data_quality` below 0.7 is acceptable.\n"
        "• Sensitivities: one row per (trigger, channel). Multiple channels per "
        "  trigger are common (RELIANCE: BRENT via OUTPUT_PRICE for refining + "
        "  via INPUT_COST for petchem). Skip triggers with |β|<0.05.\n"
        "• Linkages: include 5-10 outbound edges. Be specific — say 'BAJAJFINSV' "
        "  not 'NBFCs'. Strength should reflect symmetry — a small-cap targeting "
        "  a NIFTY 50 name should have low strength (1st cap can't move RIL).\n"
        "• Beta range: realistic [-1.0, +1.0]. Reject reasoning that yields "
        "  |β|>1.5 without overwhelming evidence (e.g. pure-play producer).\n"
        "• Lag estimates matter: OUTPUT_PRICE on traded commodities = 0 days. "
        "  INPUT_COST in COGS = ~60 days (next quarter result). FX_TRANSLATION = "
        "  ~30 days. INTEREST_BURDEN = ~90 days. CUSTOMER_DEMAND = ~180 days.\n\n"

        "BIAS WARNINGS:\n"
        "• Don't overrate domestic-FMCG to BRENT — the link is weak even for "
        "  Asian Paints (TiO2 ~12% of COGS, lag 60d).\n"
        "• Banks' exposure to USDINR is mostly via FII flows, not direct "
        "  translation. Channel = FII_FLOW for those.\n"
        "• IT services: USDINR via FX_TRANSLATION channel (revenue-linked), "
        "  NOT via INPUT_COST. SP500/NASDAQ_IT via CUSTOMER_DEMAND channel.\n"
    )
    if refresh_kind == "incremental_quarter":
        base += (
            "\n\nINCREMENTAL MODE: The previous dossier exists. Focus only on "
            "what changed in the latest quarter — segment mix shifts >5%, "
            "guidance updates, new risk disclosures, capex revisions. Preserve "
            "old citations where the underlying data hasn't changed."
        )
    return base


def _build_user_prompt(symbol: str, company_name: Optional[str]) -> str:
    name_part = f" ({company_name})" if company_name else ""
    return (
        f"Produce a full dossier for {symbol}{name_part}.\n\n"
        f"This dossier will be queried whenever news potentially affects this "
        f"company OR a company it's linked to. Quality matters more than speed. "
        f"Take as many tool calls as you need. End with submit_dossier."
    )


# ── Tool execution ──────────────────────────────────────────────────────

async def _execute_tool(name: str, args: Dict[str, Any]) -> Any:
    if name == "get_kb_company_context":
        ctx = await db.get_company_context(args["symbol"])
        return ctx or {"error": f"No KB record for {args['symbol']}"}

    if name == "get_financials":
        return await grounding.get_financials(
            args["symbol"],
            periods=int(args.get("periods", 8)),
            consolidated=args.get("consolidated", True),
        )

    if name == "get_price_returns":
        windows = args.get("windows") or [30, 90, 180, 365]
        return await grounding.get_price_returns(
            args["symbol"], windows=[int(w) for w in windows]
        )

    if name == "get_shareholding":
        return await grounding.get_shareholding(
            args["symbol"], quarters=int(args.get("quarters", 4))
        )

    if name == "get_corporate_actions":
        return await grounding.get_corporate_actions(
            args["symbol"],
            limit=int(args.get("limit", 20)),
            action_type=args.get("action_type"),
        )

    if name == "web_search":
        return await llm.grounded_search(args["query"])

    if name == "list_triggers":
        # Pull from DB so the agent sees exactly what's in the triggers table.
        rows = await db.list_triggers()
        return {"triggers": rows}

    return {"error": f"Unknown tool: {name}"}


# ── Persistence ─────────────────────────────────────────────────────────

async def _save_dossier(
    payload: Dict[str, Any],
    *,
    tokens_used: int,
    refresh_kind: str,
) -> None:
    """Persist a submitted dossier to all three tables in one transaction."""
    await db.upsert_company_dossier(
        symbol=payload["symbol"],
        source_quarter=payload.get("source_quarter"),
        refresh_kind=refresh_kind,
        model_used=DOSSIER_MODEL,
        tokens_used=tokens_used,
        data_quality=payload.get("data_quality"),
        business=payload.get("business"),
        financials=payload.get("financials"),
        sensitivities=payload.get("sensitivities"),  # full JSONB blob
        linkages=payload.get("linkages"),
        risks=payload.get("risks"),
        catalysts=payload.get("catalysts"),
        mgmt_commentary=payload.get("mgmt_commentary"),
        citations=payload.get("citations"),
    )

    # Flatten sensitivities — one row per (trigger, channel)
    sens_rows = []
    for s in payload.get("sensitivities") or []:
        if s.get("channel") not in ALLOWED_CHANNELS:
            logger.warning(
                f"[DOSSIER] {payload['symbol']} rejected sensitivity with "
                f"invalid channel={s.get('channel')!r}"
            )
            continue
        sens_rows.append({
            "trigger_id": s["trigger_id"],
            "beta": float(s["beta"]),
            "channel": s["channel"],
            "lag_days": int(s.get("lag_days", 0)),
            "confidence": s.get("confidence", "med"),
            "rationale": s.get("rationale"),
            "evidence_url": s.get("evidence_url"),
        })
    await db.replace_dossier_sensitivities(payload["symbol"], sens_rows)

    # Flatten linkages
    link_rows = []
    for link in payload.get("linkages") or []:
        if link.get("kind") not in ALLOWED_LINKAGE_KINDS:
            logger.warning(
                f"[DOSSIER] {payload['symbol']} rejected linkage with "
                f"invalid kind={link.get('kind')!r}"
            )
            continue
        link_rows.append({
            "target_symbol": link["target_symbol"].upper().strip(),
            "kind": link["kind"],
            "strength": float(link["strength"]),
            "direction": link.get("direction", "positive"),
            "propagation_lag_days": int(link.get("propagation_lag_days", 0)),
            "rationale": link.get("rationale"),
            "evidence_url": link.get("evidence_url"),
        })
    await db.replace_dossier_linkages(payload["symbol"], link_rows)

    logger.info(
        f"[DOSSIER] {payload['symbol']} saved: "
        f"data_quality={payload.get('data_quality')}, "
        f"sensitivities={len(sens_rows)}, linkages={len(link_rows)}, "
        f"tokens={tokens_used}"
    )


# ── Main agent loop ─────────────────────────────────────────────────────

async def research_company(
    symbol: str,
    *,
    refresh_kind: str = "initial",
    client: Optional[Any] = None,  # deprecated/unused: provider now chosen by the llm wrapper
) -> Optional[Dict[str, Any]]:
    """
    Run the dossier agent for one company. Returns the submitted payload
    on success, None on failure. Persists to all 3 tables atomically.
    """
    if provider_for_model(DOSSIER_MODEL) == "anthropic" and not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set — cannot run dossier agent")
        return None

    # The LLM wrapper selects the provider from DOSSIER_MODEL and handles auth
    # (Vertex ADC for claude-*/gemini-*; console key for the legacy path). The
    # multi-turn tool loop below is provider-agnostic: it re-encodes each model
    # turn via llm.assistant_content() and feeds Anthropic-style tool_result
    # blocks, which the wrapper translates to Gemini function_response parts.
    tools = _build_tools()
    system = _build_system_prompt(refresh_kind)

    company_name = await db.get_company_name(symbol)
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _build_user_prompt(symbol, company_name)}
    ]

    submitted: Optional[Dict[str, Any]] = None
    tokens_in = tokens_out = tokens_think = tokens_cached = 0
    web_search_calls = 0
    turns_used = 0
    nudges = 0
    _t0 = time.monotonic()

    def _emit_cost(success: bool, error: Optional[str] = None) -> None:
        """Emit ONE cost record per dossier run — success or failure — so token
        spend lands in the LLM cost log and the rolling-hour guardrail. Captures
        what tokens_used historically missed: Gemini thinking tokens (billed as
        output), cache reads, per-call web_search grounding, and runs that burned
        tokens but never wrote a dossier row (model error / no submit)."""
        try:
            from bharat_terminal.analysis.llm_logger import (
                get_llm_logger, LLMCallRecord, compute_cost,
            )
            billed_out = tokens_out + tokens_think
            get_llm_logger().log(LLMCallRecord(
                call_id=f"dossier_{symbol}_{time.time_ns()}",
                model=DOSSIER_MODEL,
                stage="dossier",
                prompt_tokens=tokens_in,
                completion_tokens=billed_out,
                latency_ms=(time.monotonic() - _t0) * 1000,
                cost_usd=compute_cost(DOSSIER_MODEL, tokens_in, billed_out),
                success=success,
                error=error,
                thinking_tokens=tokens_think,
                cached_tokens=tokens_cached,
                web_search_calls=web_search_calls,
                turns=turns_used,
                context_id=symbol,
            ))
        except Exception as e:  # cost logging must never break generation
            logger.warning(f"[DOSSIER] {symbol} cost-log emit failed: {e}")

    for turn in range(DOSSIER_MAX_TURNS):
        try:
            resp = await llm.acomplete(
                model=DOSSIER_MODEL,
                max_tokens=8192,
                system=system,
                tools=tools,
                messages=messages,
            )
        except Exception as e:
            logger.error(f"[DOSSIER] {symbol} model error turn={turn}: {e}")
            _emit_cost(success=False, error=f"model_error: {e}")
            return None

        turns_used = turn + 1
        tokens_in += resp.usage.input_tokens or 0
        tokens_out += resp.usage.output_tokens or 0
        tokens_think += resp.usage.thinking_tokens or 0
        tokens_cached += resp.usage.cached_tokens or 0

        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        # Check for submission first — agent might submit + add closing prose
        for tu in tool_uses:
            if tu.name == "submit_dossier":
                submitted = dict(tu.input)
                submitted["symbol"] = submitted.get("symbol") or symbol
                break
        if submitted:
            break

        if not tool_uses:
            # Text-only turn (no tool call). This is usually Gemini "thinking out
            # loud", not a real finish — nudge it to continue rather than ending
            # the run before submit_dossier. Bounded by DOSSIER_MAX_NUDGES.
            if nudges >= DOSSIER_MAX_NUDGES:
                logger.warning(
                    f"[DOSSIER] {symbol} no tool call after {nudges} nudges "
                    f"(turn={turn}) — giving up"
                )
                break
            nudges += 1
            logger.info(
                f"[DOSSIER] {symbol} text-only turn={turn}; nudging "
                f"({nudges}/{DOSSIER_MAX_NUDGES})"
            )
            # Replay the model's turn (preserves any Gemini thought_signature);
            # fall back to a placeholder so the assistant turn is never empty.
            asst = llm.assistant_content(resp) or [{"type": "text", "text": "(continuing)"}]
            messages.append({"role": "assistant", "content": asst})
            messages.append({"role": "user", "content": (
                "Continue your research using the tools. When you have gathered "
                "enough, you MUST finish by calling the submit_dossier tool exactly "
                "once with the full structured dossier. Do not reply with prose — "
                "make a tool call now."
            )})
            continue

        # Execute tools and feed back
        tool_results = []
        for tu in tool_uses:
            if tu.name == "web_search":
                web_search_calls += 1  # billed per request, not per token
            try:
                result = await _execute_tool(tu.name, dict(tu.input))
            except Exception as e:
                logger.warning(f"[DOSSIER] {symbol} tool {tu.name} error: {e}")
                result = {"error": str(e)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(result)[:50000],  # cap per-result size
            })

        # Re-submit the assistant turn via normalized blocks (provider-agnostic;
        # preserves tool_use ids so the tool_result blocks below line up).
        messages.append({"role": "assistant", "content": llm.assistant_content(resp)})
        messages.append({"role": "user", "content": tool_results})

    if not submitted:
        logger.error(f"[DOSSIER] {symbol} agent did not submit after {DOSSIER_MAX_TURNS} turns")
        _emit_cost(success=False, error="no_submit")
        return None

    try:
        await _save_dossier(
            submitted,
            # True billed total: input + output + thinking (Gemini bills thoughts
            # as output). Previously tokens_in + tokens_out silently dropped them.
            tokens_used=tokens_in + tokens_out + tokens_think,
            refresh_kind=refresh_kind,
        )
    except Exception as e:
        logger.error(f"[DOSSIER] {symbol} persistence failed: {e}", exc_info=True)
        _emit_cost(success=False, error=f"persist_error: {e}")
        return None

    _emit_cost(success=True)
    return submitted
