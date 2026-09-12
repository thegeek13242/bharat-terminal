"""Seed feature registry.

Every entry states the three things the compiler needs to refuse bad science:

  history_start   when the underlying source actually begins (VERIFIED by query
                  on 2026-09-11/12 — see RESEARCH_FACTORY_SPEC_v2 §1.1), which
                  decides whether a hypothesis can be tested historically at all;
  data_tier       H = multi-year point-in-time history, eligible for the sealed
                  holdout. F = months only (the LLM/event-pipeline era), which
                  can only be validated forward in SHADOW;
  coverage        fraction of rows where the field is actually populated.
                  `None` means UNMEASURED — the compiler treats unmeasured
                  coverage as a hard failure for controls rather than assuming.

A feature is a *code* artifact: adding one is a PR the founder merges, never an
agent write. Agents compose registered features; they cannot invent them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    version: str
    source_table: str
    data_tier: str                  # "H" | "F"
    history_start: date
    value_kind: str                 # float | int | enum | bool | date
    description: str
    # Clocks this feature is point-in-time safe for. A feature whose value is
    # only known after the event cannot be used on an earlier clock.
    valid_clocks: Tuple[str, ...]
    coverage: Optional[float] = None
    notes: str = ""
    control_eligible: bool = False
    integrity_flags: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


_ALL_CLOCKS = ("announcement_date", "exchange_first_seen", "analysis_ready")


def _f(**kw) -> FeatureSpec:
    return FeatureSpec(**kw)


_FEATURES: List[FeatureSpec] = [
    # ── Tier H: multi-year, point-in-time ────────────────────────────────
    _f(
        name="sue_composite", version="2.1", source_table="earnings_surprises",
        data_tier="H", history_start=date(2020, 1, 1), value_kind="float",
        description="Composite standardized unexpected earnings (EPS-YoY + revenue-YoY, rank-averaged).",
        valid_clocks=("announcement_date", "exchange_first_seen"), coverage=1.0,
        notes=("Frozen live recipe, study_version=pead_v2.1_2026_07. 43,082 rows 2020→2026-09-11. "
               "Its announced_at clock trails first dissemination: median 0.20h via bse_xbrl, "
               "6.15h via nse_xbrl (measured 2026-09-12)."),
    ),
    _f(
        name="sue_eps", version="2.1", source_table="earnings_surprises",
        data_tier="H", history_start=date(2020, 1, 1), value_kind="float",
        description="EPS-only standardized unexpected earnings.",
        valid_clocks=("announcement_date", "exchange_first_seen"), coverage=1.0,
    ),
    _f(
        name="sue_revenue", version="2.1", source_table="earnings_surprises",
        data_tier="H", history_start=date(2020, 1, 1), value_kind="float",
        description="Revenue-only standardized unexpected surprise.",
        valid_clocks=("announcement_date", "exchange_first_seen"), coverage=1.0,
    ),
    _f(
        name="expectation_surprise_gbm", version="1.0", source_table="expectations",
        data_tier="H", history_start=date(2023, 4, 6), value_kind="float",
        description="Actual vs the E1 global-GBM expectation (fundamental surprise).",
        valid_clocks=("announcement_date",), coverage=None,
        notes="Append-only table with inputs_max_ts; PIT auditable. Coverage per metric UNMEASURED.",
    ),
    _f(
        name="expectation_surprise_guidance", version="1.0", source_table="expectations",
        data_tier="H", history_start=date(2024, 4, 5), value_kind="float",
        description="Actual vs the guidance-derived expectation (expectation-gap surprise).",
        valid_clocks=("announcement_date",), coverage=None,
        notes="Bounded by transcript history (2024-04-05).",
    ),
    _f(
        name="guidance_change", version="1.0", source_table="transcript_guidance",
        data_tier="H", history_start=date(2024, 4, 5), value_kind="enum",
        description="Management guidance raised / lowered / reiterated / new.",
        valid_clocks=("announcement_date",), coverage=None,
        notes="Two-model corpus (Haiku + Sonnet) — disclose in any write-up.",
    ),
    _f(
        name="liquidity_tier", version="1.0", source_table="price_points",
        data_tier="H", history_start=date(2017, 11, 28), value_kind="enum",
        description="Tercile of 20-session median traded value (close x volume), point-in-time.",
        valid_clocks=_ALL_CLOCKS, coverage=1.0, control_eligible=True,
    ),
    _f(
        name="median_traded_value_cr", version="1.0", source_table="price_points",
        data_tier="H", history_start=date(2017, 11, 28), value_kind="float",
        description="20-session median traded value in Rs crore, point-in-time.",
        valid_clocks=_ALL_CLOCKS, coverage=1.0, control_eligible=True,
    ),
    _f(
        name="results_season", version="1.0", source_table="earnings_surprises",
        data_tier="H", history_start=date(2020, 1, 1), value_kind="bool",
        description="Week sits above the 60th percentile of trailing-2y results density.",
        valid_clocks=_ALL_CLOCKS, coverage=1.0, control_eligible=True,
    ),
    _f(
        name="ca_window", version="1.0", source_table="corporate_actions",
        data_tier="H", history_start=date(2005, 1, 1), value_kind="bool",
        description="A split/bonus/rights ex-date falls inside the trade window.",
        valid_clocks=_ALL_CLOCKS, coverage=None,
        notes="20,339 rows; one corrupt ex_date (0024-09-20). Closes are UNADJUSTED, so v1 excludes these windows.",
        integrity_flags=("unadjusted_closes",),
    ),
    _f(
        name="shp_promoter_delta", version="1.0", source_table="shareholding_patterns",
        data_tier="H", history_start=date(2011, 6, 30), value_kind="float",
        description="Quarter-on-quarter change in promoter holding percentage.",
        valid_clocks=("announcement_date",), coverage=None,
        notes="Recent-quarter ingestion known incomplete — measure before use.",
    ),
    # ── Tier F: the LLM / event-pipeline era. Forward validation only. ───
    _f(
        name="event_type_v2", version="1.0", source_table="news_items",
        data_tier="F", history_start=date(2024, 1, 1), value_kind="enum",
        description="22-bucket filing taxonomy derived deterministically from the headline.",
        valid_clocks=("exchange_first_seen", "analysis_ready"), coverage=None,
        notes="Deterministic (no LLM), but headline history is thin before 2026: BSE 2,817 rows in 2024, NSE only from 2026.",
        control_eligible=True,
    ),
    _f(
        name="impact_magnitude", version="1.0", source_table="impact_reports",
        data_tier="F", history_start=date(2026, 5, 10), value_kind="int",
        description="Stage-3 company impact magnitude 1-5 (LLM).",
        valid_clocks=("analysis_ready",), coverage=None,
        notes="impact_reports begins 2026-05-10. Historical re-analysis is NOT valid evidence (model knows the future).",
    ),
    _f(
        name="impact_sentiment", version="1.0", source_table="impact_reports",
        data_tier="F", history_start=date(2026, 5, 10), value_kind="enum",
        description="Stage-3 company impact sentiment (LLM).",
        valid_clocks=("analysis_ready",), coverage=None,
    ),
    _f(
        name="materiality_p", version="4.1", source_table="impact_reports",
        data_tier="F", history_start=date(2026, 5, 10), value_kind="float",
        description="Calibrated P(material) from the v4.1 gate artifact.",
        valid_clocks=("analysis_ready",), coverage=None,
        notes="Artifact fit through 2026-07-27 and known ~5pp overconfident in the top decile.",
        integrity_flags=("artifact_lookahead_suspect",),
    ),
    _f(
        name="deal_flag", version="1.0", source_table="market_deals",
        data_tier="F", history_start=date(2026, 6, 15), value_kind="bool",
        description="Bulk/block deal recorded for the symbol on the date.",
        valid_clocks=("announcement_date", "exchange_first_seen"), coverage=None,
    ),
    _f(
        name="market_state_flag", version="1.0", source_table="market_state",
        data_tier="F", history_start=date(2026, 7, 30), value_kind="enum",
        description="Exchange surveillance state (ASM/GSM/ESM stage) and price band.",
        valid_clocks=_ALL_CLOCKS, coverage=None, control_eligible=True,
    ),
    _f(
        name="commodity_shock", version="1.0", source_table="news_items",
        data_tier="F", history_start=date(2026, 7, 8), value_kind="enum",
        description="Commodity shock detector firing (z>=3 and 10-min move >=1.2%).",
        valid_clocks=("exchange_first_seen",), coverage=None,
        notes="61 shock items to date; detector last fired 2026-08-27.",
    ),
    # ── Registered but NOT usable as a control: coverage too low ─────────
    _f(
        name="sector_nse", version="1.0", source_table="companies",
        data_tier="H", history_start=date(2017, 11, 28), value_kind="enum",
        description="NSE sector label.",
        valid_clocks=_ALL_CLOCKS, coverage=0.188, control_eligible=True,
        notes="18.8% filled (VERIFIED) — fails the 80% control-coverage floor by design.",
    ),
    _f(
        name="sector_bse", version="1.0", source_table="companies",
        data_tier="H", history_start=date(2017, 11, 28), value_kind="enum",
        description="BSE macro_sector/sector/industry labels (migration 040).",
        valid_clocks=_ALL_CLOCKS, coverage=None, control_eligible=True,
        notes="Coverage UNMEASURED — measure before allowing as a control.",
    ),
]

REGISTRY: Dict[str, FeatureSpec] = {f.ref: f for f in _FEATURES}

# Controls must be this well populated before the compiler will accept them.
CONTROL_COVERAGE_FLOOR = 0.80


def parse_ref(ref: str) -> Tuple[str, Optional[str]]:
    """Split "name@version"; version None when the caller omitted it."""
    if "@" in ref:
        name, _, version = ref.partition("@")
        return name, version
    return ref, None


def get_feature(ref: str) -> Optional[FeatureSpec]:
    """Resolve an exact "name@version" reference. Unversioned refs are refused.

    Pinning is deliberate: an experiment must name the feature version it ran
    against, or reproducibility claims are unverifiable.
    """
    name, version = parse_ref(ref)
    if version is None:
        return None
    return REGISTRY.get(f"{name}@{version}")


def features_for_source(source_table: str) -> List[FeatureSpec]:
    return [f for f in _FEATURES if f.source_table == source_table]
