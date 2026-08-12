"""_fetch_index_window: flapping-bucket resilience (2026-08-12 incident).

BSE's Corp_FinanceResult_ng index endpoint intermittently returns a near-empty
200 body (1 row) PER REQUEST — minutes apart the same bucket flipped 542→1 and
1→1,817 rows — so the nightly market-wide run_market_scan(since_days=2)
silently ingested nothing for four days. The defense under test: best-of-3
retry on the selected bucket, an absolute plausibility floor for multi-day
market-wide windows (>5 rows), and escalation to wider buckets as rescue.
"""
import pytest

from bharat_terminal.kb.sync import bse_financial_results as m


def _rows(n):
    return [{"Scrip_cd": str(i)} for i in range(n)]


class _FakeLimiter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_index(monkeypatch, sequences):
    """sequences: flag -> list of successive results (last repeats forever)."""
    calls = []
    state = {k: list(v) for k, v in sequences.items()}

    async def fake_fetch(scrip_code, client, limiter, flagdur):
        calls.append(flagdur)
        seq = state.get(flagdur)
        if not seq:
            return None
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(m, "_fetch_index", fake_fetch)
    return calls


@pytest.mark.asyncio
async def test_healthy_first_fetch_passes_through(monkeypatch):
    calls = _patch_index(monkeypatch, {"2": [_rows(700)]})
    out = await m._fetch_index_window("", None, _FakeLimiter(), since_days=2)
    assert len(out) == 700
    assert calls == ["2"]  # plausible on attempt 1 — no retries, no escalation


@pytest.mark.asyncio
async def test_flap_heals_on_retry(monkeypatch):
    # The observed flap shape: near-empty body, then the real payload.
    calls = _patch_index(monkeypatch, {"2": [_rows(1), _rows(1817)]})
    out = await m._fetch_index_window("", None, _FakeLimiter(), since_days=2)
    assert len(out) == 1817
    assert calls == ["2", "2"]


@pytest.mark.asyncio
async def test_persistent_flap_escalates_wider(monkeypatch):
    calls = _patch_index(monkeypatch, {"2": [_rows(1)], "3": [_rows(2554)]})
    out = await m._fetch_index_window("", None, _FakeLimiter(), since_days=2)
    assert len(out) == 2554
    assert calls == ["2", "2", "2", "3"]  # best-of-3 exhausted, then rescue


@pytest.mark.asyncio
async def test_all_buckets_suspect_returns_largest_seen(monkeypatch):
    seqs = {f: [_rows(n)] for f, n in
            (("2", 1), ("3", 3), ("4", 0), ("5", 2), ("6", 1))}
    calls = _patch_index(monkeypatch, seqs)
    out = await m._fetch_index_window("", None, _FakeLimiter(), since_days=2)
    assert len(out) == 3  # largest across every bucket, degraded but honest


@pytest.mark.asyncio
async def test_single_day_window_passes_through_unchecked(monkeypatch):
    # 0 rows is legitimate for FlagDur=1 on a holiday — no retries, no rescue.
    calls = _patch_index(monkeypatch, {"1": [_rows(0)]})
    out = await m._fetch_index_window("", None, _FakeLimiter(), since_days=1)
    assert out == []
    assert calls == ["1"]


@pytest.mark.asyncio
async def test_unfetchable_index_returns_none(monkeypatch):
    calls = _patch_index(monkeypatch, {})
    out = await m._fetch_index_window("", None, _FakeLimiter(), since_days=2)
    assert out is None  # caller's parse_failed path
