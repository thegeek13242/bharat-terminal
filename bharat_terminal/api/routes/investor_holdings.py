"""Investor-centric smart-money views over investor_holdings (migration 021).

Two endpoints, both customer-facing and purely factual (SEBI shareholding
disclosures, no recommendation language — mirrors the earnings_surprises RA
posture):
  GET /investors                — investors ranked by footprint; curated
                                   marquee names labelled + pinned.
  GET /investors/{ident}        — one investor's positions across companies with
                                   QoQ change. `ident` is `marquee:{key}` or
                                   `norm:{holder_name_norm}` (from the list rows).

Honest disclosure floor: SHP only names holders >=1% of a company, so sub-1%
positions are invisible — strongest signal in the small/midcap universe.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory
from bharat_terminal.kb.marquee import marquee_keys, resolve_investor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/investors", tags=["investors"])

DISCLAIMER = ("Based on SEBI quarterly shareholding disclosures, which name only "
              "holders above 1% of a company. Sub-1% positions are not disclosed. "
              "Data is quarterly and lagged. Not investment advice.")


@router.get("")
async def list_investors(
    marquee_only: bool = Query(False, description="Only curated marquee investors"),
    min_companies: int = Query(1, ge=1, description="Min companies held (auto-discovery floor)"),
    limit: int = Query(200, le=1000),
):
    """Investors ranked by footprint (companies held, disclosed ₹ value). Curated
    marquee investors are merged across name spellings, labelled, and pinned."""
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            text("""
                WITH latest AS (
                    SELECT symbol, MAX(quarter_end) AS q
                    FROM investor_holdings GROUP BY symbol
                ),
                cur AS (
                    SELECT ih.holder_name_norm, ih.holder_name, ih.symbol,
                           ih.holding_pct, ih.shares_held,
                           -- stake value: disclosed shares x price, else pct x mcap
                           COALESCE(
                               ih.shares_held * (SELECT close FROM price_points pp
                                                 WHERE pp.symbol = ih.symbol
                                                 ORDER BY date DESC LIMIT 1) / 1e7,
                               ih.holding_pct / 100.0 * c.mcap_cr
                           ) AS value_cr
                    FROM investor_holdings ih
                    JOIN latest l ON ih.symbol = l.symbol AND ih.quarter_end = l.q
                    JOIN companies c ON c.symbol = ih.symbol
                )
                SELECT holder_name_norm,
                       MODE() WITHIN GROUP (ORDER BY holder_name) AS rep_name,
                       COUNT(DISTINCT symbol) AS n_companies,
                       MAX(holding_pct) AS max_pct,
                       SUM(COALESCE(value_cr, 0)) AS value_cr
                FROM cur
                GROUP BY holder_name_norm
            """),
        )).mappings().all()

    # Merge marquee aliases (multiple norms → one canonical investor).
    agg: dict = {}
    for r in rows:
        m = resolve_investor(r["rep_name"])
        if m:
            ident = f"marquee:{m['key']}"
            display, itype, is_marquee = m["display"], m["type"], True
        else:
            ident = f"norm:{r['holder_name_norm']}"
            display, itype, is_marquee = r["rep_name"].title(), "other", False
        a = agg.setdefault(ident, {
            "id": ident, "display": display, "type": itype, "marquee": is_marquee,
            "n_companies": 0, "max_pct": 0.0, "value_cr": 0.0,
        })
        a["n_companies"] += r["n_companies"]
        a["max_pct"] = max(a["max_pct"], r["max_pct"] or 0.0)
        a["value_cr"] += float(r["value_cr"] or 0.0)

    investors = list(agg.values())
    if marquee_only:
        investors = [i for i in investors if i["marquee"]]
    investors = [i for i in investors if i["n_companies"] >= min_companies]
    for i in investors:
        i["max_pct"] = round(i["max_pct"], 2)
        i["value_cr"] = round(i["value_cr"], 2)
    # marquee first, then by footprint
    investors.sort(key=lambda i: (not i["marquee"], -i["n_companies"], -i["value_cr"]))
    return {"count": len(investors), "disclaimer": DISCLAIMER, "investors": investors[:limit]}


def _marquee_alias_patterns(key: str) -> Optional[list]:
    meta = marquee_keys().get(key)
    if not meta:
        return None
    return [f"%{a.strip()}%" for a in meta["aliases"]]


DEALS_DISCLAIMER = ("Exchange-disclosed bulk & block deals (a single client "
                    "crossing 0.5% of a stock). Market-making / HFT churn is "
                    "filtered out. Same-day, not investment advice.")


def _deal_filter(ident: str) -> tuple:
    """(where_sql, params) selecting an investor's market_deals rows."""
    if ident.startswith("marquee:"):
        return "marquee_key = :dk", {"dk": ident.split(":", 1)[1]}
    if ident.startswith("norm:"):
        return "client_name_norm = :dn", {"dn": ident.split(":", 1)[1]}
    return "false", {}


