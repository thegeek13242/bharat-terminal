"""Consume analysed.sentiment.in → company ImpactReport → analysed.impact.in.

A second, independent consumer (distinct group from the classifier) so the hot
classify path stays cheap and this bridge can fail in isolation. Mirrors
commodity/consumer.py + commodity/publisher.py. Output lands on the SAME
analysed.impact.in topic as news + commodity impacts, so it flows through the
existing api/kafka_relay persistence + WebSocket path unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from bharat_terminal.types import SentimentSnapshot
from bharat_terminal.sentiment.impact_bridge import build_impact_report_from_sentiment

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_IN = "analysed.sentiment.in"
TOPIC_OUT = "analysed.impact.in"
GROUP = "sentiment-impact-bridge"


def _dedup_signals(report):
    """Cross-path dedup via the shared signal_gates Redis key (news↔commodity↔
    polymarket). Fail-open: any error returns the report unchanged."""
    try:
        from bharat_terminal.analysis.signal_gates import filter_signals
        kept = filter_signals(report.trade_signals, report.news_item.headline)
        if len(kept) != len(report.trade_signals):
            return report.model_copy(update={"trade_signals": kept})
        return report
    except Exception as e:
        logger.debug(f"[SENTIMENT-BRIDGE] dedup skipped (fail-open): {e}")
        return report


class SentimentImpactBridgeConsumer:
    def __init__(self) -> None:
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            TOPIC_IN,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id=GROUP,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
        )
        await self._consumer.start()
        await self._producer.start()
        logger.info(f"[SENTIMENT-BRIDGE] connected; {TOPIC_IN} → {TOPIC_OUT}")

    async def run(self) -> None:
        assert self._consumer and self._producer
        async for msg in self._consumer:
            try:
                snap = SentimentSnapshot(**msg.value)
            except Exception as e:
                logger.warning(f"[SENTIMENT-BRIDGE] bad snapshot, skipping: {e}")
                continue
            try:
                report = await build_impact_report_from_sentiment(snap)
            except Exception as e:
                logger.error(f"[SENTIMENT-BRIDGE] bridge error for {snap.macro_theme}: {e}", exc_info=True)
                continue
            if report is None or not report.company_impacts:
                continue
            report = _dedup_signals(report)
            await self._producer.send_and_wait(
                TOPIC_OUT,
                value=report.model_dump(),
                key=(report.macro_theme or "POLYMARKET").encode("utf-8"),
            )
            logger.info(
                f"[SENTIMENT-BRIDGE] {snap.macro_theme}: {len(report.company_impacts)} impacts, "
                f"{len(report.trade_signals)} signals → {TOPIC_OUT}"
            )

    async def stop(self) -> None:
        if self._consumer:
            await self._consumer.stop()
        if self._producer:
            await self._producer.stop()
