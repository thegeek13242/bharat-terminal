#!/usr/bin/env python3
"""Egress connectivity probe — go/no-go gate before migrating off GCP.

WHY THIS EXISTS
NSE has previously returned 403 Forbidden to this project's VM (resolved by
moving to the nsearchives host). Exchange endpoints are sensitive to the
CALLER'S IP REPUTATION, so "it works from GCP asia-south1" is NOT evidence
that it works from AWS ap-south-1. Discovering a block after a DNS cutover,
mid-results-season, would be extremely expensive. Run this on a throwaway
instance in the candidate region FIRST.

WHAT IT DOES
Hits every external host the ingestion + sync path depends on, replicating
the real adapters' headers (User-Agent / Referer / NSE cookie warm-up),
and reports PASS/FAIL/DEGRADED per source with status codes and latency.
Read-only: GETs public endpoints only. No credentials, no writes, no DB.

USAGE
    # on a fresh EC2 box in ap-south-1 (or anywhere you're evaluating):
    python3 -m pip install --quiet httpx
    python3 probe_egress.py                 # human-readable table
    python3 probe_egress.py --json          # machine-readable
    python3 probe_egress.py --compare base.json   # diff vs a baseline run

    # capture a baseline on the CURRENT GCP VM first, so you are comparing
    # like with like rather than guessing what "normal" looks like:
    python3 probe_egress.py --json > gcp-baseline.json

EXIT CODES
    0  every CRITICAL source passed
    1  at least one CRITICAL source failed  -> do NOT migrate ingestion yet
    2  probe itself could not run (missing dep, no network at all)

Angel One and Vertex/Bedrock are NOT probed: both need credentials, and
Angel One additionally may IP-allowlist. Check those separately (see
NOTES at the bottom of this file).
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing
    print("ERROR: httpx not installed.  python3 -m pip install httpx", file=sys.stderr)
    sys.exit(2)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Header sets copied from the live adapters so the probe is a faithful test:
#   ingestion/adapters/nse_filings.py     (NSE JSON API)
#   ingestion/adapters/bse_filings.py     (BSE JSON API)
#   kb/sync/nse_financial_results.py      (archives host + cookie warm-up)
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
}


def _recent_weekday(days_back: int = 1) -> datetime:
    """Last weekday — bhavcopy/participant-OI files only exist for sessions.
    Not holiday-aware: a holiday yields 404, which the probe reports as
    DEGRADED (reachable, no file) rather than FAIL (blocked)."""
    d = datetime.now(timezone.utc) - timedelta(days=days_back)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


_D = _recent_weekday()

# (key, description, url, headers, critical, expect_json)
# critical=True  -> a failure here blocks the migration
# critical=False -> secondary/enrichment source; note it, don't gate on it
CHECKS = [
    ("nse_api_announcements", "NSE corporate announcements (live filings feed)",
     "https://www.nseindia.com/api/home-corporate-announcements?index=equities",
     NSE_HEADERS, True, True),
    ("nse_api_board_meetings", "NSE board meetings",
     "https://www.nseindia.com/api/home-board-meetings?index=equities",
     NSE_HEADERS, True, True),
    ("nse_archives_equity_list", "NSE archives — EQUITY_L.csv (universe)",
     "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
     NSE_HEADERS, True, False),
    ("nse_archives_bhavcopy", f"NSE archives — bhavcopy {_D:%Y-%m-%d} (prices)",
     f"https://nsearchives.nseindia.com/content/cm/"
     f"BhavCopy_NSE_CM_0_0_0_{_D:%Y%m%d}_F_0000.csv.zip",
     NSE_HEADERS, True, False),
    ("nse_archives_bulk_deals", "NSE archives — bulk.csv (deals layer)",
     "https://nsearchives.nseindia.com/content/equities/bulk.csv",
     NSE_HEADERS, False, False),
    ("nse_archives_sec_list", "NSE archives — sec_list.csv (surveillance/ASM)",
     "https://nsearchives.nseindia.com/content/equities/sec_list.csv",
     NSE_HEADERS, False, False),
    ("nse_archives_participant_oi", f"NSE archives — participant OI {_D:%d%m%Y}",
     f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{_D:%d%m%Y}.csv",
     NSE_HEADERS, False, False),
    ("bse_ann_subcategory", "BSE announcements API (dual-exchange filings)",
     "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
     "?pageno=1&strCat=-1&strPrevDate=&strScrip=&strSearch=P&strToDate=&strType=C&subcategory=",
     BSE_HEADERS, True, True),
    ("bse_scrip_list", "BSE ListofScripData (BSE-only universe)",
     "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
     "?Group=&Scripcode=&industry=&segment=Equity&status=Active",
     BSE_HEADERS, True, True),
    # Param names must match kb/sync/bse_financial_results.py exactly —
    # wrong params return an HTML error page, which looks identical to a bot
    # wall and produces a false NO-GO.
    ("bse_financial_results", "BSE Corp_FinanceResult_ng (XBRL results chain)",
     "https://api.bseindia.com/BseIndiaAPI/api/Corp_FinanceResult_ng/w"
     # SCRIP_CD pinned to RELIANCE (500325) + FlagDur=6 (1yr): always has
     # rows and returns in ~1s. An empty SCRIP_CD scans the whole universe
     # and times out — that is a probe artifact, not a block.
     "?SCRIP_CD=500325&FlagDur=6&HFQ=&ISUBGROUP_CODE=",
     BSE_HEADERS, True, True),
    # Secondary/enrichment hosts — informational only, never gating.
    ("polymarket", "Polymarket gamma API (prediction-market sentiment)",
     "https://gamma-api.polymarket.com/markets?limit=1", {}, False, True),
    ("google_news", "Google News RSS (secondary source)",
     "https://news.google.com/rss/search?q=NSE&hl=en-IN&gl=IN&ceid=IN:en",
     {"User-Agent": NSE_HEADERS["User-Agent"]}, False, False),
]


def warm_nse_cookies(client: httpx.Client) -> str:
    """NSE hands out session cookies on the HTML homepage; the JSON API
    returns HTML/403 without them (mirrors refresh_nse_cookies() in
    kb/sync/nse_financial_results.py)."""
    try:
        r = client.get("https://www.nseindia.com/", headers=NSE_HEADERS,
                       timeout=TIMEOUT, follow_redirects=True)
        return f"{r.status_code} ({len(client.cookies)} cookies)"
    except Exception as e:
        return f"FAILED: {type(e).__name__}: {str(e)[:80]}"


def classify(resp: httpx.Response, expect_json: bool) -> tuple[str, str]:
    """PASS / DEGRADED / FAIL plus a human note.

    The distinction that matters for a migration decision:
      FAIL     = blocked or refused  -> IP reputation problem
      DEGRADED = reachable but no usable payload (holiday file missing,
                 HTML challenge page) -> investigate, not necessarily fatal
    """
    code = resp.status_code
    if code in (401, 403, 407, 429):
        return "FAIL", f"HTTP {code} — blocked/throttled at the edge"
    if code == 404:
        return "DEGRADED", "HTTP 404 — reachable, file absent (holiday?)"
    if code >= 500:
        return "DEGRADED", f"HTTP {code} — upstream error, retry later"
    if code != 200:
        return "DEGRADED", f"HTTP {code}"
    body = resp.content[:400].lstrip()
    if body[:9].lower() in (b"<!doctype", b"<html") or body[:5].lower() == b"<html":
        return "FAIL", "HTTP 200 but HTML returned — bot challenge / redirect wall"
    if expect_json:
        try:
            json.loads(resp.content)
        except Exception:
            return "DEGRADED", "HTTP 200 but body is not valid JSON"
    if not resp.content:
        return "DEGRADED", "HTTP 200 with empty body"
    return "PASS", f"HTTP 200, {len(resp.content):,} bytes"


def run() -> dict:
    results = []
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        cookie_status = warm_nse_cookies(client)
        for key, desc, url, headers, critical, expect_json in CHECKS:
            t0 = time.monotonic()
            try:
                resp = client.get(url, headers=headers)
                verdict, note = classify(resp, expect_json)
                status_code = resp.status_code
            except Exception as e:
                verdict, note, status_code = "FAIL", \
                    f"{type(e).__name__}: {str(e)[:90]}", None
            results.append({
                "key": key, "description": desc, "url": url,
                "critical": critical, "verdict": verdict, "note": note,
                "status_code": status_code,
                "latency_ms": round((time.monotonic() - t0) * 1000),
            })

    try:
        egress_ip = httpx.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        egress_ip = "unknown"

    return {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "hostname": socket.gethostname(),
        "egress_ip": egress_ip,
        "nse_cookie_warmup": cookie_status,
        "results": results,
        "critical_failures": [r["key"] for r in results
                              if r["critical"] and r["verdict"] == "FAIL"],
    }


COLOR = {"PASS": "\033[32m", "DEGRADED": "\033[33m", "FAIL": "\033[31m"}
RESET = "\033[0m"


def render(report: dict, use_color: bool = True) -> None:
    def c(v):
        return f"{COLOR[v]}{v:<8}{RESET}" if use_color else f"{v:<8}"

    print(f"\nEgress probe — {report['probed_at']}")
    print(f"host {report['hostname']}   egress IP {report['egress_ip']}")
    print(f"NSE cookie warm-up: {report['nse_cookie_warmup']}\n")
    print(f"{'VERDICT':<9}{'CRIT':<6}{'ms':>6}  SOURCE")
    print("─" * 78)
    for r in report["results"]:
        crit = "yes" if r["critical"] else "—"
        print(f"{c(r['verdict'])}{crit:<6}{r['latency_ms']:>6}  {r['description']}")
        print(f"{'':<21}{r['note']}")
    print("─" * 78)

    fails = report["critical_failures"]
    if fails:
        print(f"\n{'❌ NO-GO' if use_color else 'NO-GO'} — {len(fails)} critical "
              f"source(s) blocked: {', '.join(fails)}")
        print("This IP/ASN cannot reach the exchanges the pipeline needs.")
        print("Do NOT migrate ingestion here. Options: keep ingestion on the")
        print("current host, use a proxy/NAT with a known-good IP, or try a")
        print("different region/provider and re-run this probe.")
    else:
        degraded = [r["key"] for r in report["results"] if r["verdict"] == "DEGRADED"]
        print(f"\n{'✅ GO' if use_color else 'GO'} — all critical sources reachable.")
        if degraded:
            print(f"Review (non-blocking): {', '.join(degraded)}")
        print("Re-run on a trading day during market hours before final cutover;")
        print("some endpoints behave differently when the session is live.")


def compare(report: dict, baseline_path: str) -> None:
    with open(baseline_path) as f:
        base = json.load(f)
    b = {r["key"]: r["verdict"] for r in base["results"]}
    print(f"\nComparison vs baseline ({base.get('egress_ip', '?')} @ "
          f"{base.get('probed_at', '?')})")
    print("─" * 78)
    regressions = 0
    for r in report["results"]:
        was, now = b.get(r["key"], "—"), r["verdict"]
        if was != now:
            flag = ""
            if was == "PASS" and now != "PASS":
                flag, regressions = "  <-- REGRESSION", regressions + 1
            print(f"{r['key']:<28} {was:<9} -> {now:<9}{flag}")
    print("─" * 78)
    print(f"{regressions} regression(s) vs baseline."
          if regressions else "No regressions vs baseline.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--compare", metavar="BASELINE.json",
                    help="diff this run against a previous --json run")
    args = ap.parse_args()

    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render(report, use_color=not args.no_color)
        if args.compare:
            compare(report, args.compare)
    return 1 if report["critical_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())

# ── NOTES: what this probe deliberately does NOT cover ──────────────────────
#
# 1. ANGEL ONE (smartapi.angelone.in) — needs live credentials and may
#    IP-allowlist. Test separately by running the existing kb quote path
#    from the candidate host with real creds, or ask Angel One support
#    whether the API key is IP-bound before you cut over.
#
# 2. VERTEX AI / BEDROCK — auth, not reachability. On a GCP VM the LLM
#    wrapper gets Application Default Credentials free from the metadata
#    server; OFF GCP it needs an explicit service-account key. Verify that
#    before assuming inference keeps working from AWS.
#
# 3. MARKET-HOURS BEHAVIOUR — some NSE/BSE endpoints return different
#    payloads (or throttle harder) while a session is live. A clean run at
#    22:00 IST on a Sunday is necessary but NOT sufficient; re-run on a
#    trading day before the final cutover.
#
# 4. SUSTAINED-RATE BEHAVIOUR — this is a single-shot probe. The adapters
#    poll continuously (NSE 20/min, BSE 10/min). An IP can pass a one-off
#    request and still get throttled under steady load; watch the first
#    hour of real ingestion closely after any move.
