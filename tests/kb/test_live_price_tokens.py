"""kb.live_price — scrip-master parsing + searchScrip fallback selection.

Born from the 2026-08-21 outage: Angel One's CDN served the 35MB scrip
master at 4-11 KB/s with truncated payloads, every cold-start download
failed, both token maps stayed empty, and papertrade was blind for a whole
session. The pure helpers pinned here are what the per-symbol fallback and
the cache path rely on. No network: the module imports only httpx at
module level, and these tests touch pure functions.
"""
from bharat_terminal.kb.live_price import (_parse_scrip_master,
                                           _pick_search_match)


def rows(*specs):
    return [{"exch_seg": seg, "symbol": sym, "token": tok,
             "instrumenttype": it} for seg, sym, tok, it in specs]


# ── _parse_scrip_master ──────────────────────────────────────────────────

def test_nse_series_ranking_prefers_eq_over_be_over_sme():
    nse, _ = _parse_scrip_master(rows(
        ("NSE", "GLFL-BE", "201", ""),
        ("NSE", "GLFL-EQ", "200", ""),      # EQ must win regardless of order
        ("NSE", "ABC-SM", "300", ""),
        ("NSE", "ABC-BE", "301", ""),       # BE outranks SME
    ))
    assert nse["GLFL"] == "200"
    assert nse["ABC"] == "301"


def test_non_cash_nse_series_excluded():
    nse, _ = _parse_scrip_master(rows(
        ("NSE", "GOI-GS", "900", ""),       # gilt
        ("NSE", "FOO-MF", "901", ""),       # mutual fund
        ("NSE", "BAR-N1", "902", ""),       # bond
    ))
    assert nse == {}


def test_bse_equities_only_instrumenttype_blank():
    _, bse = _parse_scrip_master(rows(
        ("BSE", "MINID", "543999", ""),
        ("BSE", "SENSEX", "1", "AMXIDX"),   # index, excluded
    ))
    assert bse == {"MINID": "543999"}


# ── _pick_search_match ───────────────────────────────────────────────────

def search(*pairs):
    return [{"tradingsymbol": ts, "symboltoken": tok} for ts, tok in pairs]


def test_search_nse_exact_base_match_ranked():
    # "LT" must not match "LTF-EQ" or "LTTS-EQ"; EQ beats BE.
    got = _pick_search_match(search(("LTF-EQ", "1"), ("LT-BE", "2"),
                                    ("LT-EQ", "3"), ("LTTS-EQ", "4")),
                             "LT", "NSE")
    assert got == "3"


def test_search_nse_no_match_returns_none():
    assert _pick_search_match(search(("LTF-EQ", "1")), "LT", "NSE") is None


def test_search_bse_matches_tradingsymbol_exactly():
    assert _pick_search_match(search(("MINIDX", "9"), ("MINID", "543999")),
                              "MINID", "BSE") == "543999"


def test_search_ignores_rows_without_token():
    assert _pick_search_match(search(("LT-EQ", "")), "LT", "NSE") is None


# ── module surface smoke test ────────────────────────────────────────────
# 2026-08-21: a text-slice patch silently swallowed _smartconnect_from_redis
# and every /ltp call 500'd with a NameError that py_compile cannot see.
# Pin the private surface the request path depends on.

def test_request_path_helpers_exist():
    from bharat_terminal.kb import live_price as lp
    for name in ("_smartconnect_from_redis", "_ensure_equity_tokens",
                 "_resolve_unmapped", "_resolve_via_search",
                 "_refresh_from_cdn", "_download_scrip_master",
                 "_load_cached_tokens", "get_current_ltps", "get_full_quotes"):
        assert callable(getattr(lp, name)), name


# ── resumable download against a connection-cutting server ──────────────
# Simulates what Angel One's scrip-master ELB did on 2026-08-21: honours
# Range (206) but closes the connection every few KB. The loop must
# accumulate across attempts AND across calls (shared state), and must
# restart if the server ignores Range (200).
import asyncio

import httpx
import pytest

from bharat_terminal.kb.live_price import _download_scrip_master

BODY = bytes(range(256)) * 40            # 10,240 bytes
CUT = 3000                               # server closes after 3000 bytes


class CuttingStream(httpx.AsyncByteStream):
    def __init__(self, data, cut):
        self.data, self.cut = data, cut

    async def __aiter__(self):
        yield self.data[:self.cut]
        if len(self.data) > self.cut:
            raise httpx.RemoteProtocolError("peer closed connection")


def cutting_server(request):
    rng = request.headers.get("Range")
    start = int(rng.split("=")[1].rstrip("-")) if rng else 0
    if start >= len(BODY):
        return httpx.Response(416)
    chunk = BODY[start:]
    status = 206 if rng else 200
    headers = {"Content-Range": f"bytes {start}-{len(BODY)-1}/{len(BODY)}"} if rng \
        else {"Content-Length": str(len(BODY))}
    return httpx.Response(status, headers=headers,
                          stream=CuttingStream(chunk, CUT))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _nosleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", _nosleep)


async def test_resume_accumulates_across_cuts():
    state = {"buf": bytearray(), "total": None}
    async with httpx.AsyncClient(transport=httpx.MockTransport(cutting_server)) as c:
        out = await _download_scrip_master(c, "https://x/scrip.json",
                                           max_attempts=10, state=state)
    assert out == BODY                       # 4 cuts, fully assembled
    assert state["buf"] == b"" and state["total"] is None   # cleared on success


async def test_progress_persists_across_calls_when_attempts_run_out():
    state = {"buf": bytearray(), "total": None}
    async with httpx.AsyncClient(transport=httpx.MockTransport(cutting_server)) as c:
        with pytest.raises(RuntimeError):
            await _download_scrip_master(c, "https://x/scrip.json",
                                         max_attempts=2, state=state)
        kept = len(state["buf"])
        assert kept == 2 * CUT               # two cuts' worth retained
        out = await _download_scrip_master(c, "https://x/scrip.json",
                                           max_attempts=10, state=state)
    assert out == BODY                       # second call resumed, not restarted


async def test_server_ignoring_range_restarts_buffer():
    def ignores_range(request):
        return httpx.Response(200, headers={"Content-Length": str(len(BODY))},
                              stream=CuttingStream(BODY, len(BODY)))
    state = {"buf": bytearray(b"stale-partial"), "total": None}
    async with httpx.AsyncClient(transport=httpx.MockTransport(ignores_range)) as c:
        out = await _download_scrip_master(c, "https://x/scrip.json",
                                           max_attempts=3, state=state)
    assert out == BODY                       # stale partial discarded
