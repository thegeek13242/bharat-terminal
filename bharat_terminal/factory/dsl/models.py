"""The experiment DSL — the only way a hypothesis becomes executable.

Agents never emit research code; they emit one of these documents. Everything
an agent could get wrong (an unknown feature, a look-ahead entry rule, a
control the data cannot support) is a *typed* field so the compiler can reject
it deterministically. See RESEARCH_FACTORY_SPEC_v2_2026-09-12.md §6.

Fields the author does NOT set — `data_tier`, `spec_hash`, trial count and
history coverage — are computed by the compiler. They are deliberately absent
from this model so an agent cannot assert them.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DSL_VERSION = 1

HYPOTHESIS_ID_RE = re.compile(r"^H-\d{6}$")

# Holding periods we are willing to test. A closed set keeps the trial count
# honest: an agent cannot invent 37 horizons and then report the best one.
ALLOWED_HOLDING_DAYS = (1, 2, 3, 5, 10, 20, 40, 60)

MAX_SWEEP_POINTS = 24
MAX_CONTROLS = 6
MAX_FILTERS = 6


class Horizon(str, Enum):
    intraday = "intraday"
    swing = "swing"
    medium = "medium"


class LineageOp(str, Enum):
    mutation = "mutation"
    combination = "combination"
    inversion = "inversion"
    generalization = "generalization"
    simplification = "simplification"
    contradiction = "contradiction"
    followup = "followup"


class EventSource(str, Enum):
    earnings_surprises = "earnings_surprises"
    results_filings = "results_filings"
    impact_events = "impact_events"
    guidance_changes = "guidance_changes"
    shp_changes = "shp_changes"
    deals = "deals"
    market_state_changes = "market_state_changes"
    commodity_shocks = "commodity_shocks"


class EventClock(str, Enum):
    """When the information became actionable.

    `announcement_date` is date-granularity on purpose: ~20% of
    earnings_surprises.announced_at rows carry IST wall-clock stored as UTC
    (verified 2026-09-11), which makes any intraday rule on that column wrong.
    """

    announcement_date = "announcement_date"
    exchange_first_seen = "exchange_first_seen"
    analysis_ready = "analysis_ready"


class EntryRule(str, Enum):
    next_session_open = "next_session_open"
    next_session_close = "next_session_close"
    same_session_close_if_before = "same_session_close_if_before"
    t_plus_minutes = "t_plus_minutes"
    preopen_auction = "preopen_auction"


class StopKind(str, Enum):
    none = "none"
    vol_trail = "vol_trail"
    flat = "flat"


class Benchmark(str, Enum):
    niftybees = "niftybees"
    juniorbees = "juniorbees"
    mid150bees = "mid150bees"
    liquidity_matched_etf = "liquidity_matched_etf"
    universe_median = "universe_median"


class Direction(str, Enum):
    long = "long"
    short = "short"


class FilterOp(str, Enum):
    gt = "gt"
    ge = "ge"
    lt = "lt"
    le = "le"
    eq = "eq"
    is_in = "in"
    decile_ge = "decile_ge"
    decile_le = "decile_le"
    pctile_ge = "pctile_ge"
    pctile_le = "pctile_le"
    zscore_ge = "zscore_ge"
    zscore_le = "zscore_le"
    changed_to = "changed_to"


class CohortUnit(str, Enum):
    calendar_week = "calendar_week"
    calendar_month = "calendar_month"


class PrimaryMetric(str, Enum):
    net_abnormal_return = "net_abnormal_return"
    gross_abnormal_return = "gross_abnormal_return"
    raw_return = "raw_return"


class CostModel(str, Enum):
    india_cash_v1 = "india_cash_v1"
    india_fno_v1 = "india_fno_v1"


class SlippageModel(str, Enum):
    adv_bucket_v1 = "adv_bucket_v1"
    flat_bps = "flat_bps"


class HoldoutPolicy(str, Enum):
    sealed_h_v1 = "sealed_h_v1"
    forward_only = "forward_only"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Parent(_Base):
    id: str
    version: int = Field(ge=1)
    op: LineageOp

    @field_validator("id")
    @classmethod
    def _id_form(cls, v: str) -> str:
        if not HYPOTHESIS_ID_RE.match(v):
            raise ValueError("parent id must look like H-000123")
        return v


class Claim(_Base):
    description: str = Field(min_length=20, max_length=600)
    mechanism: str = Field(min_length=20, max_length=800)
    falsified_if: str = Field(min_length=10, max_length=600)


class Universe(_Base):
    base: str = "listed_equity_nse_bse"
    min_median_traded_value_cr: float = Field(default=1.0, ge=0.0, le=1000.0)
    exclude: List[str] = Field(default_factory=lambda: ["fund_scheme", "suspended"])
    max_participation_pct_adv: float = Field(default=2.0, gt=0.0, le=10.0)


class EventFilter(_Base):
    feature: str          # "name@version", resolved against the registry
    op: FilterOp
    value: Any
    reference: Optional[str] = None   # e.g. trailing_365d (ranking window)


class EventBlock(_Base):
    source: EventSource
    clock: EventClock
    filters: List[EventFilter] = Field(default_factory=list, max_length=MAX_FILTERS)


class SignalBlock(_Base):
    feature: str
    direction: Direction


class EntryBlock(_Base):
    rule: EntryRule
    delay_minutes: int = Field(default=0, ge=0, le=390)
    before_time: Optional[str] = None   # "15:00" for same_session_close_if_before

    @field_validator("before_time")
    @classmethod
    def _hhmm(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("before_time must be HH:MM (IST)")
        return v


class ExitBlock(_Base):
    holding_days: List[int] = Field(min_length=1, max_length=6)
    stop: StopKind = StopKind.none
    stop_param: Optional[float] = Field(default=None, gt=0)
    squareoff_intraday: bool = False

    @field_validator("holding_days")
    @classmethod
    def _allowed(cls, v: List[int]) -> List[int]:
        bad = [d for d in v if d not in ALLOWED_HOLDING_DAYS]
        if bad:
            raise ValueError(f"holding_days {bad} outside {ALLOWED_HOLDING_DAYS}")
        if len(set(v)) != len(v):
            raise ValueError("holding_days must be unique")
        return sorted(v)

    @model_validator(mode="after")
    def _stop_param_required(self) -> "ExitBlock":
        if self.stop in (StopKind.vol_trail, StopKind.flat) and self.stop_param is None:
            raise ValueError(f"stop={self.stop.value} requires stop_param")
        if self.stop is StopKind.none and self.stop_param is not None:
            raise ValueError("stop_param set but stop=none")
        return self


class CostBlock(_Base):
    model: CostModel = CostModel.india_cash_v1
    slippage: SlippageModel = SlippageModel.adv_bucket_v1
    multiplier: float = Field(default=1.0, ge=1.0, le=5.0)
    flat_bps: Optional[float] = Field(default=None, ge=0, le=500)


class StatsBlock(_Base):
    unit: CohortUnit = CohortUnit.calendar_week
    primary_metric: PrimaryMetric = PrimaryMetric.net_abnormal_return
    primary_holding_days: int
    min_events: int = Field(default=300, ge=30)
    min_cohorts: int = Field(default=12, ge=4)

    @field_validator("primary_holding_days")
    @classmethod
    def _allowed(cls, v: int) -> int:
        if v not in ALLOWED_HOLDING_DAYS:
            raise ValueError(f"primary_holding_days {v} outside {ALLOWED_HOLDING_DAYS}")
        return v


class ValidationBlock(_Base):
    holdout: HoldoutPolicy = HoldoutPolicy.sealed_h_v1
    battery: str = "default_v1"


class HypothesisSpec(_Base):
    """One falsifiable, machine-executable claim."""

    dsl_version: int = DSL_VERSION
    id: str
    version: int = Field(default=1, ge=1)
    parents: List[Parent] = Field(default_factory=list, max_length=4)
    family: str = Field(min_length=3, max_length=64)
    question: Optional[str] = None

    horizon: Horizon
    claim: Claim
    universe: Universe = Field(default_factory=Universe)
    event: EventBlock
    signal: SignalBlock
    entry: EntryBlock
    exit_: ExitBlock = Field(alias="exit")
    benchmark: Benchmark = Benchmark.liquidity_matched_etf
    costs: CostBlock = Field(default_factory=CostBlock)
    controls: List[str] = Field(default_factory=list, max_length=MAX_CONTROLS)
    statistics: StatsBlock
    validation: ValidationBlock = Field(default_factory=ValidationBlock)
    sweep: Dict[str, List[Any]] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_form(cls, v: str) -> str:
        if not HYPOTHESIS_ID_RE.match(v):
            raise ValueError("id must look like H-000123")
        return v

    @field_validator("dsl_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != DSL_VERSION:
            raise ValueError(f"dsl_version {v} unsupported (expected {DSL_VERSION})")
        return v

    @field_validator("controls")
    @classmethod
    def _unique(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("controls must be unique")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "HypothesisSpec":
        if self.statistics.primary_holding_days not in self.exit_.holding_days:
            raise ValueError("statistics.primary_holding_days must be one of exit.holding_days")
        if self.sweep and self.sweep_points() > MAX_SWEEP_POINTS:
            raise ValueError(f"sweep expands to {self.sweep_points()} points (max {MAX_SWEEP_POINTS})")
        for path, values in self.sweep.items():
            if not values:
                raise ValueError(f"sweep path {path!r} has no values")
            if len(set(map(repr, values))) != len(values):
                raise ValueError(f"sweep path {path!r} has duplicate values")
        return self

    def sweep_points(self) -> int:
        """Total parameter combinations — every one counts as a trial (§7.3)."""
        n = 1
        for values in self.sweep.values():
            n *= len(values)
        return n

    def feature_refs(self) -> List[str]:
        """Every registry reference this spec depends on, in stable order."""
        refs = [self.signal.feature] + [f.feature for f in self.event.filters]
        seen: List[str] = []
        for r in refs:
            if r not in seen:
                seen.append(r)
        return seen
