"""The ranked-event loader: PIT filtering, and honest drop accounting.

A study that quietly loses half its sample is a study whose result means
something other than what it claims, so every exclusion is counted.
"""
from __future__ import annotations

import csv
import gzip
from datetime import date

import pytest

from bharat_terminal.factory.backtest.events import RAW_COLUMN, load_ranked_events
from bharat_terminal.factory.backtest.signals import UnbacktestableSpec

HEADER = ["symbol", "period_end", "announced_at", "sue_composite", "sue_eps",
          "sue_revenue", "percentile", "decile", "liquidity_pass",
          "median_traded_value_cr", "study_version", "sector_bse",
          "expectation_surprise_gbm", "guidance_change", "shp_promoter_delta"]


def _snapshot(tmp_path, rows):
    p = tmp_path / "earnings_surprises.csv.gz"
    with gzip.open(p, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})
    return str(p)


def _row(symbol, day, decile="10", liq="t", **kw):
    base = {"symbol": symbol, "period_end": "2024-01-01",
            "announced_at": f"{day} 05:00:00+00", "decile": decile,
            "liquidity_pass": liq, "median_traded_value_cr": "50"}
    base.update(kw)
    return base


def test_in_sample_window_excludes_holdout_events(tmp_path):
    path = _snapshot(tmp_path, [_row("A", "2024-01-10"), _row("B", "2026-01-10")])
    kept, drops = load_ranked_events(path, "sue_composite@2.1", date(2025, 9, 13))
    assert [k["symbol"] for k in kept] == ["A"]
    assert drops["other_window"] == 1


def test_holdout_window_returns_only_sealed_events(tmp_path):
    path = _snapshot(tmp_path, [_row("A", "2024-01-10"), _row("B", "2026-01-10")])
    kept, drops = load_ranked_events(path, "sue_composite@2.1", date(2025, 9, 13),
                                     window="holdout")
    assert [k["symbol"] for k in kept] == ["B"]
    assert drops["other_window"] == 1


def test_holdout_events_rank_against_in_sample_history(tmp_path):
    """The order of ranking and windowing is the whole correctness of the OOS test.

    Rank after windowing and a holdout event's reference set is a handful of
    other holdout events, so its decile means something different from the
    in-sample deciles it is about to be compared against.
    """
    # In-sample events run up to the seal; the holdout event follows shortly
    # after, so the 365-day lookback genuinely spans the boundary.
    import datetime as _dt
    start = _dt.date(2025, 3, 1)
    rows = [_row(f"S{i}", str(start + _dt.timedelta(days=i)),
                 expectation_surprise_gbm=str(i / 200.0))
            for i in range(190)]
    rows.append(_row("HOLD", "2025-09-20", expectation_surprise_gbm="0.5"))
    kept, _ = load_ranked_events(path=_snapshot(tmp_path, rows),
                                 feature="expectation_surprise_gbm@1.0",
                                 sealed_from=date(2025, 9, 13),
                                 deciles=tuple(range(1, 11)),
                                 window="holdout")
    assert len(kept) == 1
    # 200 in-sample events are in its reference set, not the 0 other holdout ones.
    assert kept[0]["reference_n"] >= 190


def test_an_invalid_window_is_refused(tmp_path):
    path = _snapshot(tmp_path, [_row("A", "2024-01-10")])
    with pytest.raises(ValueError):
        load_ranked_events(path, "sue_composite@2.1", date(2025, 9, 13), window="both")


def test_illiquid_events_are_excluded_and_counted(tmp_path):
    path = _snapshot(tmp_path, [_row("A", "2024-01-10"),
                                _row("B", "2024-01-11", liq="f")])
    kept, drops = load_ranked_events(path, "sue_composite@2.1", date(2025, 9, 13))
    assert [k["symbol"] for k in kept] == ["A"]
    assert drops["liquidity_pass_false"] == 1


def test_sue_uses_the_precomputed_decile_and_never_reranks(tmp_path):
    """Re-ranking SUE here would stop EXP-000001 reproducing."""
    assert RAW_COLUMN["sue_composite@2.1"] is None
    path = _snapshot(tmp_path, [_row("A", "2024-01-10", decile="10"),
                                _row("B", "2024-01-11", decile="5")])
    kept, drops = load_ranked_events(path, "sue_composite@2.1", date(2025, 9, 13))
    assert [k["symbol"] for k in kept] == ["A"]        # decile 5 not selected
    assert drops["decile_not_selected"] == 1
    assert "reference_n" not in kept[0]                # nothing was re-ranked


def test_a_missing_raw_value_is_dropped_by_name(tmp_path):
    """The drop key names the column, so a sparse feature is obvious in a log."""
    path = _snapshot(tmp_path, [_row("A", "2024-01-10", expectation_surprise_gbm=""),
                                _row("B", "2024-01-11", expectation_surprise_gbm="0.5")])
    _, drops = load_ranked_events(path, "expectation_surprise_gbm@1.0", date(2025, 9, 13))
    assert drops["no_expectation_surprise_gbm"] == 1


def test_under_referenced_rows_are_dropped_not_given_a_fake_decile(tmp_path):
    """Two events cannot produce a decile, and must not pretend to."""
    path = _snapshot(tmp_path, [_row("A", "2024-01-10", expectation_surprise_gbm="0.1"),
                                _row("B", "2024-01-11", expectation_surprise_gbm="0.9")])
    kept, drops = load_ranked_events(path, "expectation_surprise_gbm@1.0", date(2025, 9, 13))
    assert kept == []
    assert drops["under_referenced_for_decile"] == 2


def test_an_unsupported_signal_is_refused_before_any_io(tmp_path):
    path = _snapshot(tmp_path, [_row("A", "2024-01-10")])
    with pytest.raises(UnbacktestableSpec):
        load_ranked_events(path, "impact_magnitude@1.0", date(2025, 9, 13))


def test_sector_and_guidance_ride_along_for_downstream_use(tmp_path):
    path = _snapshot(tmp_path, [_row("A", "2024-01-10", sector_bse="Financial Services",
                                     guidance_change="raised")])
    kept, _ = load_ranked_events(path, "sue_composite@2.1", date(2025, 9, 13))
    assert kept[0]["sector_bse"] == "Financial Services"
    assert kept[0]["guidance_change"] == "raised"
