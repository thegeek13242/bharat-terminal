"""Event-study engine.

Built on synthetic prices with a PLANTED effect, so every assertion has a known
right answer. The headline test is the look-ahead one: entry must never be a
price that printed before the event.
"""
from __future__ import annotations

from datetime import date, time, timedelta

from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.engine import (
    EventInput,
    StudyConfig,
    run_event_study,
)
from bharat_terminal.factory.backtest.returns import (
    Bar,
    PriceSeries,
    benchmark_return,
    gross_return,
    has_corporate_action,
    resolve_trade_window,
    suspicious_unexplained_gap,
)

# A 120-session weekday calendar starting on a Monday.
START = date(2024, 1, 1)


def business_days(n: int, start: date = START):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


SESSIONS = business_days(120)
CAL = TradingCalendar(SESSIONS)


def flat_series(symbol: str, price: float = 100.0) -> PriceSeries:
    return PriceSeries(symbol, [Bar(d, price, price) for d in SESSIONS])


def drifting_series(symbol: str, event_date: date, drift: float, days: int) -> PriceSeries:
    """Flat at 100 until the session after `event_date`, then +drift over `days`."""
    entry_idx = CAL.next_session_index(event_date, strictly_after=True)
    bars = []
    for i, d in enumerate(SESSIONS):
        if i < entry_idx:
            px = 100.0
        elif i >= entry_idx + days:
            px = 100.0 * (1.0 + drift)
        else:
            step = (i - entry_idx + 1) / days
            px = 100.0 * (1.0 + drift * step)
        # Entry is at the OPEN of the entry session, which must still be 100.
        open_px = 100.0 if i <= entry_idx else px
        bars.append(Bar(d, open_px, px))
    return PriceSeries(symbol, bars)


# ── calendar ─────────────────────────────────────────────────────────────

def test_calendar_skips_non_trading_days():
    friday = date(2024, 1, 5)
    assert friday in CAL
    assert date(2024, 1, 6) not in CAL           # Saturday
    assert CAL.next_session(friday) == date(2024, 1, 8)


def test_calendar_shift_and_between():
    d = SESSIONS[10]
    assert CAL.shift(d, 5) == SESSIONS[15]
    assert CAL.shift(d, -3) == SESSIONS[7]
    assert CAL.sessions_between(SESSIONS[10], SESSIONS[15]) == 5


def test_calendar_edges_return_none():
    assert CAL.shift(SESSIONS[-1], 5) is None
    assert CAL.next_session(SESSIONS[-1]) is None
    assert CAL.index_of(date(2024, 1, 6)) is None


# ── entry resolution: the look-ahead guard ───────────────────────────────

def test_entry_is_strictly_after_the_event_session():
    """An evening filing must not be entered at that day's close."""
    event_date = SESSIONS[10]
    series = flat_series("ABC")
    window, reason = resolve_trade_window(
        event_date=event_date, series=series, calendar=CAL,
        entry_rule="next_session_open", holding_days=5,
    )
    assert reason is None
    assert window.entry_date == SESSIONS[11]
    assert window.entry_date > event_date
    assert window.exit_date == SESSIONS[16]
    assert window.entry_basis == "open"


def test_event_on_a_holiday_still_enters_at_the_next_session():
    saturday = date(2024, 1, 6)
    window, reason = resolve_trade_window(
        event_date=saturday, series=flat_series("ABC"), calendar=CAL,
        entry_rule="next_session_close", holding_days=1,
    )
    assert reason is None
    assert window.entry_date == date(2024, 1, 8)
    assert window.entry_basis == "close"


def test_same_session_rule_requires_a_timestamp():
    window, reason = resolve_trade_window(
        event_date=SESSIONS[10], series=flat_series("ABC"), calendar=CAL,
        entry_rule="same_session_close_if_before", holding_days=5,
    )
    assert window is None and reason == "same_session_rule_requires_timestamp"


def test_same_session_entry_only_before_the_cutoff():
    early, late = time(11, 0), time(18, 30)
    cutoff = time(15, 0)
    w_early, _ = resolve_trade_window(
        event_date=SESSIONS[10], series=flat_series("ABC"), calendar=CAL,
        entry_rule="same_session_close_if_before", holding_days=5,
        event_time_ist=early, same_session_cutoff=cutoff)
    w_late, _ = resolve_trade_window(
        event_date=SESSIONS[10], series=flat_series("ABC"), calendar=CAL,
        entry_rule="same_session_close_if_before", holding_days=5,
        event_time_ist=late, same_session_cutoff=cutoff)
    assert w_early.entry_date == SESSIONS[10]     # traded intraday
    assert w_late.entry_date == SESSIONS[11]      # after the close -> next day


def test_horizon_that_has_not_elapsed_is_rejected():
    window, reason = resolve_trade_window(
        event_date=SESSIONS[-3], series=flat_series("ABC"), calendar=CAL,
        entry_rule="next_session_open", holding_days=20,
    )
    assert window is None and reason == "horizon_not_elapsed"


