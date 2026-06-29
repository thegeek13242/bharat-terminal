"""Shared alerting entrypoint for all microservices.

The implementation lives in ``analysis.guardrails`` (self-contained — only
stdlib, no analysis-specific imports) and is re-exported here so any service can:

    from bharat_terminal.alerts import get_monitor
    get_monitor().alert_now("INGESTION_ADAPTER_CRASH", "...")      # one-shot critical
    get_monitor().record_breach("API_DB_PERSIST", "...")          # rate-based

Each process gets its own monitor singleton (independent breach/cost state) but
they all deliver through the same env-configured channel (Teams webhook / SMTP),
so alerts from every service land in the same place with per-kind cooldowns.
"""
from bharat_terminal.analysis.guardrails import (  # noqa: F401
    GuardrailMonitor,
    get_monitor,
)

__all__ = ["GuardrailMonitor", "get_monitor"]
