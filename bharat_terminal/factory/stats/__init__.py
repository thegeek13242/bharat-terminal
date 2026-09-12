"""Deterministic statistics. No LLM ever computes or adjusts a number here."""
from __future__ import annotations

from bharat_terminal.factory.stats.bootstrap import block_bootstrap_ci
from bharat_terminal.factory.stats.cohort import CohortStats, cohort_stats, newey_west_tstat
from bharat_terminal.factory.stats.dsr import deflated_sharpe
from bharat_terminal.factory.stats.fdr import LordPP, benjamini_hochberg
from bharat_terminal.factory.stats.placebo import placebo_percentile

__all__ = [
    "CohortStats",
    "LordPP",
    "benjamini_hochberg",
    "block_bootstrap_ci",
    "cohort_stats",
    "deflated_sharpe",
    "newey_west_tstat",
    "placebo_percentile",
]