# ── returns ──────────────────────────────────────────────────────────────

def test_direction_flips_the_sign():
    event_date = SESSIONS[10]
    series = drifting_series("ABC", event_date, drift=0.02, days=5)
    window, _ = resolve_trade_window(
        event_date=event_date, series=series, calendar=CAL,
        entry_rule="next_session_open", holding_days=5)
    long_ret = gross_return(window, "long")
    short_ret = gross_return(window, "short")
    assert abs(long_ret - 0.02) < 1e-9
    assert abs(short_ret + 0.02) < 1e-9


def test_benchmark_subtraction_uses_the_same_window():
    event_date = SESSIONS[10]
    bench = flat_series("NIFTYBEES")
    window, _ = resolve_trade_window(
        event_date=event_date, series=flat_series("ABC"), calendar=CAL,
        entry_rule="next_session_open", holding_days=5)
    assert benchmark_return(bench, window.entry_date, window.exit_date, "open") == 0.0


def test_corporate_action_window_detection_pads_sessions():
    start, end = SESSIONS[10], SESSIONS[15]
    assert has_corporate_action([SESSIONS[12]], start, end)
    assert not has_corporate_action([SESSIONS[20]], start, end)
    # One session of padding catches an ex-date immediately before entry.
    assert has_corporate_action([SESSIONS[9]], start, end, pad_sessions=1, calendar=CAL)


def test_unexplained_gap_detector():
    bars = [Bar(d, 100.0, 100.0) for d in SESSIONS[:5]]
    bars.append(Bar(SESSIONS[5], 50.0, 50.0))     # a 1:2 split with no CA row
    series = PriceSeries("ABC", bars)
    assert suspicious_unexplained_gap(series, SESSIONS[0], SESSIONS[5])
    assert not suspicious_unexplained_gap(flat_series("ABC"), SESSIONS[0], SESSIONS[5])


# ── the study ────────────────────────────────────────────────────────────

def build_study(n_events: int = 12, drift: float = 0.02, liquidity: float = 60.0):
    events, prices = [], {}
    for i in range(n_events):
        sym = f"SYM{i}"
        event_date = SESSIONS[10 + i]
        prices[sym] = drifting_series(sym, event_date, drift=drift, days=5)
        events.append(EventInput(symbol=sym, event_date=event_date,
                                 median_traded_value_cr=liquidity))
    return events, prices


def test_planted_drift_is_recovered_gross_and_reduced_net():
    events, prices = build_study()
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5))
    assert out.events_used == 12
    gross = sum(r.gross_abnormal for r in out.results) / out.events_used
    net = sum(r.net_abnormal for r in out.results) / out.events_used
    assert abs(gross - 0.02) < 1e-9
    # Costs must bite: statutory round trip + 2 x 5bps slippage on a liquid name.
    assert net < gross
    assert abs((gross - net) * 10_000 - out.mean_cost_bps) < 1e-6


def test_costs_can_erase_a_small_edge():
    events, prices = build_study(drift=0.002)     # 20 bps gross
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5))
    assert all(r.net_abnormal < 0 for r in out.results)


def test_illiquid_events_are_excluded_not_charged():
    events, prices = build_study(liquidity=0.4)
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5))
    assert out.events_used == 0
    assert out.exclusions["below_liquidity_floor"] == 12


def test_missing_liquidity_measure_is_an_exclusion_not_an_assumption():
    events, prices = build_study()
    events = [EventInput(e.symbol, e.event_date, None, None) for e in events]
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5))
    assert out.events_used == 0
    assert out.exclusions["no_liquidity_measure"] == 12


def test_corporate_action_events_are_dropped_and_counted():
    events, prices = build_study()
    cas = {events[0].symbol: [SESSIONS[13]]}
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5), corporate_actions=cas)
    assert out.events_used == 11
    assert out.exclusions["corporate_action_window"] == 1


def test_exclusions_are_reported_not_silent():
    events, prices = build_study(n_events=3)
    events.append(EventInput("UNKNOWN", SESSIONS[10], median_traded_value_cr=60.0))
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5))
    assert out.events_in == 4 and out.events_used == 3
    assert out.exclusions["no_price_series"] == 1


def test_cohorts_group_by_entry_week():
    events, prices = build_study(n_events=12)
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5))
    assert sum(n for _, n, _ in out.cohorts) == out.events_used
    keys = [k for k, _, _ in out.cohorts]
    assert keys == sorted(keys)
    # Every cohort key is a Monday.
    assert all(date.fromisoformat(k).weekday() == 0 for k in keys)


def test_short_direction_on_a_falling_stock_is_profitable_gross():
    events, prices = build_study(drift=-0.03)
    out = run_event_study(events, prices, flat_series("NIFTYBEES"), CAL,
                          StudyConfig(holding_days=5, direction="short"))
    assert all(r.gross_abnormal > 0 for r in out.results)
