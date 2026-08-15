"""Shared fixtures for the papertrade unit suite.

FakePool stands in for asyncpg.Pool and covers exactly the SQL that
engine.py / rules.py issue, dispatching on distinctive SQL fragments.
Unhandled SQL raises AssertionError — a new query in production code can
never silently no-op in tests.

No test in this package may touch the network, Redis, Kafka, or a real
database. scripts/test.sh additionally points every infra env var at an
unresolvable host so an accidental escape fails loudly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bharat_terminal.analysis.market_hours import IST


def ist_dt(y, mo, d, h=10, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


class FakePool:
    def __init__(self):
        # knobs a test sets up
        self.gross = 0.0                  # _GROSS_SQL result
        self.prev_close = {}              # symbol -> prev close (gap guard)
        self.vol60 = {}                   # symbol -> vol60 (trailing stop)
        self.vol_runup = {}               # symbol -> (vol60, ret5_pre)
        self.sue_abs = {}                 # symbol -> |sue| (materiality feat)
        self.pead_sue = {}                # symbol -> sue_composite
        self.sue_threshold = None         # P90 threshold (None = window empty)
        self.live_pos = set()             # {(symbol, strategy)} concentration
        self.known_symbols = None         # None = every symbol in companies
        self.pending = []                 # PENDING row dicts
        self.open_rows = []               # OPEN row dicts
        self.existing_keys = set()        # {(impact_id, symbol, strategy)}
        # call logs a test asserts on
        self.opened = []
        self.rejected = []
        self.queued = []
        self.fills = []                   # (row_id, qty, entry, prov_json, vol60)
        self.pending_rejects = []         # (row_id, reason, prov_json)
        self.closed = []                  # (row_id, price, reason)
        self.sue_threshold_queries = 0
        self.executed = []                # every execute() call, raw
        self._next_id = 1000

    def _id(self):
        self._next_id += 1
        return self._next_id

    # ── asyncpg.Pool surface ────────────────────────────────────────────

    async def fetchval(self, sql, *args):
        if "SELECT close FROM price_points" in sql:
            return self.prev_close.get(args[0])
        if "sum(abs(qty)" in sql and "strategy = $1" in sql:
            return 0.0                                    # strategy_notional
        if "sum(abs(qty)" in sql and "status = 'OPEN'" in sql:
            return self.gross                             # _GROSS_SQL
        raise AssertionError(f"FakePool.fetchval unhandled: {sql[:90]!r}")

    async def fetchrow(self, sql, *args):
        if "INSERT INTO paper_trades" in sql:
            if "'REJECTED'" in sql:                       # _REJECT_SQL
                key = (args[1], args[3], args[0])
                if key in self.existing_keys:
                    return None
                self.existing_keys.add(key)
                rid = self._id()
                self.rejected.append({
                    "id": rid, "strategy": args[0], "impact_id": args[1],
                    "symbol": args[3], "side": args[4], "instrument": args[5],
                    "reason": args[6], "prov": args[7]})
                return {"id": rid}
            if "'PENDING'" in sql:                        # _QUEUE_SQL
                key = (args[1], args[3], args[0])
                if key in self.existing_keys:
                    return None
                self.existing_keys.add(key)
                rid = self._id()
                self.queued.append({"id": rid, "strategy": args[0],
                                    "symbol": args[3], "prov": args[6]})
                return {"id": rid}
            if "'OPEN'" in sql:                           # _OPEN_SQL
                key = (args[1], args[3], args[0])
                if key in self.existing_keys:
                    return None
                self.existing_keys.add(key)
                rid = self._id()
                self.opened.append({
                    "id": rid, "strategy": args[0], "impact_id": args[1],
                    "symbol": args[3], "side": args[4], "instrument": args[5],
                    "qty": args[6], "entry": args[7], "prov": args[8],
                    "vol60": args[9]})
                return {"id": rid}
        if "SET status = 'CLOSED'" in sql:                # _CLOSE_SQL
            self.closed.append((args[0], args[1], args[2]))
            return {"pnl": 0.0}
        if "AS vol60" in sql and "stddev_samp" in sql and "OVER w" not in sql:
            return {"vol60": self.vol60.get(args[0])}     # _VOL60_SQL
        if "ret5_pre" in sql:                             # _VOL_RUNUP_SQL
            v = self.vol_runup.get(args[0])
            return None if v is None else {"vol60": v[0], "ret5_pre": v[1]}
        if "AS sue_abs" in sql:                           # _SUE_ABS_SQL
            v = self.sue_abs.get(args[0])
            return None if v is None else {"sue_abs": v}
        if "SELECT sue_composite" in sql:                 # _PEAD_SUE_SQL
            v = self.pead_sue.get(args[0])
            return None if v is None else {"sue_composite": v}
        if "percentile_cont" in sql:                      # _SUE_THRESHOLD_SQL
            self.sue_threshold_queries += 1
            return {"thr": self.sue_threshold}
        if "FROM companies" in sql:                       # _SYMBOL_KNOWN_SQL
            known = (self.known_symbols is None
                     or args[0] in self.known_symbols)
            return {"ok": 1} if known else None
        if "status IN ('PENDING', 'OPEN')" in sql:        # _LIVE_POS_SQL
            return {"ok": 1} if (args[0], args[1]) in self.live_pos else None
        if "paper_equity_snapshots" in sql:               # _SNAPSHOT_SQL
            return None
        raise AssertionError(f"FakePool.fetchrow unhandled: {sql[:90]!r}")

    async def fetch(self, sql, *args):
        if "status = 'PENDING'" in sql:
            return [dict(r) for r in self.pending]        # copy: engine mutates
        if "DISTINCT symbol" in sql:
            return [{"symbol": r["symbol"]} for r in self.open_rows]
        if "status = 'OPEN'" in sql:
            return [dict(r) for r in self.open_rows]
        raise AssertionError(f"FakePool.fetch unhandled: {sql[:90]!r}")

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "SET qty = $2" in sql:                         # _FILL_SQL
            self.fills.append(args)
            self.pending = [r for r in self.pending if r["id"] != args[0]]
            return "UPDATE 1"
        if "SET status = 'REJECTED'" in sql:              # _PENDING_REJECT_SQL
            self.pending_rejects.append(args)
            self.pending = [r for r in self.pending if r["id"] != args[0]]
            return "UPDATE 1"
        if "SET mtm_price" in sql:                        # _MARK_SQL
            return "UPDATE 1"
        if "provenance = provenance ||" in sql:           # annotate_pending
            return "UPDATE 1"
        raise AssertionError(f"FakePool.execute unhandled: {sql[:90]!r}")


@pytest.fixture
def pool():
    return FakePool()


@pytest.fixture
def engine(pool):
    from bharat_terminal.papertrade.engine import PaperEngine
    return PaperEngine(pool)


@pytest.fixture
def make_intent():
    from bharat_terminal.papertrade.rules import SignalIntent

    def _make(**kw):
        defaults = dict(strategy="headwind_short", symbol="TEST", side="SHORT",
                        instrument="CASH", impact_id="imp-1", news_id="n-1",
                        provenance={})
        defaults.update(kw)
        return SignalIntent(**defaults)
    return _make


@pytest.fixture
def make_report():
    """ImpactReport factory with a single hop-0 company impact."""
    from bharat_terminal.types import CompanyImpact, ImpactReport, NewsItem

    def _make(*, source="NSE_FILINGS", relevant=True, age_s=60.0,
              sentiment="negative", magnitude=4, hop=0, symbol="TEST",
              headline="Unaudited Financial Results for the quarter",
              confidence=0.8, time_horizon="short_term"):
        now = datetime.now(timezone.utc)
        ni = NewsItem(source=source,
                      timestamp_utc=now - timedelta(seconds=age_s),
                      headline=headline, url="https://fixture.invalid/f",
                      ingest_latency_ms=0.0)
        ci = CompanyImpact(symbol=symbol, company_name="Test Co",
                           sentiment=sentiment, magnitude=magnitude,
                           time_horizon=time_horizon, explanation="fixture",
                           hop_distance=hop)
        return ImpactReport(news_id=ni.id, news_item=ni, relevant=relevant,
                            confidence=confidence, company_impacts=[ci],
                            processing_latency_ms=1.0, created_at=now)
    return _make


@pytest.fixture
def fake_lots(monkeypatch):
    """Patch the Angel One scrip-master lot lookup with a fixed table."""
    table = {}

    async def _get_fno_lots():
        return dict(table)

    from bharat_terminal.papertrade import marks
    monkeypatch.setattr(marks, "get_fno_lots", _get_fno_lots)
    return table


def open_row(*, id=1, strategy="headwind_short", symbol="TEST", side="SHORT",
             instrument="CASH", qty=100, entry_price=100.0, entry_at=None,
             trail_extreme=None, entry_vol60=None):
    """A paper_trades OPEN row as _exit_reason / run_exits sees it."""
    return {"id": id, "strategy": strategy, "symbol": symbol, "side": side,
            "instrument": instrument, "qty": qty, "entry_price": entry_price,
            "entry_at": entry_at or ist_dt(2026, 7, 30, 10, 0),
            "trail_extreme": trail_extreme, "entry_vol60": entry_vol60}


def pending_row(*, id=1, strategy="headwind_short", symbol="TEST",
                side="SHORT", instrument="CASH", created_at=None):
    return {"id": id, "strategy": strategy, "symbol": symbol, "side": side,
            "instrument": instrument,
            "created_at": created_at or datetime.now(timezone.utc)}
