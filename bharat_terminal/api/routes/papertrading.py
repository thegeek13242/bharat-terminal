"""Paper-trading dashboard API — INTERNAL-ONLY read views (Phase C pilot).

Read-only endpoints over paper_trades / paper_equity_snapshots (migration
030). This is simulated-performance data: under the RA-compliance posture
(Lane A) performance figures must never reach customer surfaces, so the
whole router carries require_internal_user (INTERNAL_RESEARCH_EMAILS
allowlist) as a router-level dependency — the same gate as the signal
reports — in addition to the gate applied at include time in main.py.
Belt and braces: mounting this file without the gate must still 403.

Design rules encoded here:
- Empty tables are a normal state (the dashboard renders before the first
  trade ever fires): return zeros/nulls with HTTP 200, never 5xx.
- Honesty over polish: Sharpe is null below 5 daily equity points rather
  than a number annualized out of noise; profit_factor is null when there
  are no losing trades yet rather than infinity; an unmarked OPEN position
  contributes null unrealized P&L, not a fabricated 0.
- Aggregation happens in SQL; Python only derives ratios. No pandas.
"""
import logging
import math
import os
import statistics

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.auth import require_internal_user
from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/papertrading",
    tags=["papertrading"],
    dependencies=[Depends(require_internal_user)],
)

# Simulated starting capital for equity/exposure math. Env-overridable so the
# pilot can be re-based without a code change; must match the executor's value.
PAPER_CAPITAL = float(os.getenv("PAPER_CAPITAL", "1000000"))

_STATUSES = ("OPEN", "CLOSED", "REJECTED")

# Mark-to-market P&L of an OPEN row. A NULL entry_price or mtm_price
# propagates to NULL (and SUM skips it): a position that has never been
# marked contributes nothing rather than a silently-invented 0.
_UNREALIZED_SQL = """
    CASE WHEN side = 'LONG'
         THEN (mtm_price - entry_price) * qty
         ELSE (entry_price - mtm_price) * qty
    END
"""

# Shared aggregate set for the totals row and the per-strategy breakdown so
# the two can never drift apart.
_STATS_SQL = f"""
    count(*) FILTER (WHERE status = 'OPEN') AS n_open,
    count(*) FILTER (WHERE status = 'CLOSED') AS n_closed,
    count(*) FILTER (WHERE status = 'REJECTED') AS n_rejected,
    coalesce(sum(pnl) FILTER (WHERE status = 'CLOSED'), 0)::float8
        AS realized_pnl,
    coalesce(sum({_UNREALIZED_SQL}) FILTER (WHERE status = 'OPEN'), 0)::float8
        AS unrealized_pnl,
    coalesce(sum(qty * coalesce(mtm_price, entry_price))
             FILTER (WHERE status = 'OPEN'), 0)::float8 AS gross_exposure,
    count(*) FILTER (WHERE status = 'CLOSED' AND pnl > 0) AS n_wins,
    count(*) FILTER (WHERE status = 'CLOSED' AND pnl IS NOT NULL)
        AS n_evaluated,
    sum(pnl) FILTER (WHERE status = 'CLOSED' AND pnl > 0)::float8 AS win_sum,
    sum(pnl) FILTER (WHERE status = 'CLOSED' AND pnl < 0)::float8 AS loss_sum,
    avg(pnl) FILTER (WHERE status = 'CLOSED' AND pnl > 0)::float8 AS avg_win,
    avg(pnl) FILTER (WHERE status = 'CLOSED' AND pnl < 0)::float8 AS avg_loss
"""

_TRADE_COLUMNS = """
    id, created_at, strategy, impact_id, news_id, symbol, side, instrument,
    qty, entry_price, entry_at, exit_price, exit_at, exit_reason, status,
    mtm_price, mtm_at, pnl, provenance
"""

_TS_KEYS = ("created_at", "entry_at", "exit_at", "mtm_at")


def _rnd(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def _row_out(mapping) -> dict:
    """Row dict with timestamps ISO-serialized (None stays None)."""
    d = dict(mapping)
    for k in _TS_KEYS:
        if k in d and d[k] is not None:
            d[k] = d[k].isoformat()
    return d


def _closed_stats(r) -> dict:
    """Hit-rate / avg win-loss / profit factor from a _STATS_SQL row.

    profit_factor is null (not infinity) with zero losing trades, and null
    with zero closed trades — the dashboard must show "no data yet", never
    a fake-perfect number.
    """
    n_eval = r["n_evaluated"] or 0
    hit_rate = (r["n_wins"] / n_eval) if n_eval else None
    win_sum = float(r["win_sum"]) if r["win_sum"] is not None else 0.0
    loss_abs = abs(float(r["loss_sum"])) if r["loss_sum"] is not None else 0.0
    profit_factor = (win_sum / loss_abs) if loss_abs > 0 else None
    return {
        "hit_rate": _rnd(hit_rate, 4),
        "avg_win": _rnd(r["avg_win"]),
        "avg_loss": _rnd(r["avg_loss"]),
        "profit_factor": _rnd(profit_factor, 3),
    }


@router.get("/positions")
async def positions(
    status: str | None = Query(
        default=None,
        description="OPEN|CLOSED|REJECTED|all; default = all except REJECTED",
    ),
    limit: int = Query(default=500, ge=1, le=500),
):
    """Blotter: newest first. OPEN rows carry live unrealized P&L + age.

    REJECTED rows are excluded by default (they are gate-audit records, not
    positions) — ask for status=REJECTED or status=all to see them.
    """
    where = "WHERE status != 'REJECTED'"
    params: dict = {"lim": limit}
    if status is not None:
        s = status.strip().upper()
        if s == "ALL":
            where = ""
        elif s in _STATUSES:
            where = "WHERE status = :status"
            params["status"] = s
        else:
            raise HTTPException(
                400, "status must be one of OPEN, CLOSED, REJECTED, all")

    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(text(f"""
            SELECT {_TRADE_COLUMNS},
                   CASE WHEN status = 'OPEN'
                        THEN ({_UNREALIZED_SQL})::float8
                   END AS unrealized_pnl,
                   CASE WHEN status = 'OPEN'
                        THEN EXTRACT(EPOCH FROM
                             (now() - coalesce(entry_at, created_at)))::float8
                   END AS age_seconds
            FROM paper_trades
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT :lim
        """), params)).mappings().all()

    return {
        "items": [_row_out(r) for r in rows],
        "count": len(rows),
        "status_filter": (status or "default_no_rejected"),
    }


