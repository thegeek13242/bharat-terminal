"""Shared fixtures for factory tests.

`make_spec()` returns a spec that MUST compile cleanly. Every compiler test
then breaks exactly one thing, so a failure names the rule that broke.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict

import pytest

from bharat_terminal.factory.dsl.compiler import CompileContext
from bharat_terminal.factory.dsl.models import HypothesisSpec


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


BASE_SPEC: Dict[str, Any] = {
    "dsl_version": 1,
    "id": "H-000031",
    "version": 1,
    "parents": [{"id": "H-000012", "version": 2, "op": "mutation"}],
    "family": "pead_liquidity",
    "question": "RQ-000007",
    "horizon": "swing",
    "claim": {
        "description": "Top-decile composite-SUE surprises in liquid names drift up over 5-20 sessions.",
        "mechanism": "Investors anchor on the naive headline number and under-react to the revenue leg.",
        "falsified_if": "Net abnormal return <= 0 or cohort t < 3 pre-holdout.",
    },
    "universe": {
        "base": "listed_equity_nse_bse",
        "min_median_traded_value_cr": 5.0,
        "exclude": ["fund_scheme", "suspended"],
        "max_participation_pct_adv": 2.0,
    },
    "event": {
        "source": "earnings_surprises",
        "clock": "announcement_date",
        "filters": [
            {
                "feature": "sue_composite@2.1",
                "op": "decile_ge",
                "value": 10,
                "reference": "trailing_365d",
            }
        ],
    },
    "signal": {"feature": "sue_composite@2.1", "direction": "long"},
    "entry": {"rule": "next_session_open", "delay_minutes": 0},
    "exit": {"holding_days": [5, 10, 20], "stop": "none", "squareoff_intraday": False},
    "benchmark": "liquidity_matched_etf",
    "costs": {"model": "india_cash_v1", "slippage": "adv_bucket_v1", "multiplier": 1.0},
    "controls": ["liquidity_tier"],
    "statistics": {
        "unit": "calendar_week",
        "primary_metric": "net_abnormal_return",
        "primary_holding_days": 20,
        "min_events": 300,
        "min_cohorts": 12,
    },
    "validation": {"holdout": "sealed_h_v1", "battery": "default_v1"},
    "sweep": {},
}


def make_spec(**overrides: Any) -> HypothesisSpec:
    return HypothesisSpec.model_validate(_deep_merge(BASE_SPEC, overrides))


def make_ctx(**overrides: Any) -> CompileContext:
    defaults: Dict[str, Any] = {
        "dataset_version": "DS-20260912-01",
        "snapshot_age_hours": 2.0,
        "source_max_dates": {"price_points": date(2026, 9, 11)},
        # The measured clock lag is real, so tests carry its flag by default.
        "integrity_flags": frozenset({"clock_lags_dissemination"}),
        "integrity_failures": frozenset(),
        "sealed_from": date(2025, 9, 12),
        "embargoes": (),
        "known_spec_hashes": {},
        "family_trials_remaining": 100,
        "event_counter": None,
    }
    defaults.update(overrides)
    return CompileContext(**defaults)


@pytest.fixture
def spec() -> HypothesisSpec:
    return make_spec()


@pytest.fixture
def ctx() -> CompileContext:
    return make_ctx()
