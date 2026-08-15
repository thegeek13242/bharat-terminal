"""_preopen_decision — observe-only gate with per-strategy enforcement.

Founder decision 2026-08-15, from the first accuracy readout (141 scored
decisions, 2026-08-03→08-14): the pre-open book veto was inverted for
shorts (the imbalance is priced into the auction open), so only
orderwin_long enforces; every other strategy records would_veto and fills.
Default enforcement set = {"orderwin_long"} via PAPER_PREOPEN_VETO_STRATEGIES.
"""
from bharat_terminal.papertrade.main import (
    PAPER_PREOPEN_MIN_RATIO,
    PAPER_PREOPEN_VETO_STRATEGIES,
    _preopen_decision,
)


def test_default_enforcement_set_is_orderwin_only():
    assert PAPER_PREOPEN_VETO_STRATEGIES == {"orderwin_long"}


def test_orderwin_long_low_demand_vetoes():
    confirmed, enforce = _preopen_decision("LONG", "orderwin_long", 0.3)
    assert confirmed is False and enforce is True


def test_orderwin_long_demand_confirms():
    confirmed, enforce = _preopen_decision("LONG", "orderwin_long", 2.0)
    assert confirmed is True and enforce is True


def test_headwind_short_buy_pressure_is_observe_only():
    # ratio 5.0 = heavy buying against a short: the old gate vetoed this —
    # the accuracy readout showed those were the BEST shorts. Now recorded
    # as would_veto (confirmed False) but never enforced.
    confirmed, enforce = _preopen_decision("SHORT", "headwind_short", 5.0)
    assert confirmed is False and enforce is False


def test_headwind_short_sell_pressure_confirms_unenforced():
    confirmed, enforce = _preopen_decision("SHORT", "headwind_short", 0.2)
    assert confirmed is True and enforce is False


def test_no_book_is_indeterminate_for_everyone():
    for strat in ("orderwin_long", "headwind_short", "pead_long"):
        confirmed, enforce = _preopen_decision("LONG", strat, None)
        assert confirmed is None
        # indeterminate never vetoes regardless of enforcement flag
        assert not (confirmed is False and enforce)


def test_min_ratio_boundary():
    confirmed, _ = _preopen_decision("LONG", "orderwin_long",
                                     PAPER_PREOPEN_MIN_RATIO)
    assert confirmed is True
    confirmed, _ = _preopen_decision("SHORT", "headwind_short",
                                     1.0 / PAPER_PREOPEN_MIN_RATIO)
    assert confirmed is True
