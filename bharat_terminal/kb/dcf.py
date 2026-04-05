"""
DCF (Discounted Cash Flow) calculator with WACC estimation, terminal value,
and bull/base/bear sensitivity table.
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# Indian market defaults
RISK_FREE_RATE = 0.072       # 10Y Gsec yield ~7.2% as of 2025
MARKET_PREMIUM = 0.065       # India ERP ~6.5%
DEFAULT_BETA = 1.0
DEFAULT_COST_OF_DEBT = 0.09  # ~9% for average India corporate
DEFAULT_TAX_RATE = 0.25      # Base corporate tax rate India
DEFAULT_TERMINAL_GROWTH = 0.06  # ~6% nominal (India long-term GDP growth)


@dataclass
class DCFInputs:
    symbol: str
    revenue_ttm_cr: float
    ebitda_margin_pct: float
    net_debt_cr: float
    shares_outstanding_cr: float  # Crores of shares

    # Capital structure (for WACC)
    equity_value_cr: Optional[float] = None   # Market cap
    debt_value_cr: Optional[float] = None
    beta: float = DEFAULT_BETA
    cost_of_debt: float = DEFAULT_COST_OF_DEBT
    tax_rate: float = DEFAULT_TAX_RATE

    # Growth assumptions
    revenue_growth_y1: float = 0.12   # 12% Year 1
    revenue_growth_y2: float = 0.11
    revenue_growth_y3: float = 0.10
    revenue_growth_y4: float = 0.09
    revenue_growth_y5: float = 0.08

    terminal_growth_pct: float = DEFAULT_TERMINAL_GROWTH

    # Margin assumptions
    ebitda_margin_terminal: Optional[float] = None  # If None, use current margin
    capex_pct_of_revenue: float = 0.05
    working_capital_change_pct: float = 0.02

    # Data source tracking
    source: str = "screener_api"
    confidence: float = 0.8


@dataclass
class DCFProjectionYear:
    year: int
    revenue: float
    ebitda: float
    pat: float
    eps: float
    fcf: float
    discounted_fcf: float


@dataclass
class DCFResult:
    symbol: str
    wacc_pct: float
    terminal_growth_pct: float
    year_projections: List[DCFProjectionYear]
    terminal_value: float
    pv_terminal_value: float
    pv_fcfs: float
    enterprise_value: float
    equity_value: float
    fair_value_per_share: float

    # Sensitivity
    bull_value: float    # +2% revenue growth, +1% margin
    base_value: float    # Base case
    bear_value: float    # -2% revenue growth, -1% margin

    margin_of_safety_pct: float  # (fair_value - current_price) / fair_value * 100

    source: str = "screener_api"
    confidence: float = 0.8


def estimate_wacc(inputs: DCFInputs) -> float:
    """
    Estimate WACC using CAPM for cost of equity and given cost of debt.

    WACC = (E/V) * Ke + (D/V) * Kd * (1 - T)
    where Ke = Rf + Beta * ERP (CAPM)
    """
    cost_of_equity = RISK_FREE_RATE + inputs.beta * MARKET_PREMIUM

    equity_val = inputs.equity_value_cr or (inputs.revenue_ttm_cr * 3)  # Rough proxy
    debt_val = inputs.debt_value_cr or max(0, inputs.net_debt_cr)
    total_val = equity_val + debt_val

    if total_val <= 0:
        return cost_of_equity

    weight_equity = equity_val / total_val
    weight_debt = debt_val / total_val

    wacc = (weight_equity * cost_of_equity +
            weight_debt * inputs.cost_of_debt * (1 - inputs.tax_rate))

    # Clamp to reasonable range [6%, 20%]
    return max(0.06, min(0.20, wacc))


def project_fcf(
    revenue_ttm: float,
    ebitda_margin: float,
    revenue_growths: List[float],
    capex_pct: float,
    wc_change_pct: float,
    tax_rate: float,
    wacc: float,
    shares_cr: float,
) -> List[DCFProjectionYear]:
    """Project free cash flows for 5 years with discounting."""
    projections = []
    current_revenue = revenue_ttm

    for year, growth in enumerate(revenue_growths, start=1):
        projected_revenue = current_revenue * (1 + growth)
        projected_ebitda = projected_revenue * ebitda_margin

        # EBIT = EBITDA - D&A (assume D&A = 3% of revenue)
        da = projected_revenue * 0.03
        ebit = projected_ebitda - da

        # NOPAT = EBIT * (1 - tax)
        nopat = ebit * (1 - tax_rate)

        # FCF = NOPAT + D&A - Capex - Working Capital change
        capex = projected_revenue * capex_pct
        wc_change = projected_revenue * wc_change_pct
        fcf = nopat + da - capex - wc_change

        discount_factor = 1 / ((1 + wacc) ** year)
        discounted_fcf = fcf * discount_factor

        # PAT proxy (NOPAT for simplicity)
        pat = nopat
        eps = pat / shares_cr if shares_cr > 0 else 0

        projections.append(DCFProjectionYear(
            year=year,
            revenue=round(projected_revenue, 2),
            ebitda=round(projected_ebitda, 2),
            pat=round(pat, 2),
            eps=round(eps, 2),
            fcf=round(fcf, 2),
            discounted_fcf=round(discounted_fcf, 2),
        ))

        current_revenue = projected_revenue

    return projections


def compute_dcf(inputs: DCFInputs, current_price: Optional[float] = None) -> DCFResult:
    """
    Compute DCF valuation with bull/base/bear sensitivity table.

    Args:
        inputs: DCFInputs with all required fields
        current_price: Current market price for margin of safety calculation

    Returns:
        DCFResult with full valuation
    """
    wacc = estimate_wacc(inputs)
    terminal_growth = inputs.terminal_growth_pct

    ebitda_margin = inputs.ebitda_margin_pct / 100
    terminal_margin = (inputs.ebitda_margin_terminal or inputs.ebitda_margin_pct) / 100

    revenue_growths = [
        inputs.revenue_growth_y1,
        inputs.revenue_growth_y2,
        inputs.revenue_growth_y3,
        inputs.revenue_growth_y4,
        inputs.revenue_growth_y5,
    ]

    shares = inputs.shares_outstanding_cr

    # Base case
    projections = project_fcf(
        inputs.revenue_ttm_cr, ebitda_margin, revenue_growths,
        inputs.capex_pct_of_revenue, inputs.working_capital_change_pct,
        inputs.tax_rate, wacc, shares
    )

    pv_fcfs = sum(p.discounted_fcf for p in projections)

    # Terminal value (Gordon Growth Model on FCF)
    final_fcf = projections[-1].fcf
    terminal_value = final_fcf * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1 + wacc) ** len(projections))

    enterprise_value = pv_fcfs + pv_terminal
    equity_value = enterprise_value - (inputs.net_debt_cr or 0)
    fair_value = equity_value / shares if shares > 0 else 0

    # Bull: +2% revenue growth, +1% EBITDA margin
    bull_growths = [g + 0.02 for g in revenue_growths]
    bull_projs = project_fcf(
        inputs.revenue_ttm_cr, ebitda_margin + 0.01, bull_growths,
        inputs.capex_pct_of_revenue, inputs.working_capital_change_pct,
        inputs.tax_rate, wacc * 0.98, shares
    )
    bull_pv_fcfs = sum(p.discounted_fcf for p in bull_projs)
    bull_final_fcf = bull_projs[-1].fcf
    bull_tv = bull_final_fcf * (1 + terminal_growth) / ((wacc * 0.98) - terminal_growth)
    bull_pv_tv = bull_tv / ((1 + wacc * 0.98) ** 5)
    bull_ev = bull_pv_fcfs + bull_pv_tv - (inputs.net_debt_cr or 0)
    bull_value = bull_ev / shares if shares > 0 else 0

    # Bear: -2% revenue growth, -1% EBITDA margin
    bear_growths = [max(0, g - 0.02) for g in revenue_growths]
    bear_projs = project_fcf(
        inputs.revenue_ttm_cr, max(0.05, ebitda_margin - 0.01), bear_growths,
        inputs.capex_pct_of_revenue, inputs.working_capital_change_pct,
        inputs.tax_rate, wacc * 1.02, shares
    )
    bear_pv_fcfs = sum(p.discounted_fcf for p in bear_projs)
    bear_final_fcf = bear_projs[-1].fcf
    bear_tv = bear_final_fcf * (1 + terminal_growth) / ((wacc * 1.02) - terminal_growth)
    bear_pv_tv = bear_tv / ((1 + wacc * 1.02) ** 5)
    bear_ev = bear_pv_fcfs + bear_pv_tv - (inputs.net_debt_cr or 0)
    bear_value = bear_ev / shares if shares > 0 else 0

    # Margin of safety
    mos_pct = 0.0
    if current_price and fair_value > 0:
        mos_pct = ((fair_value - current_price) / fair_value) * 100

    return DCFResult(
        symbol=inputs.symbol,
        wacc_pct=round(wacc * 100, 2),
        terminal_growth_pct=round(terminal_growth * 100, 2),
        year_projections=projections,
        terminal_value=round(terminal_value, 2),
        pv_terminal_value=round(pv_terminal, 2),
        pv_fcfs=round(pv_fcfs, 2),
        enterprise_value=round(enterprise_value, 2),
        equity_value=round(equity_value, 2),
        fair_value_per_share=round(fair_value, 2),
        bull_value=round(bull_value, 2),
        base_value=round(fair_value, 2),
        bear_value=round(bear_value, 2),
        margin_of_safety_pct=round(mos_pct, 2),
        source=inputs.source,
        confidence=inputs.confidence,
    )


def print_sensitivity_table(result: DCFResult):
    """Print a formatted DCF sensitivity table."""
    print(f"\n{'='*60}")
    print(f"DCF VALUATION: {result.symbol}")
    print(f"{'='*60}")
    print(f"WACC: {result.wacc_pct:.1f}% | Terminal Growth: {result.terminal_growth_pct:.1f}%")
    print(f"\n{'Year':<6} {'Revenue':>10} {'EBITDA':>10} {'FCF':>10} {'PV(FCF)':>10}")
    print("-" * 50)
    for p in result.year_projections:
        print(f"{p.year:<6} {p.revenue:>10.0f} {p.ebitda:>10.0f} {p.fcf:>10.0f} {p.discounted_fcf:>10.0f}")
    print("-" * 50)
    print(f"{'PV of FCFs':<20} {result.pv_fcfs:>10.0f}")
    print(f"{'PV of Terminal Value':<20} {result.pv_terminal_value:>10.0f}")
    print(f"{'Enterprise Value':<20} {result.enterprise_value:>10.0f}")
    print(f"{'Equity Value':<20} {result.equity_value:>10.0f}")
    print(f"\n{'SCENARIO ANALYSIS':^30}")
    print(f"{'Bull':>12} {'Base':>12} {'Bear':>12}")
    print(f"{'Rs.' + str(round(result.bull_value)):>12} {'Rs.' + str(round(result.base_value)):>12} {'Rs.' + str(round(result.bear_value)):>12}")
    print(f"\nMargin of Safety: {result.margin_of_safety_pct:+.1f}%")
    print(f"Source: {result.source} | Confidence: {result.confidence:.0%}")
    print('='*60)
