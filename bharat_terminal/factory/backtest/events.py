"""Load snapshot events ranked on whichever signal the spec asks for.

WHY NOT JUST EXTEND THE INCUMBENT LOADER
`scripts/factory_exp_000001.load_events` is the frozen recipe that reproduces
EXP-000001, and it filters on the snapshot's precomputed `decile` column. That
column is composite SUE, ranked by the production pipeline. If this module
re-ranked SUE itself the numbers would drift and EXP-000001 would stop
reproducing, so for SUE the precomputed decile is used verbatim and only NEW
signals are ranked here.

RANKING IS POINT-IN-TIME
New signals are exported raw and ranked by `ranking.trailing_deciles`: each
event against events strictly before it, inside a rolling window. See that
module for why this is not done in SQL.
"""
from __future__ import annotations

import csv
import gzip
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from bharat_terminal.factory.backtest.ranking import trailing_deciles
from bharat_terminal.factory.backtest.signals import check_signal_supported

# feature ref -> the snapshot column holding its RAW value. A feature whose
# decile is precomputed upstream maps to None and is never re-ranked here.
RAW_COLUMN: Dict[str, Optional[str]] = {
    "sue_composite@2.1": None,                      # precomputed `decile`
    "expectation_surprise_gbm@1.0": "expectation_surprise_gbm",
    # Mapped but not in SNAPSHOT_RANKED_FEATURES -- see signals.py for why.
    "shp_promoter_delta@1.0": "shp_promoter_delta",
}

IST_OFFSET_SECONDS = 5.5 * 3600


def _rows(path: str):
    with gzip.open(path, "rt", newline="") as fh:
        yield from csv.DictReader(fh)


def _ist_date(raw: str) -> Optional[date]:
    """The IST calendar date of an announcement - the clock the compiler allows."""
    try:
        ts = datetime.fromisoformat((raw or "").replace(" ", "T"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(
        ts.astimezone(timezone.utc).timestamp() + IST_OFFSET_SECONDS,
        tz=timezone.utc).date()


IN_SAMPLE = "in_sample"
HOLDOUT = "holdout"


def load_ranked_events(
    path: str,
    feature: str,
    sealed_from: date,
    deciles: Tuple[int, ...] = (1, 10),
    window: str = IN_SAMPLE,
) -> Tuple[List[dict], Dict[str, int]]:
    """Events in one window, ranked on `feature`.

    Returns (events, drops). Every exclusion is counted rather than silently
    dropped, because a study that quietly loses half its sample is a study
    whose result means something other than what it claims.

    RANKING HAPPENS BEFORE WINDOWING, AND THAT ORDER IS LOAD-BEARING.
    A holdout event must rank against ITS OWN PAST -- which includes the whole
    in-sample period -- not against the handful of other holdout events. Rank
    after windowing and the holdout's reference set is both tiny and drawn from
    the wrong distribution, so its deciles would mean something different from
    the in-sample deciles they are about to be compared against. That would not
    error; it would quietly produce an out-of-sample test of a different signal.
    """
    if window not in (IN_SAMPLE, HOLDOUT):
        raise ValueError(f"window must be {IN_SAMPLE!r} or {HOLDOUT!r}, got {window!r}")
    check_signal_supported(feature)
    raw_col = RAW_COLUMN[feature]
    drops: Dict[str, int] = defaultdict(int)

    staged: List[dict] = []
    for r in _rows(path):
        ev = _ist_date(r.get("announced_at") or "")
        if ev is None:
            drops["unparseable_announced_at"] += 1
            continue
        if (r.get("liquidity_pass") or "").lower() not in ("t", "true"):
            drops["liquidity_pass_false"] += 1
            continue
        try:
            mtv = float(r.get("median_traded_value_cr") or 0.0)
        except ValueError:
            mtv = 0.0

        row = {"symbol": r["symbol"], "event_date": ev, "mtv": mtv,
               "window": HOLDOUT if ev >= sealed_from else IN_SAMPLE,
               "sector_bse": (r.get("sector_bse") or "") or None,
               "guidance_change": (r.get("guidance_change") or "") or None}

        if raw_col is None:
            dec = r.get("decile")
            if dec in (None, ""):
                drops["no_precomputed_decile"] += 1
                continue
            row["decile"] = int(dec)
            row["value"] = None
        else:
            v = r.get(raw_col)
            if v in (None, ""):
                drops[f"no_{raw_col}"] += 1
                continue
            try:
                row["value"] = float(v)
            except ValueError:
                drops[f"unparseable_{raw_col}"] += 1
                continue
        staged.append(row)

    if raw_col is not None:
        ranks = trailing_deciles([(r["event_date"], r["value"]) for r in staged])
        ranked: List[dict] = []
        for row, rk in zip(staged, ranks):
            if rk.decile is None:
                drops["under_referenced_for_decile"] += 1
                continue
            row["decile"] = rk.decile
            row["reference_n"] = rk.reference_n
            ranked.append(row)
        staged = ranked

    in_window = [r for r in staged if r["window"] == window]
    drops["other_window"] = len(staged) - len(in_window)

    kept = [r for r in in_window if r["decile"] in deciles]
    drops["decile_not_selected"] = len(in_window) - len(kept)
    return kept, dict(drops)
