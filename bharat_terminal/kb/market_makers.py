"""Market-maker / HFT / prop-desk exclusion for bulk & block deals.

Bulk-deal disclosures (single client crossing 0.5% of a stock intraday) are
dominated by market-makers and prop/arb desks that round-trip the same name the
same day with ~zero net position — NOT informed investors. We flag those flows so
the Smart Money tab shows only directional, informed deals.

Two grounded pillars (see the live NSE bulk feed 2026-07-13, where 51/71
client-symbol pairs were same-day round-trips):

  1. Behavioural churn: a client that BOTH bought and sold the same symbol the
     same day with a near-balanced net (|net|/gross below CHURN_NET_RATIO) is
     market-making — no denylist needed, catches the long tail.
  2. Curated denylist: well-known HFT/prop/MM firms (Quadeye/QE Securities,
     Graviton, HRTI, NK Securities, Junomoneta, Microcurves, Alpha Grep, Tower
     Research, Jane Street, Optiver, Jump, XTX, Citadel Securities, Dolat, Estee,
     iRage/Quantbox, Mako, Graviton…). Names are normalized substring fragments.

Conservative by design (accuracy rule): we FLAG (is_excluded + exclude_reason),
never delete, so the filter is auditable and reversible.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List

# A round-tripped (client, symbol, day) whose net is below this fraction of gross
# traded is treated as market-making. 0.60 keeps strongly directional accumulation
# (bought 1M / sold 50k → net/gross 0.90 = kept) while dropping balanced churn.
CHURN_NET_RATIO = 0.60

_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|inc|corp|co|the|and|india|securities|"
    r"capital|markets|trading|investments?|holdings?|fund|company)\b", re.I)


def norm_client(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[.,&'\-/()]", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# Normalized fragments — matched as substrings against norm_client(name). Kept
# deliberately specific to avoid nuking a real investor that shares a word.
_DENYLIST_RAW = [
    "qe securities", "quadeye",           # Quadeye
    "graviton",                            # Graviton Research
    "hrti",                                # Hyperlink / HRTI prop
    "nk securities research", "nk securities",
    "junomoneta",                          # Junomoneta Finsol
    "microcurves",                         # Microcurves Trading
    "alphagrep", "alpha grep",
    "tower research", "tower capital",
    "jane street",
    "optiver",
    "jump trading",
    "xtx markets", "xtx",
    "citadel securities",
    "dolat capital", "dolat",
    "estee", "estee advisors",
    "irage", "quantbox", "i rage",         # iRage / Quantbox
    "mako",
    "graviton research",
    "sea securities",
    "progressive share brokers",
    "gsa capital",
    "ihs markit", "ssj finance",
    "crossseas", "cross seas",
    "chanakya", "chanakya securities",
    "manishi securities",
    "silverleaf capital",                  # Silverleaf Capital Services (prop)
    "dealmoney",                           # Dealmoney (broker prop churn)
]
_DENYLIST = [norm_client(x) for x in _DENYLIST_RAW]


def is_market_maker(client_name: str) -> bool:
    """True if the client resolves to a curated MM/HFT/prop desk."""
    n = norm_client(client_name)
    if not n:
        return False
    return any(frag and frag in n for frag in _DENYLIST)


def compute_exclusions(deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Set is_excluded + exclude_reason on each deal dict in place, then return it.

    Each deal needs: client_name, client_name_norm, symbol, side ('BUY'/'SELL'),
    quantity, deal_date. Denylist wins over churn.
    """
    # 1) denylist
    for d in deals:
        if is_market_maker(d.get("client_name", "")):
            d["is_excluded"] = True
            d["exclude_reason"] = "market_maker"
        else:
            d.setdefault("is_excluded", False)
            d.setdefault("exclude_reason", None)

    # 2) same-day round-trip churn (only among not-already-denylisted)
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for d in deals:
        if d["is_excluded"]:
            continue
        groups[(d["client_name_norm"], d.get("symbol"), d["deal_date"])].append(d)

    for legs in groups.values():
        sides = {leg["side"] for leg in legs}
        if "BUY" not in sides or "SELL" not in sides:
            continue
        buy = sum((leg["quantity"] or 0) for leg in legs if leg["side"] == "BUY")
        sell = sum((leg["quantity"] or 0) for leg in legs if leg["side"] == "SELL")
        gross = buy + sell
        if gross <= 0:
            continue
        if abs(buy - sell) / gross < CHURN_NET_RATIO:
            for leg in legs:
                leg["is_excluded"] = True
                leg["exclude_reason"] = "churn"
    return deals
