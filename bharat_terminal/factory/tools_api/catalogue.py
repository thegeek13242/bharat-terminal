"""The adversarial battery catalogue, `default_v1`.

Transcribed from RESEARCH_FACTORY_SPEC_v2 section 7.5. Fifteen tests, not the
fourteen an earlier plan draft claimed -- the spec is the source.

This is code, not a table: the Adversary may only request attacks that exist
here, so the catalogue bounds what an agent can ask for. A test whose
`critical` flag is set can uphold a veto; the others are reported and never
block on their own. That distinction is deterministic and lives here rather
than in a prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class BatteryTest:
    test_id: str
    what: str
    critical: bool
    kill_condition: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "test_id": self.test_id,
            "what": self.what,
            "critical": self.critical,
            "kill_condition": self.kill_condition,
        }


BATTERY: Tuple[BatteryTest, ...] = (
    BatteryTest("latency_plus1", "entry one session later (or +30 min for Tier F)",
                True, "net effect sign flips or falls below 50%"),
    BatteryTest("cost_x2", "costs doubled",
                True, "net <= 0"),
    BatteryTest("drop_top5", "remove the 5 symbols contributing most",
                True, "net <= 0 or t < 2"),
    BatteryTest("year_loo", "leave-one-year-out",
                True, "any single year's removal flips the sign"),
    BatteryTest("placebo_dates", "pseudo-events on the same symbols",
                True, "real effect <= 97.5th percentile of placebo"),
    BatteryTest("ca_exclusion", "stricter corporate-action window, +/-5 sessions",
                True, "net effect falls below 50%"),
    BatteryTest("clock_swap", "exchange_first_seen vs announcement_date where both exist",
                True, "sign flip"),
    BatteryTest("param_neighbourhood", "+/-1 grid step on each swept parameter",
                True, "median neighbour below 50% of the point estimate"),
    BatteryTest("season_split", "results season vs off-season",
                False, "report only; strategy is tagged"),
    BatteryTest("sector_loo", "leave-one-sector-out (BSE sector map where covered)",
                False, "report only"),
    BatteryTest("liquidity_tiers", "effect by tercile",
                False, "report only; feeds the capacity estimate"),
    BatteryTest("benchmark_swap", "NIFTYBEES / JUNIORBEES / universe median",
                False, "report only"),
    BatteryTest("simpler_explanation", "same test with a strictly smaller feature set",
                False, "if >=80% of the effect remains, a simpler child is created "
                       "and the parent is RETIRED on the child's survival"),
    BatteryTest("survivorship_stress",
                "restrict to symbols listed >=3 years before the event whose price "
                "history continues to the snapshot",
                False, "report plus a haircut note"),
    BatteryTest("market_state_excl", "ASM/GSM exclusion (Tier F coverage only)",
                False, "report only"),
)

BY_ID: Dict[str, BatteryTest] = {t.test_id: t for t in BATTERY}
CRITICAL_IDS: Tuple[str, ...] = tuple(t.test_id for t in BATTERY if t.critical)


def battery_catalog() -> Dict[str, Any]:
    """Payload for get_battery_catalog. No database involved."""
    return {
        "battery_version": "default_v1",
        "n_tests": len(BATTERY),
        "n_critical": len(CRITICAL_IDS),
        "critical_ids": list(CRITICAL_IDS),
        "tests": [t.as_dict() for t in BATTERY],
        "rule": ("A veto is upheld only when a CRITICAL test fails. That check is "
                 "deterministic; an Adversary memo is advisory."),
    }


def is_known(test_id: str) -> bool:
    """Used by submit_attack_plan later: an agent cannot invent an attack."""
    return test_id in BY_ID