async def _recent_deals(s, ident: str, limit: int = 30) -> list:
    where, params = _deal_filter(ident)
    rows = (await s.execute(
        text(f"""
            SELECT deal_date, exchange, deal_type, symbol, company_name,
                   side, quantity, price, value_cr
            FROM market_deals
            WHERE {where} AND is_excluded = false
            ORDER BY deal_date DESC, value_cr DESC NULLS LAST
            LIMIT :lim
        """),
        {**params, "lim": limit},
    )).mappings().all()
    return [{
        "deal_date": r["deal_date"].isoformat(),
        "exchange": r["exchange"], "deal_type": r["deal_type"],
        "symbol": r["symbol"], "company_name": r["company_name"],
        "side": r["side"], "quantity": r["quantity"],
        "price": r["price"], "value_cr": r["value_cr"],
    } for r in rows]


@router.get("/{ident}")
async def get_investor(ident: str):
    """One investor's positions across companies with QoQ change. Positions are
    summed across a holder's named entities within each company."""
    factory = get_session_factory()
    params: dict = {}
    if ident.startswith("marquee:"):
        key = ident.split(":", 1)[1]
        patterns = _marquee_alias_patterns(key)
        if patterns is None:
            raise HTTPException(status_code=404, detail="Unknown marquee investor")
        meta = marquee_keys()[key]
        display, itype = meta["display"], meta["type"]
        where = "holder_name ILIKE ANY(:patterns)"
        params["patterns"] = patterns
    elif ident.startswith("norm:"):
        norm = ident.split(":", 1)[1]
        where = "holder_name_norm = :norm"
        params["norm"] = norm
        display, itype = norm.title(), "other"
    else:
        raise HTTPException(status_code=400, detail="ident must be 'marquee:<key>' or 'norm:<name>'")

    async with factory() as s:
        rows = (await s.execute(
            text(f"""
                WITH inv AS (
                    SELECT symbol, quarter_end,
                           SUM(holding_pct) AS pct, SUM(shares_held) AS shares
                    FROM investor_holdings
                    WHERE {where}
                    GROUP BY symbol, quarter_end
                ),
                ranked AS (
                    SELECT *, row_number() OVER (PARTITION BY symbol
                                                 ORDER BY quarter_end DESC) AS rn
                    FROM inv
                ),
                cur AS (SELECT * FROM ranked WHERE rn = 1),
                prv AS (SELECT symbol, pct AS prev_pct FROM ranked WHERE rn = 2)
                SELECT cur.symbol, c.company_name, cur.quarter_end,
                       cur.pct AS holding_pct, cur.shares AS shares_held,
                       prv.prev_pct, c.mcap_cr,
                       (SELECT close FROM price_points pp WHERE pp.symbol = cur.symbol
                        ORDER BY date DESC LIMIT 1) AS ltp
                FROM cur
                JOIN companies c ON c.symbol = cur.symbol
                LEFT JOIN prv ON prv.symbol = cur.symbol
                ORDER BY cur.pct DESC
            """),
            params,
        )).mappings().all()
        recent_deals = await _recent_deals(s, ident)

    positions = []
    total_value = 0.0
    for r in rows:
        pct, prev = r["holding_pct"], r["prev_pct"]
        delta = round(pct - prev, 2) if (pct is not None and prev is not None) else None
        if prev is None:
            change = "new"
        elif delta is None:
            change = "hold"
        else:
            change = "add" if delta > 0.01 else "trim" if delta < -0.01 else "hold"
        shares, ltp, mcap = r["shares_held"], r["ltp"], r["mcap_cr"]
        # shares is SUM(bigint) -> Decimal, ltp is Numeric -> cast both to float.
        if shares and ltp:
            value_cr = round(float(shares) * float(ltp) / 1e7, 2)
        elif pct is not None and mcap:
            value_cr = round(pct / 100.0 * float(mcap), 2)
        else:
            value_cr = None
        if value_cr:
            total_value += value_cr
        positions.append({
            "symbol": r["symbol"],
            "company_name": r["company_name"],
            "quarter_end": r["quarter_end"].isoformat(),
            "holding_pct": round(pct, 2) if pct is not None else None,
            "prev_pct": round(prev, 2) if prev is not None else None,
            "delta_pct": delta,
            "change": change,
            "shares_held": int(shares) if shares else None,
            "value_cr": value_cr,
        })
    if not positions and not recent_deals:
        raise HTTPException(status_code=404, detail="No disclosed positions for this investor")
    return {
        "id": ident, "display": display, "type": itype,
        "n_companies": len(positions),
        "total_value_cr": round(total_value, 2) if total_value else None,
        "disclaimer": DISCLAIMER,
        "positions": positions,
        "recent_deals": recent_deals,
    }


