"""
India-equity macro taxonomy for Polymarket question classification.

Each `Theme` declares:
    keywords        — case-insensitive substrings/regex hits on question+tags
    direction_on_yes — effect on Indian equities if the YES outcome wins
                      (e.g. "Fed cuts rates" YES → bullish for India: FII flows)
    sectors_positive — NSE sectors that benefit from a YES-direction move
    sectors_negative — NSE sectors hurt by a YES-direction move
    watchlist_symbols — representative NSE symbols to surface in the UI
    base_magnitude   — default impact magnitude (1-5); scaled by |delta_1h|
    noise_floor      — min |delta_1h| to emit a snapshot (overrides global)

The classifier walks themes in order and picks the first match. Order matters:
specific themes (e.g. INDIA_ELECTIONS) appear before generic fallbacks
(e.g. CRYPTO_RISK_ON).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional


Direction = Literal["bullish_in", "bearish_in", "neutral", "ambiguous"]


@dataclass(frozen=True)
class Theme:
    name: str
    keywords: tuple[str, ...]
    direction_on_yes: Direction
    sectors_positive: tuple[str, ...] = ()
    sectors_negative: tuple[str, ...] = ()
    watchlist_symbols: tuple[str, ...] = ()
    base_magnitude: int = 2
    noise_floor: float = 0.02
    explanation_template: str = (
        "Polymarket implied probability of '{question}' moved by {delta_pp:+.1f}pp "
        "to {prob:.0%}. Theme={theme}, India-equity bias={direction}."
    )


# Strict universe — Fed + commodities only.
#
# We intentionally dropped INDIA_ELECTIONS, RBI_RATES, INR_RUPEE, MIDEAST,
# US_CHINA, RUSSIA_UKRAINE, US_RECESSION, US_INFLATION, CRYPTO_RISK_ON.
# Those signals are either (a) too noisy on Polymarket, (b) have no liquid
# markets at the $1M+ volume floor, or (c) don't translate cleanly into a
# directional Indian-equity bias. We can re-introduce them later once a
# specific signal proves itself in backtesting.
#
# Order = priority. First match wins.
THEMES: tuple[Theme, ...] = (
    # ── US monetary policy (FII flow driver) ────────────────────────────────
    Theme(
        name="FED_RATES",
        keywords=(
            "fed", "fomc", "powell", "federal reserve",
            "rate cut", "rate hike", "rate decision", "interest rate",
            "basis points", "bps cut", "bps hike",
        ),
        direction_on_yes="bullish_in",  # rate cut YES → FII inflows → bullish India
        sectors_positive=("IT", "BANK", "REALTY", "NBFC"),
        sectors_negative=(),
        watchlist_symbols=("TCS", "INFY", "HDFCBANK", "ICICIBANK", "BAJFINANCE"),
        base_magnitude=4,
        noise_floor=0.02,
    ),

    # ── Oil (India imports ~85% of crude) ───────────────────────────────────
    Theme(
        name="OIL_PRICES",
        keywords=("oil", "crude", "brent", "wti", "opec"),
        direction_on_yes="bearish_in",  # higher oil YES → CAD pressure, inflation, bearish
        sectors_positive=("UPSTREAM",),
        sectors_negative=("OIL_MARKETING", "PAINTS", "AVIATION", "AUTO"),
        watchlist_symbols=("ONGC", "OIL", "BPCL", "HPCL", "IOC", "ASIANPAINT", "INDIGO"),
        base_magnitude=3,
        noise_floor=0.03,
    ),

    # ── Precious metals (India: world's largest gold consumer) ──────────────
    Theme(
        name="GOLD_PRICES",
        keywords=("gold", "silver"),
        direction_on_yes="ambiguous",   # high gold = jewellery margin pressure but
                                        # also gold-loan NBFCs & inflation hedge demand
        sectors_positive=("GOLD_LOAN",),
        sectors_negative=("JEWELLERY",),
        watchlist_symbols=("MUTHOOTFIN", "MANAPPURAM", "TITAN", "KALYANKJIL"),
        base_magnitude=2,
        noise_floor=0.03,
    ),

    # ── Industrial metals (capex-cycle proxy) ───────────────────────────────
    Theme(
        name="INDUSTRIAL_METALS",
        keywords=(
            "copper", "aluminium", "aluminum", "steel", "iron ore",
        ),
        direction_on_yes="bullish_in",  # rising metals = global capex cycle = metals stocks
        sectors_positive=("METALS",),
        sectors_negative=("AUTO", "CAPITAL_GOODS"),  # input-cost pressure for downstream
        watchlist_symbols=("TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "NMDC"),
        base_magnitude=3,
        noise_floor=0.03,
    ),

    # ── Natural gas (city gas distributors, fertilisers, power) ─────────────
    Theme(
        name="NATURAL_GAS",
        keywords=("natural gas", "nat gas"),
        direction_on_yes="bearish_in",  # higher nat-gas → CGD margin squeeze, fertiliser cost
        sectors_positive=("UPSTREAM",),
        sectors_negative=("CITY_GAS", "FERTILISER", "POWER"),
        watchlist_symbols=("ONGC", "GAIL", "IGL", "MGL", "CHAMBLFERT"),
        base_magnitude=2,
        noise_floor=0.04,
    ),
)


@lru_cache(maxsize=512)
def _kw_pattern(kw: str) -> re.Pattern:
    """
    Compile a word-boundary regex for a keyword.

    Substring matching causes false positives — e.g. 'eth' inside 'something',
    'oil' inside 'spoiler'. We anchor to word boundaries so 'fed' matches
    'fed' / 'Fed:' / 'the Fed,' but not 'federation'-as-prefix.

    Multi-word keywords (e.g. 'rate cut') get \\b on the outer ends only.
    """
    parts = re.escape(kw.strip().lower())
    return re.compile(rf"\b{parts}\b", re.IGNORECASE)


def find_theme(haystack: str) -> Optional[Theme]:
    """Return the first theme whose keywords match the haystack at word boundaries."""
    h = haystack.lower()
    for theme in THEMES:
        for kw in theme.keywords:
            if _kw_pattern(kw).search(h):
                return theme
    return None


def scale_magnitude(base: int, delta_abs: float) -> int:
    """
    Scale a theme's base magnitude by the size of the probability move.

    |delta| ≥ 0.10  → +2  (huge move, e.g. binary surprise)
    |delta| ≥ 0.05  → +1
    |delta| ≥ 0.02  →  0
    else            → -1  (tiny — likely will be filtered upstream anyway)

    Clamped to [1, 5].
    """
    if delta_abs >= 0.10:
        bump = 2
    elif delta_abs >= 0.05:
        bump = 1
    elif delta_abs >= 0.02:
        bump = 0
    else:
        bump = -1
    return max(1, min(5, base + bump))


def resolve_direction(theme: Theme, delta_1h: Optional[float]) -> Direction:
    """
    Resolve direction given the theme's bias and the delta sign.

        theme.direction_on_yes = bullish_in
            delta > 0  → bullish_in     (prob of bullish event rose)
            delta < 0  → bearish_in     (prob of bullish event fell)
        theme.direction_on_yes = bearish_in
            delta > 0  → bearish_in
            delta < 0  → bullish_in
        ambiguous / neutral             → as declared
    """
    if theme.direction_on_yes in ("ambiguous", "neutral"):
        return theme.direction_on_yes

    if delta_1h is None or delta_1h == 0.0:
        return theme.direction_on_yes  # first observation — assume YES-direction

    if delta_1h > 0:
        return theme.direction_on_yes
    return "bearish_in" if theme.direction_on_yes == "bullish_in" else "bullish_in"