@router.get("/summary")
async def summary():
    """Portfolio scoreboard: totals, hit stats, per-strategy breakdown,
    equity curve (<=500 points), max drawdown, Sharpe.

    Equity = PAPER_CAPITAL + total P&L (the snapshots store their own equity
    figure for the curve; totals here are recomputed live from trades so the
    two are independently checkable).
    """
    factory = get_session_factory()
    async with factory() as session:
        t = (await session.execute(text(f"""
            SELECT {_STATS_SQL} FROM paper_trades
        """))).mappings().one()

        strat_rows = (await session.execute(text(f"""
            SELECT strategy, {_STATS_SQL}
            FROM paper_trades
            GROUP BY strategy
            ORDER BY strategy
        """))).mappings().all()

        # Downsample in SQL: stride = ceil(total/499), always keep the last
        # point, so the curve is <= 500 points no matter how many marks exist.
        curve_rows = (await session.execute(text("""
            SELECT ts, equity FROM (
                SELECT ts, equity,
                       row_number() OVER (ORDER BY ts, id) AS rn,
                       count(*) OVER () AS total
                FROM paper_equity_snapshots
            ) s
            WHERE (rn - 1) % GREATEST(1, (total + 498) / 499) = 0
               OR rn = total
            ORDER BY ts
        """))).mappings().all()

        # Max drawdown on the FULL snapshot series (not the downsampled one —
        # downsampling can hide the trough).
        mdd = (await session.execute(text("""
            SELECT max((peak - equity) / NULLIF(peak, 0) * 100)::float8
            FROM (
                SELECT equity,
                       max(equity) OVER (
                           ORDER BY ts, id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS peak
                FROM paper_equity_snapshots
            ) p
        """))).scalar_one()

        # Last snapshot of each IST market day -> daily equity series.
        daily_equity = (await session.execute(text("""
            SELECT equity FROM (
                SELECT DISTINCT ON (((ts AT TIME ZONE 'Asia/Kolkata')::date))
                       (ts AT TIME ZONE 'Asia/Kolkata')::date AS day, equity
                FROM paper_equity_snapshots
                ORDER BY (ts AT TIME ZONE 'Asia/Kolkata')::date,
                         ts DESC, id DESC
            ) d ORDER BY day
        """))).scalars().all()

    realized = float(t["realized_pnl"])
    unrealized = float(t["unrealized_pnl"])
    total_pnl = realized + unrealized

    # Sharpe from daily returns, annualized sqrt(252). Below 5 daily points
    # (or flat/degenerate series) the honest answer is null, not a number.
    sharpe = None
    if len(daily_equity) >= 5:
        rets = [
            cur / prev - 1.0
            for prev, cur in zip(daily_equity, daily_equity[1:])
            if prev and prev > 0
        ]
        if len(rets) >= 2:
            sd = statistics.stdev(rets)
            if sd > 0:
                sharpe = statistics.mean(rets) / sd * math.sqrt(252)

    per_strategy = []
    for r in strat_rows:
        entry = {
            "strategy": r["strategy"],
            "n_open": r["n_open"],
            "n_closed": r["n_closed"],
            "n_rejected": r["n_rejected"],
            "realized_pnl": _rnd(r["realized_pnl"]),
            "unrealized_pnl": _rnd(r["unrealized_pnl"]),
            "total_pnl": _rnd(
                float(r["realized_pnl"]) + float(r["unrealized_pnl"])),
            "gross_exposure": _rnd(r["gross_exposure"]),
        }
        entry.update(_closed_stats(r))
        per_strategy.append(entry)

    out = {
        "totals": {
            "realized_pnl": _rnd(realized),
            "unrealized_pnl": _rnd(unrealized),
            "total_pnl": _rnd(total_pnl),
            "capital": PAPER_CAPITAL,
            "equity": _rnd(PAPER_CAPITAL + total_pnl),
            "gross_exposure": _rnd(t["gross_exposure"]),
            "n_open": t["n_open"],
            "n_closed": t["n_closed"],
            "n_rejected": t["n_rejected"],
        },
        "per_strategy": per_strategy,
        "equity_curve": [
            {"ts": r["ts"].isoformat(), "equity": float(r["equity"])}
            for r in curve_rows
        ],
        "max_drawdown_pct": _rnd(mdd),
        "sharpe": _rnd(sharpe, 3),
        "n_daily_points": len(daily_equity),
    }
    out.update(_closed_stats(t))
    return out


@router.get("/trades/{trade_id}")
async def trade_detail(trade_id: int):
    """Full audit view of one trade, provenance JSON included — which
    filing, which model artifact, which gate values led to this order."""
    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(text(f"""
            SELECT {_TRADE_COLUMNS},
                   CASE WHEN status = 'OPEN'
                        THEN ({_UNREALIZED_SQL})::float8
                   END AS unrealized_pnl
            FROM paper_trades
            WHERE id = :id
        """), {"id": trade_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "trade_not_found")
    return _row_out(row)
