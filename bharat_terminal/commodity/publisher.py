"""
Publishers for the commodity engine.

Two outputs:
  • analysed.impact.in   — full ImpactReport, same schema the news pipeline
    emits. The API gateway / WebSocket relay already broadcasts this topic
    so the frontend sees commodity-driven signals with no UI changes.
  • analysed.commodity_shock.in — raw CommodityShock event, kept for audit
    / replay / future per-shock dashboards.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from aiokafka import AIOKafkaProducer

from bharat_terminal.types import CommodityShock, ImpactReport

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_IMPACT = "analysed.impact.in"
TOPIC_SHOCK = "analysed.commodity_shock.in"


class CommodityEnginePublisher:
    def __init__(self):
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            acks="all",
            enable_idempotence=True,
            compression_type="gzip",
            linger_ms=10,
        )
        await self._producer.start()
        logger.info(
            f"[COMMODITY-PUB] connected; emitting to {TOPIC_IMPACT} + {TOPIC_SHOCK}"
        )

    async def publish_impact(self, report: ImpactReport) -> None:
        assert self._producer is not None
        data = report.model_dump()
        # Coerce datetimes
        if isinstance(data.get("created_at"), datetime):
            data["created_at"] = data["created_at"].isoformat()
        ni = data.get("news_item") or {}
        if isinstance(ni.get("timestamp_utc"), datetime):
            ni["timestamp_utc"] = ni["timestamp_utc"].isoformat()

        await self._producer.send_and_wait(
            TOPIC_IMPACT,
            value=data,
            key=(report.macro_theme or "COMMODITY").encode("utf-8"),
            headers=[
                ("source", b"COMMODITY_ENGINE"),
                ("macro_theme", (report.macro_theme or "").encode()),
                ("n_signals", str(len(report.trade_signals)).encode()),
            ],
        )

    async def publish_shock(self, shock: CommodityShock) -> None:
        assert self._producer is not None
        data = shock.model_dump()
        if isinstance(data.get("timestamp_utc"), datetime):
            data["timestamp_utc"] = data["timestamp_utc"].isoformat()
        await self._producer.send_and_wait(
            TOPIC_SHOCK,
            value=data,
            key=shock.commodity.encode("utf-8"),
            headers=[
                ("commodity", shock.commodity.encode()),
                ("direction", shock.direction.encode()),
                ("z_score", f"{shock.z_score:+.2f}".encode()),
                ("pct_move", f"{shock.pct_move:+.4f}".encode()),
            ],
        )

    async def stop(self) -> None:
        if self._producer:
            await self._producer.stop()
            logger.info("[COMMODITY-PUB] stopped")
