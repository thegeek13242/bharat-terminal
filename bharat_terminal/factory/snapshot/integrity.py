"""Integrity gates — run on every snapshot, before any experiment may use it.

This project's characteristic failure is stale or subtly wrong data producing
confident output. So the snapshot is guilty until proven innocent: a FAIL
blocks all new experiments (compiler E015), and a FLAG attaches a bias marker
that follows the result into the ledger and the report.

The timestamp check is the live one. `earnings_surprises.announced_at` carries
IST wall-clock stored as UTC on a subset of rows — verified 2026-09-11 as a
sharp +5.5h spike in 99 of 503 gaps measured against the first news sighting
of the same result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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


def check_timestamp_ist_as_utc(
    gaps_hours: Sequence[float],
    table: str = "earnings_surprises",
    column: str = "announced_at",
    tolerance_hours: float = 0.25,
    min_share: float = 0.05,
) -> IntegrityResult:
    """Detect IST wall-clock stored as UTC.

    `gaps_hours` are (stored timestamp - first independent sighting) in hours.
    A genuine publishing lag spreads out; a timezone mislabel piles up at
    exactly +5.5h. When the spike carries at least `min_share` of the sample we
    raise the `ts_ist_as_utc` flag.

    This is a FLAG, not a FAIL: the offset makes stored times LATER than truth,
    so next-session entries lag rather than peek. The compiler separately
    refuses intraday rules on this clock (E003).
    """
    check = f"timestamp_offset:{table}.{column}"
    n = len(gaps_hours)
    if n == 0:
        return IntegrityResult(check, WARN, "no overlapping sightings to compare", 0, min_share)

    spike = sum(1 for g in gaps_hours
                if abs(g - IST_UTC_OFFSET_HOURS) <= tolerance_hours)
    share = spike / n
    if share >= min_share:
        return IntegrityResult(
            check, WARN,
            f"{spike}/{n} ({share:.1%}) of gaps sit at +{IST_UTC_OFFSET_HOURS}h — "
            f"IST stored as UTC on part of {table}.{column}",
            round(share, 4), min_share, flag="ts_ist_as_utc",
        )
    return IntegrityResult(
        check, PASS,
        f"no timezone spike: {spike}/{n} ({share:.1%}) near +{IST_UTC_OFFSET_HOURS}h",
        round(share, 4), min_share,
    )


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
        suite.add(check_timestamp_ist_as_utc(timestamp_gaps_hours))
    if survivorship is not None:
        suite.add(check_universe_survivorship(*survivorship))
    return suite
