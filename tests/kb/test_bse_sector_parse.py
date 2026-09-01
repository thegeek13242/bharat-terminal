"""bse_sector_sync.parse_header — the official 4-level classification.

Verified live 2026-08-27 against ACC (scrip 500410): BSE's ComHeadernew
returns Sector/IndustryNew/IGroup/ISubGroup. This is the only source that
reaches the BSE-only tail; NSE's index CSVs cover 752 symbols (already
harvested) and ListofScripData's INDUSTRY field is 0% populated.
"""
from bharat_terminal.kb.sync.bse_sector_sync import parse_header

LIVE_ACC = {"Sector": "Commodities", "IndustryNew": "Construction Materials",
            "IGroup": "Cement & Cement Products",
            "ISubGroup": "Cement & Cement Products", "Group": "A",
            "Industry": "Cement & Cement Products"}


def test_parses_live_four_level_shape():
    got = parse_header(LIVE_ACC)
    assert got == {"macro_sector": "Commodities",
                   "sector": "Construction Materials",
                   "industry": "Cement & Cement Products",
                   "basic_industry": "Cement & Cement Products"}


def test_legacy_industry_fills_missing_igroup():
    got = parse_header({"Sector": "Commodities", "Industry": "Sugar"})
    assert got["industry"] == "Sugar" and got["macro_sector"] == "Commodities"


def test_blank_strings_become_none_not_empty():
    got = parse_header({"Sector": "  ", "IndustryNew": "", "IGroup": "Steel"})
    assert got["macro_sector"] is None and got["sector"] is None
    assert got["industry"] == "Steel"


def test_unclassified_scrip_returns_none_rather_than_blank_row():
    # Writing all-NULLs would mark the row "done" and it would never retry.
    assert parse_header({"Group": "X", "Industry": ""}) is None
    assert parse_header({}) is None
    assert parse_header(None) is None


def test_bse_placeholder_dashes_are_not_data():
    # Verified live on BSE-listed DEBT instruments (scrip 543145 "08ADR"):
    # unclassified scrips come back with "-" / blanks. Writing those would
    # mark the row classified with junk and block any future retry.
    assert parse_header({"Sector": None, "IndustryNew": None,
                         "IGroup": "-", "ISubGroup": "-"}) is None
    assert parse_header({"Sector": "NA", "IGroup": "N/A"}) is None
    assert parse_header({"Sector": "Commodities", "IGroup": "-"})["industry"] is None
