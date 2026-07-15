"""
NSE ownership-edge sync (company_relationships Layer 2).

Derives AUTHORITATIVE ownership edges from the SEBI shareholding XBRL: named
shareholders that are themselves listed companies in our `companies` table become
high-confidence `company_relationships` rows (source_of_truth='nse_shp'). This
complements the LLM-inferred dossier_linkages layer with filing-sourced facts.

Conservative by design (data-accuracy rule): only EXACT normalized name matches to
a listed company, only named holders with a parseable holding %, never fuzzy.
Most Nifty promoters are individuals/unlisted holdcos, so yield is intentionally
small but every edge is a fact.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.models import Company, CompanyRelationship
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import create_async_session
from bharat_terminal.kb.sync.nse_financial_results import create_nse_client, refresh_nse_cookies
from bharat_terminal.kb.sync.nse_shareholding_sync import (
    SHP_MASTER_API, SHP_REFERER, _local_facts,
)

logger = logging.getLogger(__name__)

# Strip ONLY legal-form suffixes + punctuation. Deliberately do NOT strip words
# like "holdings"/"investments"/"company" — "Bajaj Auto Holdings Ltd" must NOT
# collapse onto "Bajaj Auto Ltd" (a different, listed entity).
_SUFFIXES = re.compile(r"\b(limited|ltd|private|pvt)\b", re.I)


def _norm(name: str) -> str:
    """Normalize a company name for EXACT matching (conservative)."""
    s = (name or "").lower()
    s = re.sub(r"[.,&'\-/()]", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_named_holders(xml: str) -> List[Tuple[str, Optional[float]]]:
    """Return (name, holding_pct or None) for each named shareholder.

    The per-shareholder percentage is not reliably co-located with the name fact
    in this taxonomy, so holding is often None — we record the *relationship*
    (a fact) and leave weight NULL rather than fabricate a number.
    """
    pct = _local_facts(xml, "ShareholdingAsAPercentageOfTotalNumberOfShares")
    total = pct.get("ShareholdingPattern_ContextI")
    try:
        scale = 100.0 if (total is not None and float(total) <= 1.5) else 1.0
    except ValueError:
        scale = 1.0
    out: List[Tuple[str, Optional[float]]] = []
    for cref, name in re.findall(
            r'<[\w-]+:NameOfTheShareholder contextRef="([^"]+)"[^>]*>([^<]+)</[\w-]+:NameOfTheShareholder>',
            xml):
        # The % for a named holder lives at the same context id minus the 'D_'
        # detail-table prefix (name @ D_X_Context5, pct @ X_Context5).
        ctx_pct = cref[2:] if cref.startswith("D_") else cref
        raw = pct.get(ctx_pct) or pct.get(cref)
        holding = None
        if raw is not None:
            try:
                holding = round(float(raw) * scale, 2)
            except ValueError:
                pass
        out.append((name.strip(), holding))
    return out


# SHP detail-table context prefix → holder category. The context id names the
# axis a >1% holder was disclosed under (e.g. D_MutualFundsOrUTI_Context15).
_CATEGORY_PREFIXES = [
    ("mutualfundsoruti", "mutual_fund"),
    ("insurancecompanies", "insurance"),
    ("foreignportfolioinvestor", "fii"),
    ("otherforeignshareholders", "fii"),
    ("otherinstitutionsforeign", "fii"),
    ("financialinstitutionsbanks", "bank_fi"),
    ("alternateinvestmentfunds", "aif"),
    ("othernoninstitutions", "individual"),
    ("individual", "individual"),
    ("bodiescorporate", "body_corporate"),
    ("centralgovernment", "government"),
    ("stategovernment", "government"),
    ("anyother", "public_other"),
]


def _holder_category(cref: str, is_promoter: bool) -> str:
    if is_promoter:
        return "promoter"
    key = cref.lower()
    for frag, cat in _CATEGORY_PREFIXES:
        if frag in key:
            return cat
    return "public"


def parse_named_holders_full(xml: str) -> List[Dict[str, Any]]:
    """Per named (>=1%) shareholder: {name, holding_pct, shares, category}.

    Extends parse_named_holders with the SEBI shares tag and a holder category.
    Reuses the same D_-prefix context join (name @ D_X_Ctx, numerics @ X_Ctx)
    and the fraction-vs-percent scale derivation. shares is frequently absent in
    the >1% table (verified across filings) and is left None when so — pct is
    the reliable field; downstream value = pct% * mcap.
    """
    pct = _local_facts(xml, "ShareholdingAsAPercentageOfTotalNumberOfShares")
    # Per-holder share count. The SEBI taxonomy tag is
    # NumberOfFullyPaidUpEquityShares (there is NO NumberOfEquitySharesHeld);
    # fall back to the plain NumberOfShares total.
    shares = _local_facts(xml, "NumberOfFullyPaidUpEquityShares")
    shares_alt = _local_facts(xml, "NumberOfShares")
    promoter_type = _local_facts(xml, "TypeOfPromoterShareholding")
    total = pct.get("ShareholdingPattern_ContextI")
    try:
        scale = 100.0 if (total is not None and float(total) <= 1.5) else 1.0
    except ValueError:
        scale = 1.0

    out: List[Dict[str, Any]] = []
    for cref, name in re.findall(
            r'<[\w-]+:NameOfTheShareholder contextRef="([^"]+)"[^>]*>([^<]+)</[\w-]+:NameOfTheShareholder>',
            xml):
        ctx = cref[2:] if cref.startswith("D_") else cref
        holding = None
        raw = pct.get(ctx) or pct.get(cref)
        if raw is not None:
            try:
                holding = round(float(raw) * scale, 2)
            except ValueError:
                pass
        sh = None
        raw_sh = (shares.get(ctx) or shares.get(cref)
                  or shares_alt.get(ctx) or shares_alt.get(cref))
        if raw_sh is not None:
            try:
                sh = int(float(raw_sh))
            except ValueError:
                pass
        is_promoter = bool(promoter_type.get(cref) or promoter_type.get(ctx))
        out.append({
            "name": name.strip(),
            "holding_pct": holding,
            "shares": sh,
            "category": _holder_category(cref, is_promoter),
        })
    return out


async def _latest_xbrl_url(symbol, client, limiter) -> Optional[str]:
    async with limiter:
        r = await client.get(SHP_MASTER_API, params={"index": "equities", "symbol": symbol},
                             headers={"Referer": SHP_REFERER})
    if "application/json" not in r.headers.get("content-type", ""):
        await refresh_nse_cookies(client)
        async with limiter:
            r = await client.get(SHP_MASTER_API, params={"index": "equities", "symbol": symbol},
                                 headers={"Referer": SHP_REFERER})
    try:
        recs = r.json()
    except Exception:
        return None
    return recs[0].get("xbrl") if isinstance(recs, list) and recs else None


async def run(symbols: List[str]) -> Dict[str, int]:
    SessionFactory = create_async_session()
    async with SessionFactory() as s:
        rows = (await s.execute(select(Company.symbol, Company.company_name))).all()
    # normalized listed-company name → symbol (skip blanks/short)
    name_to_sym: Dict[str, str] = {}
    for sym, nm in rows:
        n = _norm(nm or "")
        if len(n) >= 4:
            name_to_sym.setdefault(n, sym)

    client = await create_nse_client()
    limiter = RateLimiter(rate=2.0)
    stats = {"symbols": 0, "edges": 0}
    edges: List[Dict[str, Any]] = []
    try:
        for sym in symbols:
            sym = sym.upper().strip()
            url = await _latest_xbrl_url(sym, client, limiter)
            if not url:
                continue
            try:
                async with limiter:
                    xr = await client.get(url, headers={"Referer": SHP_REFERER})
                holders = parse_named_holders(xr.text)
            except Exception as e:
                logger.warning(f"[OWN] {sym}: {e}")
                continue
            seen = set()
            for name, pct in holders:
                tgt = name_to_sym.get(_norm(name))
                if not tgt or tgt == sym or tgt in seen:
                    continue
                if pct is None:  # weight is NOT NULL; don't fabricate a stake
                    continue
                seen.add(tgt)
                edges.append({
                    "source_symbol": sym,
                    "target_symbol": tgt,
                    "target_name": name,
                    # Accurate, neutral: "target_symbol is a listed shareholder of
                    # source_symbol." Promoter-vs-public is not reliably derivable.
                    "relationship_type": "shareholder",
                    "weight": round(pct / 100.0, 4),
                    "source_of_truth": "nse_shp",
                    "confidence": 1.0,
                })
            stats["symbols"] += 1
            logger.info(f"[OWN] {sym}: {len(seen)} listed-holder edges")
    finally:
        await client.aclose()

    if edges:
        async with SessionFactory() as s:
            for v in edges:
                stmt = pg_insert(CompanyRelationship.__table__).values(**v)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_relationship",
                    set_={k: stmt.excluded[k] for k in v
                          if k not in ("source_symbol", "target_symbol", "relationship_type")},
                )
                await s.execute(stmt)
            await s.commit()
        stats["edges"] = len(edges)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="nse_ownership_sync")
    ap.add_argument("--symbols", help="Comma-separated; default = research universe")
    args = ap.parse_args()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        from bharat_terminal.research.universe import RESEARCH_UNIVERSE
        symbols = sorted(RESEARCH_UNIVERSE)
    logger.info(f"[OWN] DONE: {asyncio.run(run(symbols))}")


if __name__ == "__main__":
    main()
