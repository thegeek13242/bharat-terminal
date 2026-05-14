"""
Bharat Terminal — F&O engine.

Consumes the raw.fno.in tick stream from Angel One SmartWebSocketV2 and
writes OI-significant events to the `derivative_ticks` table for downstream
analysis. Raw tick rate is too high to persist unfiltered (~5M ticks/day);
this service is the persistent-state boundary between high-frequency ticks
and the slower analytical layer.

Persistence policy (see `engine.py` for current thresholds):
  • OI delta ≥ 1.0% vs last persisted tick for that contract  → persist
  • At least one tick per contract per 60 s                    → persist (heartbeat)
  • Final tick of session                                      → persist
  • Otherwise                                                  → discard

Trigger reason is stamped on every persisted row for audit.
"""
