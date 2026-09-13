"""Run one experiment: events in, cohort statistics out.

Every exclusion is counted and returned. A silent drop is how a backtest
quietly becomes a survivorship study, so `StudyOutput.exclusions` is part of
the result and lands in the ledger next to the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Dict, List, Mapping, Optional, Sequence

from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.returns import (
    PriceSeries,
    TradeWindow,
    benchmark_return,
    gross_return,
    has_corporate_action,
    resolve_trade_window,
)
from bharat_terminal.factory.costs.india_cash import (
    CASH_DELIVERY_V1,
    RateCard,
    round_trip_bps,
)
from bharat_terminal.factory.costs.slippage import slippage_bps


@dataclass(frozen=True)
class EventInput:
    symbol: str
    event_date: date
    event_time_ist: Optional[time] = None
    median_traded_value_cr: Optional[float] = None
    meta: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class StudyConfig:
    entry_rule: str = "next_session_open"
    holding_days: int = 20
    direction: str = "long"
    same_session_cutoff: Optional[time] = None
    rate_card: RateCard = CASH_DELIVERY_V1
    cost_multiplier: float = 1.0
    order_value_inr: float = 100_000.0
    min_median_traded_value_cr: float = 1.0
    exclude_ca_windows: bool = True
    ca_pad_sessions: int = 1
    flag_unexplained_gaps: bool = True


@dataclass(frozen=True)
class EventResult:
    symbol: str
    event_date: date
    entry_date: date
    exit_date: date
    gross_return: float
    benchmark_return: float
    gross_abnormal: float
    cost_bps: float
    net_abnormal: float
    cohort_key: str


@dataclass(frozen=True)
class StudyOutput:
    events_in: int
    results: List[EventResult]
    exclusions: Dict[str, int]
    cohorts: List[tuple]          # (cohort_key, n_events, mean_net_abnormal)
    mean_cost_bps: float

    @property
    def events_used(self) -> int:
        return len(self.results)


def _cohort_key(d: date) -> str:
    """ISO date of the Monday of the entry week."""
    monday = d.fromordinal(d.toordinal() - d.weekday())
    return monday.isoformat()


def run_event_study(
    events: Sequence[EventInput],
    prices: Mapping[str, PriceSeries],
    benchmark: PriceSeries,
    calendar: TradingCalendar,
    config: StudyConfig | None = None,
    corporate_actions: Optional[Mapping[str, Sequence[date]]] = None,
) -> StudyOutput:
    cfg = config or StudyConfig()
    cas = corporate_actions or {}
    results: List[EventResult] = []
    exclusions: Dict[str, int] = {}
    costs: List[float] = []

    def drop(reason: str) -> None:
        exclusions[reason] = exclusions.get(reason, 0) + 1

    for ev in events:
        series = prices.get(ev.symbol)
        if series is None or len(series) == 0:
            drop("no_price_series")
            continue

        # Liquidity screen first: an untradeable name must leave the sample,
        # not merely carry a bigger fee.
        if ev.median_traded_value_cr is None:
            drop("no_liquidity_measure")
            continue
        if ev.median_traded_value_cr < cfg.min_median_traded_value_cr:
            drop("below_liquidity_floor")
            continue
        slip = slippage_bps(ev.median_traded_value_cr, order_value_inr=cfg.order_value_inr)
        if slip is None:
            drop("untradeable")
            continue

        window, reason = resolve_trade_window(
            event_date=ev.event_date,
            series=series,
            calendar=calendar,
            entry_rule=cfg.entry_rule,
            holding_days=cfg.holding_days,
            event_time_ist=ev.event_time_ist,
            same_session_cutoff=cfg.same_session_cutoff,
        )
        if window is None:
            drop(reason or "no_window")
            continue

        if cfg.exclude_ca_windows and has_corporate_action(
            cas.get(ev.symbol, ()), window.entry_date, window.exit_date,
            pad_sessions=cfg.ca_pad_sessions, calendar=calendar,
        ):
            drop("corporate_action_window")
            continue

        bench = benchmark_return(benchmark, window.entry_date, window.exit_date, window.entry_basis)
        if bench is None:
            drop("no_benchmark_price")
            continue

        gross = gross_return(window, cfg.direction)
        statutory = round_trip_bps(cfg.rate_card, cfg.order_value_inr)
        cost_bps = (statutory + 2.0 * slip) * cfg.cost_multiplier
        costs.append(cost_bps)

        # The benchmark carries the POSITION's sign. A short's market-adjusted
        # return is -(stock - benchmark); subtracting the benchmark from an
        # already-negated return would double-count it, making every short look
        # worse by twice the market move.
        sign = 1.0 if cfg.direction == "long" else -1.0
        gross_abn = gross - sign * bench
        net_abn = gross_abn - cost_bps / 10_000.0

        results.append(EventResult(
            symbol=ev.symbol,
            event_date=ev.event_date,
            entry_date=window.entry_date,
            exit_date=window.exit_date,
            gross_return=gross,
            benchmark_return=bench,
            gross_abnormal=gross_abn,
            cost_bps=cost_bps,
            net_abnormal=net_abn,
            cohort_key=_cohort_key(window.entry_date),
        ))

    grouped: Dict[str, List[float]] = {}
    for r in results:
        grouped.setdefault(r.cohort_key, []).append(r.net_abnormal)
    cohorts = [(k, len(v), sum(v) / len(v)) for k, v in sorted(grouped.items())]

    return StudyOutput(
        events_in=len(events),
        results=results,
        exclusions=exclusions,
        cohorts=cohorts,
        mean_cost_bps=(sum(costs) / len(costs)) if costs else 0.0,
    )
