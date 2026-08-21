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