# ── Deal flow feed (separate prefix so it doesn't collide with /investors/{ident})
deals_router = APIRouter(prefix="/deals", tags=["deals"])


@deals_router.get("")
async def deal_flow(
    marquee_only: bool = Query(False, description="Only curated marquee investors"),
    include_excluded: bool = Query(False, description="Also show filtered MM/churn flows"),
    days: int = Query(30, le=180),
    limit: int = Query(100, le=500),
):
    """Recent informed bulk/block flows, market-maker & churn filtered by default."""
    where = ["deal_date >= (now()::date - (:days)::int)"]
    params: dict = {"days": days, "lim": limit}
    if not include_excluded:
        where.append("is_excluded = false")
    if marquee_only:
        where.append("marquee_key IS NOT NULL")
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            text(f"""
                SELECT deal_date, exchange, deal_type, symbol, company_name,
                       client_name, client_name_norm, marquee_key,
                       side, quantity, price, value_cr, is_excluded, exclude_reason
                FROM market_deals
                WHERE {" AND ".join(where)}
                ORDER BY deal_date DESC, value_cr DESC NULLS LAST
                LIMIT :lim
            """),
            params,
        )).mappings().all()

    deals = []
    for r in rows:
        m = resolve_investor(r["client_name"]) if r["marquee_key"] else None
        deals.append({
            "deal_date": r["deal_date"].isoformat(),
            "exchange": r["exchange"], "deal_type": r["deal_type"],
            "symbol": r["symbol"], "company_name": r["company_name"],
            "client_name": r["client_name"],
            "investor_id": (f"marquee:{r['marquee_key']}" if r["marquee_key"]
                           else f"norm:{r['client_name_norm']}"),
            "marquee": m["display"] if m else None,
            "side": r["side"], "quantity": r["quantity"],
            "price": r["price"], "value_cr": r["value_cr"],
            "is_excluded": r["is_excluded"], "exclude_reason": r["exclude_reason"],
        })
    return {"count": len(deals), "disclaimer": DEALS_DISCLAIMER, "deals": deals}
