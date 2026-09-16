"""Integrity gates — run on every snapshot, before any experiment may use it.

This project's characteristic failure is stale or subtly wrong data producing
confident output. So a snapshot is guilty until proven innocent: a FAIL blocks
all new experiments (compiler E015), and a FLAG attaches a bias marker that
follows the result into the ledger and the report.

CLOCK LAG — what was actually measured (2026-09-12, 502 matched pairs, 45 days)
------------------------------------------------------------------------------
`earnings_surprises.announced_at` was compared against the earliest independent
RESULT sighting in `news_items`:

    median 0.91h · p25 0.09h · p75 5.81h · p90 7.83h · 3.4% negative
    by upstream source: bse_xbrl median 0.20h, nse_xbrl median 6.15h

The distribution is BIMODAL because it mixes two feeds, not because of a
timezone bug. An earlier reading of coarse half-hour buckets looked like a
"+5.5h spike"; at 0.1h resolution it is a broad hump peaking at 5.6-5.9h, and
the decisive test settles it: of 502 pairs, **zero** have the stored UTC
time-of-day equal to the true IST wall-clock time-of-day, which a constant
+5.5h mislabel would produce in bulk. `announced_at` is simply a source-provided
timestamp (NSE broadcast/filing date, or the BSE index time) that lags first
dissemination.

That is still a real hazard — it is not the dissemination clock — so it is
measured and flagged, and the compiler refuses intraday rules on it. But it is
a publication lag, not a mislabel, and the gate says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

IST_UTC_OFFSET_HOURS = 5.5

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass(frozen=True)
class IntegrityResult:
    check: str
    status: str                 # pass | warn | fail
    detail: str
    value: object = None
    threshold: object = None
    flag: Optional[str] = None  # bias marker attached to downstream results

    @property
    def ok(self) -> bool:
        return self.status != FAIL

    def as_dict(self) -> Dict[str, object]:
        return {"check": self.check, "status": self.status, "detail": self.detail,
                "value": self.value, "threshold": self.threshold, "flag": self.flag}


@dataclass
class IntegritySuite:
    results: List[IntegrityResult] = field(default_factory=list)

    def add(self, r: IntegrityResult) -> "IntegritySuite":
        self.results.append(r)
        return self

    @property
    def failures(self) -> Tuple[str, ...]:
        return tuple(r.check for r in self.results if r.status == FAIL)

    @property
    def flags(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(r.flag for r in self.results if r.flag))

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "failures": list(self.failures),
            "flags": list(self.flags),
            "results": [r.as_dict() for r in self.results],
        }


def check_snapshot_age(
    created_at: datetime, now: Optional[datetime] = None, max_hours: float = 26.0
) -> IntegrityResult:
    now = now or datetime.now(timezone.utc)
    age = (now - created_at).total_seconds() / 3600.0
    status = PASS if age <= max_hours else FAIL
    return IntegrityResult(
        "snapshot_age", status,
        f"snapshot is {age:.1f}h old (limit {max_hours}h)",
        round(age, 2), max_hours,
    )


def check_source_freshness(
    table: str,
    max_date: Optional[date],
    expected_max_date: date,
    max_lag_sessions: int = 1,
    trading_dates: Optional[Sequence[date]] = None,
) -> IntegrityResult:
    """Is a source current as of the last session it should cover?

    Lag is measured in SESSIONS when a calendar is supplied, so a long weekend
    is not mistaken for a stalled feed.
    """
    check = f"freshness:{table}"
    if max_date is None:
        return IntegrityResult(check, FAIL, f"{table} has no rows / no max date", None,
                               expected_max_date.isoformat())
    if max_date >= expected_max_date:
        return IntegrityResult(check, PASS, f"{table} current to {max_date.isoformat()}",
                               max_date.isoformat(), expected_max_date.isoformat())

    if trading_dates:
        sessions = [d for d in sorted(trading_dates) if max_date < d <= expected_max_date]
        lag = len(sessions)
    else:
        lag = (expected_max_date - max_date).days

    status = PASS if lag <= max_lag_sessions else FAIL
    return IntegrityResult(
        check, status,
        f"{table} is {lag} session(s) behind {expected_max_date.isoformat()}",
        max_date.isoformat(), expected_max_date.isoformat(),
    )


def check_row_delta(
    table: str, rows: int, previous_rows: Optional[int],
    max_drop_pct: float = 5.0, max_growth_pct: float = 100.0,
) -> IntegrityResult:
    """Catch a truncated or duplicated export by comparing to the last snapshot."""
    check = f"row_delta:{table}"
    if previous_rows is None:
        return IntegrityResult(check, PASS, f"{table}: no prior snapshot to compare", rows, None)
    if previous_rows == 0:
        return IntegrityResult(check, PASS, f"{table}: prior snapshot empty", rows, None)

    delta_pct = 100.0 * (rows - previous_rows) / previous_rows
    if delta_pct < -abs(max_drop_pct):
        return IntegrityResult(check, FAIL,
                               f"{table} lost {abs(delta_pct):.1f}% of rows vs the last snapshot",
                               round(delta_pct, 2), -abs(max_drop_pct))
    if delta_pct > abs(max_growth_pct):
        return IntegrityResult(check, WARN,
                               f"{table} grew {delta_pct:.1f}% vs the last snapshot",
                               round(delta_pct, 2), abs(max_growth_pct))
    return IntegrityResult(check, PASS, f"{table} row delta {delta_pct:+.2f}%",
                           round(delta_pct, 2), abs(max_drop_pct))


def check_null_share(
    table: str, column: str, null_share: float, max_share: float = 0.20
) -> IntegrityResult:
    check = f"null_share:{table}.{column}"
    status = PASS if null_share <= max_share else WARN
    return IntegrityResult(
        check, status,
        f"{table}.{column} is {null_share:.1%} null (limit {max_share:.0%})",
        round(null_share, 4), max_share,
    )


# ── clock quality ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LagStats:
    n: int
    median: float
    p90: float
    share_within_30min: float
    share_negative: float


def clock_lag_stats(gaps_hours: Sequence[float]) -> LagStats:
    """Summarize (stored clock - first independent sighting), in hours."""
    xs = sorted(float(g) for g in gaps_hours)
    n = len(xs)
    if n == 0:
        return LagStats(0, 0.0, 0.0, 0.0, 0.0)

    def q(p: float) -> float:
        if n == 1:
            return xs[0]
        pos = p * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return LagStats(
        n=n,
        median=q(0.5),
        p90=q(0.9),
        share_within_30min=sum(1 for g in xs if abs(g) <= 0.5) / n,
        share_negative=sum(1 for g in xs if g < -0.05) / n,
    )


def check_clock_lag(
    gaps_hours: Sequence[float],
    table: str = "earnings_surprises",
    column: str = "announced_at",
    max_median_hours: float = 0.5,
) -> IntegrityResult:
    """Does this clock actually mark dissemination, or does it trail it?

    Measured 2026-09-12: median 0.91h overall — bse_xbrl 0.20h, nse_xbrl 6.15h.
    A clock that trails dissemination cannot time an intraday entry, so when the
    median lag exceeds the threshold we raise `clock_lags_dissemination`. The
    compiler refuses intraday rules on such a clock regardless (E003).
    """
    check = f"clock_lag:{table}.{column}"
    st = clock_lag_stats(gaps_hours)
    if st.n == 0:
        return IntegrityResult(check, WARN, "no overlapping sightings to compare", 0,
                               max_median_hours)

    detail = (f"median lag {st.median:.2f}h, p90 {st.p90:.2f}h, "
              f"{st.share_within_30min:.0%} within 30min, {st.share_negative:.1%} negative "
              f"(n={st.n})")
    if st.median > max_median_hours:
        return IntegrityResult(check, WARN, detail, round(st.median, 2), max_median_hours,
                               flag="clock_lags_dissemination")
    return IntegrityResult(check, PASS, detail, round(st.median, 2), max_median_hours)


def check_constant_offset(
    gaps_hours: Sequence[float],
    table: str = "earnings_surprises",
    column: str = "announced_at",
    offset_hours: float = IST_UTC_OFFSET_HOURS,
    bin_hours: float = 0.1,
    min_share: float = 0.05,
    min_concentration: float = 3.0,
) -> IntegrityResult:
    """Detect a CONSTANT clock offset, e.g. IST wall-clock stored as UTC.

    A timezone mislabel is a delta function: every affected row sits at exactly
    the offset. A publication lag is a smooth hump. So a mere pile-up near the
    offset is not enough — the offset bin must also dominate its neighbours by
    `min_concentration`. Without that test, the NSE XBRL lag (which peaks at
    5.6-5.9h) reads as a timezone bug. It is not one.
    """
    check = f"constant_offset:{table}.{column}"
    n = len(gaps_hours)
    if n == 0:
        return IntegrityResult(check, WARN, "no overlapping sightings to compare", 0, min_share)

    half = bin_hours / 2.0

    def count_near(centre: float) -> int:
        return sum(1 for g in gaps_hours if abs(float(g) - centre) <= half)

    at_offset = count_near(offset_hours)
    neighbours = [count_near(offset_hours + k * bin_hours) for k in (-3, -2, -1, 1, 2, 3)]
    neighbour_mean = (sum(neighbours) / len(neighbours)) if neighbours else 0.0
    share = at_offset / n
    concentration = (at_offset / neighbour_mean) if neighbour_mean > 0 else float(at_offset)

    if share >= min_share and concentration >= min_concentration:
        return IntegrityResult(
            check, WARN,
            f"{at_offset}/{n} ({share:.1%}) sit at exactly +{offset_hours}h, "
            f"{concentration:.1f}x the neighbouring bins — constant offset, not a lag",
            round(share, 4), min_share, flag="clock_constant_offset",
        )
    return IntegrityResult(
        check, PASS,
        f"no constant offset at +{offset_hours}h: {at_offset}/{n} ({share:.1%}), "
        f"{concentration:.1f}x neighbours (needs {min_concentration}x)",
        round(share, 4), min_share,
    )


def check_series_continuity(
    symbol: str,
    series_dates: Sequence[date],
    market_dates: Sequence[date],
    max_gap_sessions: int = 5,
    min_coverage: float = 0.98,
) -> IntegrityResult:
    """Does a series actually trade on every session it should?

    Found the hard way on 2026-09-12: NIFTYBEES, JUNIORBEES and BANKBEES each
    stop at 2025-05-08 and resume 2026-06-04 — a 392-day hole in `price_points`.
    A benchmark with a hole does not fail loudly; it silently prices a 20-day
    window against quotes a year apart, and if it is also used as the trading
    calendar it pushes entry dates forward by months.

    Coverage is measured against the market's own session set, so a genuinely
    young series (a recent listing) is judged only over its own lifetime.
    """
    check = f"series_continuity:{symbol}"
    have = sorted(set(series_dates))
    if not have:
        return IntegrityResult(check, FAIL, f"{symbol} has no bars", 0, min_coverage,
                               flag="benchmark_gap")

    lo, hi = have[0], have[-1]
    expected = [d for d in sorted(set(market_dates)) if lo <= d <= hi]
    coverage = len(have) / len(expected) if expected else 1.0

    worst_gap = 0
    worst_at: Optional[date] = None
    present = set(have)
    run = 0
    for d in expected:
        if d in present:
            run = 0
        else:
            run += 1
            if run > worst_gap:
                worst_gap, worst_at = run, d

    detail = (f"{symbol}: {len(have)}/{len(expected)} sessions ({coverage:.1%}) "
              f"between {lo.isoformat()} and {hi.isoformat()}; "
              f"largest gap {worst_gap} session(s)"
              + (f" around {worst_at.isoformat()}" if worst_at else ""))

    if coverage < min_coverage or worst_gap > max_gap_sessions:
        return IntegrityResult(check, FAIL, detail, round(coverage, 4), min_coverage,
                               flag="benchmark_gap")
    return IntegrityResult(check, PASS, detail, round(coverage, 4), min_coverage)


def check_universe_survivorship(
    delisted_symbols: int, total_symbols: int, min_expected_share: float = 0.02
) -> IntegrityResult:
    """A universe where almost nothing ever delists is a survivor panel.

    Verified today: 25 of 3,923 symbols (0.6%) ever stop pricing. This never
    passes until a point-in-time universe exists; it exists to keep the bias
    visible in every snapshot rather than remembered in a doc.
    """
    share = (delisted_symbols / total_symbols) if total_symbols else 0.0
    status = PASS if share >= min_expected_share else WARN
    return IntegrityResult(
        "universe_survivorship", status,
        f"only {delisted_symbols}/{total_symbols} ({share:.1%}) symbols ever stop pricing",
        round(share, 4), min_expected_share,
        flag=None if status == PASS else "survivorship",
    )


def run_integrity_suite(
    *,
    created_at: datetime,
    now: Optional[datetime] = None,
    freshness: Sequence[Tuple[str, Optional[date], date]] = (),
    trading_dates: Optional[Sequence[date]] = None,
    row_counts: Sequence[Tuple[str, int, Optional[int]]] = (),
    null_shares: Sequence[Tuple[str, str, float]] = (),
    timestamp_gaps_hours: Optional[Sequence[float]] = None,
    survivorship: Optional[Tuple[int, int]] = None,
    series_continuity: Sequence[Tuple[str, Sequence[date], Sequence[date]]] = (),
) -> IntegritySuite:
    """Run every gate over one snapshot and collect the verdicts."""
    suite = IntegritySuite()
    suite.add(check_snapshot_age(created_at, now))
    for table, max_date, expected in freshness:
        suite.add(check_source_freshness(table, max_date, expected, trading_dates=trading_dates))
    for table, rows, prev in row_counts:
        suite.add(check_row_delta(table, rows, prev))
    for table, column, share in null_shares:
        suite.add(check_null_share(table, column, share))
    if timestamp_gaps_hours is not None:
        suite.add(check_clock_lag(timestamp_gaps_hours))
        suite.add(check_constant_offset(timestamp_gaps_hours))
    for symbol, series_dates, market_dates in series_continuity:
        suite.add(check_series_continuity(symbol, series_dates, market_dates))
    if survivorship is not None:
        suite.add(check_universe_survivorship(*survivorship))
    return suite
