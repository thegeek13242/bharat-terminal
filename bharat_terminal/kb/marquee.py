"""Marquee-investor registry — curated smart-money identity resolution.

SHP names >1% holders as free text with wild spelling/entity variance and no
reliable PAN, so we resolve identity by normalized name + a curated alias set
for well-known investors (Jhunjhunwala family/RARE, govt, LIC, notable FIIs &
individuals). Uncurated holders still surface via auto-discovery (held in >=N
companies) — this registry only PINS and LABELS the famous names.

Conservative (data-accuracy rule): aliases only merge entities we are confident
are the same beneficial owner; when unsure, leave separate.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

_SUFFIXES = re.compile(
    r"\b(limited|ltd|private|pvt|llp|inc|corp|co|the|and|huf)\b", re.I)


def norm_investor(name: str) -> str:
    """Normalize a holder name for matching/grouping."""
    s = (name or "").lower()
    s = re.sub(r"[.,&'\-/()]", " ", s)
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# canonical_key -> {display, type, aliases (normalized fragments, substring match)}
# type: individual | family_office | government | insurer | mutual_fund | fii | institution
_MARQUEE_RAW = {
    "jhunjhunwala": {
        "display": "Rakesh Jhunjhunwala (RARE)", "type": "family_office",
        "aliases": ["rakesh jhunjhunwala", "rekha jhunjhunwala", "rekha rakesh jhunjhunwala",
                    "rare enterprises"],
    },
    "damani": {
        "display": "Radhakishan Damani (DMart / Bright Star)", "type": "family_office",
        "aliases": ["radhakishan damani", "rakesh damani", "gopikishan damani",
                    "bright star investments", "derive trading", "damani estates"],
    },
    "kacholia": {"display": "Ashish Kacholia", "type": "individual",
                 "aliases": ["ashish kacholia", "ashish dhawan"]},
    "vijay_kedia": {"display": "Vijay Kedia", "type": "individual",
                    "aliases": ["vijay kishanlal kedia", "kedia securities"]},
    "mukul_agrawal": {"display": "Mukul Agrawal", "type": "individual",
                      "aliases": ["mukul mahavir agrawal", "mukul agrawal"]},
    "dolly_khanna": {"display": "Dolly Khanna", "type": "individual",
                     "aliases": ["dolly khanna", "rajiv khanna"]},
    "porinju": {"display": "Porinju Veliyath (Equity Intelligence)", "type": "individual",
                "aliases": ["porinju veliyath", "equity intelligence"]},
    "sunil_singhania": {"display": "Sunil Singhania (Abakkus)", "type": "family_office",
                        "aliases": ["sunil singhania", "abakkus"]},
    "nemish_shah": {"display": "Nemish Shah (Enam)", "type": "individual",
                    "aliases": ["nemish shah"]},
    "president_of_india": {"display": "President of India (Govt)", "type": "government",
                           "aliases": ["president of india"]},
    "governor": {"display": "Government (Governor)", "type": "government",
                 "aliases": ["governor of", "government of india", "govt of india"]},
    "lic": {"display": "Life Insurance Corporation of India", "type": "insurer",
            "aliases": ["life insurance corporation", "lic of india", "lici ", "licindia"]},
    "sbi_mf": {"display": "SBI Mutual Fund", "type": "mutual_fund",
               "aliases": ["sbi mutual fund", "sbi small cap", "sbi large", "sbi bluechip"]},
    "hdfc_mf": {"display": "HDFC Mutual Fund", "type": "mutual_fund",
                "aliases": ["hdfc mutual fund", "hdfc small cap", "hdfc large", "hdfc trustee"]},
    "icici_pru_mf": {"display": "ICICI Prudential Mutual Fund", "type": "mutual_fund",
                     "aliases": ["icici prudential"]},
    "nippon_mf": {"display": "Nippon India Mutual Fund", "type": "mutual_fund",
                  "aliases": ["nippon india", "nippon life india", "reliance capital trustee"]},
    "kotak_mf": {"display": "Kotak Mutual Fund", "type": "mutual_fund",
                 "aliases": ["kotak mahindra trustee", "kotak small cap", "kotak flexi"]},
    "govt_singapore": {"display": "Government of Singapore", "type": "fii",
                       "aliases": ["government of singapore", "monetary authority of singapore"]},
    "smallcap_world": {"display": "Smallcap World Fund", "type": "fii",
                       "aliases": ["smallcap world fund"]},
    "vanguard": {"display": "Vanguard", "type": "fii", "aliases": ["vanguard"]},
    "blackrock": {"display": "BlackRock", "type": "fii", "aliases": ["blackrock"]},
    "fidelity": {"display": "Fidelity", "type": "fii",
                 "aliases": ["fidelity", "fil investment", "fmr "]},
    "abu_dhabi": {"display": "Abu Dhabi Investment Authority", "type": "fii",
                  "aliases": ["abu dhabi investment"]},
    "norges": {"display": "Norges Bank (Norway)", "type": "fii", "aliases": ["norges bank"]},
}

# Flattened alias -> (canonical_key, meta) for substring resolution.
_ALIAS_INDEX: List[tuple] = []
for _key, _meta in _MARQUEE_RAW.items():
    for _a in _meta["aliases"]:
        _ALIAS_INDEX.append((norm_investor(_a), _key, _meta))
# longest alias first so specific matches win
_ALIAS_INDEX.sort(key=lambda t: -len(t[0]))


def resolve_investor(holder_name: str) -> Optional[Dict[str, str]]:
    """Return {key, display, type} if the holder is a curated marquee investor,
    else None. Substring match on the normalized name (aliases are fragments)."""
    n = norm_investor(holder_name)
    if not n:
        return None
    for alias_norm, key, meta in _ALIAS_INDEX:
        if alias_norm and alias_norm in n:
            return {"key": key, "display": meta["display"], "type": meta["type"]}
    return None


def marquee_keys() -> Dict[str, Dict]:
    return _MARQUEE_RAW
