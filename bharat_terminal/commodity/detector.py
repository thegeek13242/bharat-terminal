"""
Sliding-window commodity-shock detector.

Maintains an in-memory deque of recent ticks per commodity. On each new
tick we evaluate whether the move over the last `window_minutes` is
"shocking" enough to fire — defined as both:

  1. |return| ≥ pct_threshold  (absolute % move floor — kills slow drift)
  2. |z-score| ≥ z_threshold   (return / rolling realised vol — kills
     normal-vol moves on volatile days)

Both conditions are required so that a 0.3% move on a normally-still day
doesn't fire (z is high but return tiny), and a 2% move during a
high-vol session doesn't fire either (return big but z low).

After firing, we suppress further shocks on the same commodity for
`cooldown_seconds` to prevent spamming during sustained moves.

State is per-process: if you run multiple replicas, partition by
commodity (the Kafka producer already does this — key = commodity name).
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple

from bharat_terminal.types import CommodityShock, CommodityTick

logger = logging.getLogger(__name__)


@dataclass
class _CommodityState:
    # (epoch_seconds, last_price)
    history: Deque[Tuple[float, float]] = field(default_factory=lambda: deque(maxlen=10000))
    last_shock_at: float = 0.0


class CommodityShockDetector:
    """
    Stateful detector. One instance per process; thread-safe access not
    needed since the consumer is single-task.
    """

    def __init__(
        self,
        window_minutes: int = 10,
        pct_threshold: float = 0.012,        # 1.2% move floor
        z_threshold: float = 3.0,
        baseline_window_minutes: int = 60,
        cooldown_seconds: float = 300.0,     # 5 min between shocks per commodity
        min_history_points: int = 20,
    ):
        self._window_seconds = window_minutes * 60
        self._pct_threshold = pct_threshold
        self._z_threshold = z_threshold
        self._baseline_seconds = baseline_window_minutes * 60
        self._cooldown = cooldown_seconds
        self._min_history = min_history_points
        self._window_minutes = window_minutes

        self._state: Dict[str, _CommodityState] = {}

    # ── Tick ingest + evaluation ─────────────────────────────────────────

    def evaluate(self, tick: CommodityTick) -> Optional[CommodityShock]:
        """
        Append the tick to its commodity's history, then check shock conditions.
        Returns a CommodityShock if a fresh shock just fired, else None.
        """
        commodity = tick.commodity
        st = self._state.setdefault(commodity, _CommodityState())

        now_s = tick.timestamp_utc.timestamp()
        st.history.append((now_s, tick.last_price))
        self._evict_old(st, now_s)

        if len(st.history) < self._min_history:
            return None

        # Cooldown: don't double-fire while a shock is still developing
        if (now_s - st.last_shock_at) < self._cooldown:
            return None

        baseline_price = self._price_at_offset(st, now_s, self._window_seconds)
        if baseline_price is None or baseline_price <= 0:
            return None

        ret = (tick.last_price / baseline_price) - 1.0
        if abs(ret) < self._pct_threshold:
            return None

        # Realised-vol baseline over the longer window for z-scoring
        vol = self._rolling_return_vol(st, now_s, self._baseline_seconds, self._window_seconds)
        if vol is None or vol <= 0:
            return None

        z = ret / vol
        if abs(z) < self._z_threshold:
            return None

        # Fire
        st.last_shock_at = now_s
        direction = "up" if ret > 0 else "down"

        detection_latency_ms = max(
            0.0, (time.time() - now_s) * 1000.0
        )

        shock = CommodityShock(
            timestamp_utc=datetime.fromtimestamp(now_s, tz=timezone.utc),
            commodity=commodity,
            contract_symbol=tick.contract_symbol,
            direction=direction,
            pct_move=ret,
            z_score=z,
            window_minutes=self._window_minutes,
            spot_price=tick.last_price,
            baseline_price=baseline_price,
            detection_latency_ms=detection_latency_ms,
        )
        logger.info(
            f"[DETECTOR] SHOCK {commodity} {direction.upper()} "
            f"Δ={ret*100:+.2f}% z={z:+.2f} "
            f"({baseline_price:.2f} → {tick.last_price:.2f} over "
            f"{self._window_minutes}min)"
        )
        return shock

    # ── Helpers ─────────────────────────────────────────────────────────

    def _evict_old(self, st: _CommodityState, now_s: float) -> None:
        """Drop history older than the vol baseline reach: the vol window now
        ends at (now - detection_window) and spans baseline_seconds before that,
        so we must retain window + baseline (+slack) of history."""
        cutoff = now_s - self._window_seconds - self._baseline_seconds - 60
        while st.history and st.history[0][0] < cutoff:
            st.history.popleft()

    @staticmethod
    def _price_at_offset(
        st: _CommodityState, now_s: float, offset_s: float
    ) -> Optional[float]:
        """Find the most recent price at-or-before (now - offset_s)."""
        target = now_s - offset_s
        candidate: Optional[float] = None
        # Linear scan; deques are short enough for this to be cheap.
        for ts, px in st.history:
            if ts <= target:
                candidate = px
            else:
                break
        return candidate

    def _rolling_return_vol(
        self,
        st: _CommodityState,
        now_s: float,
        baseline_seconds: float,
        return_window_seconds: float,
    ) -> Optional[float]:
        """
        Realised vol of N-minute returns over the baseline hour that ENDS at
        (now - return_window_seconds) — i.e. the "normal" volatility BEFORE the
        current detection window, explicitly excluding the move we're testing.

        This is the fix for the detector's historical shocks=0: measuring vol
        over a window that included the live move let the move inflate its own
        denominator, so a genuine +3.2%/10min crude shock scored z≈2.4 and never
        fired. Anchoring the vol baseline before the detection window makes a
        sharp move look abnormal against the calm that preceded it (that same
        crude shock now scores z≈6-10). During genuinely volatile regimes the
        prior hour is itself volatile, so z is naturally dampened — the intended
        behaviour of the second gate.
        """
        anchor = now_s - return_window_seconds     # exclude the detection window
        cutoff = anchor - baseline_seconds
        # Sample prices on a coarse grid (every return_window_seconds)
        grid_step = return_window_seconds
        sampled: list = []
        t = cutoff
        while t <= anchor:
            px = self._price_at_offset(st, now_s, now_s - t)
            if px is not None:
                sampled.append(px)
            t += grid_step

        if len(sampled) < 4:
            return None

        rets = []
        for i in range(1, len(sampled)):
            prev = sampled[i - 1]
            cur = sampled[i]
            if prev > 0:
                rets.append((cur / prev) - 1.0)
        if len(rets) < 3:
            return None

        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var <= 0:
            return None
        return math.sqrt(var)
