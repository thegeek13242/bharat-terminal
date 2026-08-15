"""Progressive-delivery compat contract (2026-08-13 latency work).

Every ImpactReport producer that predates progressive delivery (sentiment
bridge, commodity publisher, batch processor) and every payload persisted
before the rollout must deserialize as a normal COMPLETE report — the new
fields exist only to mark market-hours partials and late summary updates.
"""
from datetime import datetime, timezone

from bharat_terminal.types import ImpactReport, NewsItem


def _news():
    return NewsItem(
        id="n1", source="NSE_FILINGS", timestamp_utc=datetime.now(timezone.utc),
        headline="h", body="b", url="u", ingest_latency_ms=0.0)


def test_defaults_mark_reports_complete():
    r = ImpactReport(
        news_id="n1", news_item=_news(), relevant=True, confidence=0.9,
        processing_latency_ms=1.0, created_at=datetime.now(timezone.utc))
    assert r.analysis_status == "complete"
    assert r.update_seq == 0


def test_pre_rollout_payload_roundtrip():
    r = ImpactReport(
        news_id="n1", news_item=_news(), relevant=True, confidence=0.9,
        processing_latency_ms=1.0, created_at=datetime.now(timezone.utc))
    data = r.model_dump()
    # A pre-rollout consumer's payload lacks the new keys entirely.
    data.pop("analysis_status")
    data.pop("update_seq")
    r2 = ImpactReport(**data)
    assert r2.analysis_status == "complete"
    assert r2.update_seq == 0


def test_partial_update_roundtrip():
    r = ImpactReport(
        news_id="n1", news_item=_news(), relevant=True, confidence=0.9,
        processing_latency_ms=1.0, created_at=datetime.now(timezone.utc),
        analysis_status="analysing")
    assert r.analysis_status == "analysing"
    # model_copy on the frozen model is how the pipeline builds the late
    # summary update — update_seq must carry through serialization.
    upd = r.model_copy(update={"analysis_status": "complete", "update_seq": 1})
    assert ImpactReport(**upd.model_dump()).update_seq == 1
