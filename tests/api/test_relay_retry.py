"""Kafka relay boot-race retry: consumer.start() failing (api up before
redpanda, host reboots 2026-08-16 / 2026-08-21) must retry with backoff, not
kill the task silently — that froze the frontend feed for 8 days while
/health stayed green. Covers kafka_relay and sentiment_relay symmetrically."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bharat_terminal.api import kafka_relay, sentiment_relay

RELAYS = [
    pytest.param(kafka_relay, "_handle_report", "API_RELAY_DOWN", id="impact"),
    pytest.param(sentiment_relay, "_handle_snapshot", "API_SENTIMENT_RELAY_DOWN",
                 id="sentiment"),
]


def _fake_consumer_cls(fail_times: int, messages: list):
    """AIOKafkaConsumer stand-in: the first `fail_times` instances raise on
    start() (fresh instance per retry, like the real loop); later instances
    start, yield `messages` from getone(), then block forever."""
    created: list = []

    class FakeConsumer:
        def __init__(self, *args, **kwargs):
            self.idx = len(created)
            self.started = False
            self.stopped = False
            created.append(self)

        async def start(self):
            if self.idx < fail_times:
                raise ConnectionError("boot race: redpanda not accepting yet")
            self.started = True

        async def stop(self):
            self.stopped = True

        async def getone(self):
            if messages:
                return SimpleNamespace(value=messages.pop(0))
            await asyncio.Event().wait()

    return FakeConsumer, created


async def _until(cond, timeout_s: float = 2.0):
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not cond():
        assert asyncio.get_event_loop().time() < deadline, "condition never met"
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("relay, handler_name, breach_kind", RELAYS)
async def test_start_failure_retries_then_consumes(monkeypatch, relay,
                                                   handler_name, breach_kind):
    FakeConsumer, created = _fake_consumer_cls(fail_times=2,
                                               messages=[{"payload": 1}])
    breaches: list = []
    handled: list = []

    async def fake_handler(item):
        handled.append(item)

    monkeypatch.setattr(relay, "AIOKafkaConsumer", FakeConsumer)
    monkeypatch.setattr(relay, "CONNECT_RETRY_INITIAL_S", 0.0)
    monkeypatch.setattr(relay, "get_monitor", lambda: SimpleNamespace(
        record_breach=lambda kind, detail: breaches.append(kind)))
    monkeypatch.setattr(relay, handler_name, fake_handler)

    task = asyncio.create_task(relay._relay_loop())
    try:
        await _until(lambda: handled)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert handled == [{"payload": 1}], "message consumed after reconnect"
    assert len(created) == 3, "two failed attempts, then success"
    assert created[0].stopped and created[1].stopped, "failed consumers closed"
    assert breaches == [breach_kind] * 2, "each failed attempt recorded"
    assert created[2].started and created[2].stopped, \
        "live consumer closed on cancel"


@pytest.mark.parametrize("relay, handler_name, breach_kind", RELAYS)
async def test_cancel_during_backoff_ends_task(monkeypatch, relay,
                                               handler_name, breach_kind):
    FakeConsumer, created = _fake_consumer_cls(fail_times=10 ** 9, messages=[])
    monkeypatch.setattr(relay, "AIOKafkaConsumer", FakeConsumer)
    # Park the loop in its backoff sleep, then cancel into it.
    monkeypatch.setattr(relay, "CONNECT_RETRY_INITIAL_S", 30.0)
    monkeypatch.setattr(relay, "get_monitor", lambda: SimpleNamespace(
        record_breach=lambda kind, detail: None))

    task = asyncio.create_task(relay._relay_loop())
    await _until(lambda: created and created[0].stopped)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
