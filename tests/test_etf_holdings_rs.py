"""Tests for etf_holdings_rs — per-ETF 1M strongest-holdings line.

No network: the stockanalysis page fetch and yfinance calls are injected /
monkeypatched. The HTML fixture is the real XLF holdings page (2026-10-31)
trimmed to its ``<table id="main-table">``.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import etf_holdings_rs

FIX = Path(__file__).parent / "fixtures" / "stockanalysis_xlf_holdings.html"


# --- Task 1: parsing + US symbol filter ---


def test_parse_real_page_gives_25_symbols_in_page_order():
    syms = etf_holdings_rs.parse_holdings_html(FIX.read_text())
    assert len(syms) == 25
    assert syms[:2] == ["BRK.B", "JPM"]
    assert all(s == s.strip() and "<" not in s for s in syms)


def test_parse_table_without_rows_is_none():
    html = ('<table id="main-table"><thead><tr><th>No.</th><th>Symbol</th></tr></thead>'
            '<tbody></tbody></table>')
    assert etf_holdings_rs.parse_holdings_html(html) is None


def test_parse_page_without_table_is_none():
    assert etf_holdings_rs.parse_holdings_html("<html><body>Subscribe</body></html>") is None


def test_parse_unescapes_and_strips_anchor():
    html = ('<table id="main-table"><tr><th>No.</th><th>Symbol</th></tr>'
            '<tr><td>1</td><td><a href="/stocks/brk.b/"> BRK.B </a></td>'
            '<td>Berkshire &amp; Co</td></tr></table>')
    assert etf_holdings_rs.parse_holdings_html(html) == ["BRK.B"]


@pytest.mark.parametrize("sym,ok", [
    ("NVDA", True), ("BRK.B", True), ("A", True),
    ("TLV:NXSN", False), ("688836.SZ", False), ("SHE:002472", False),
    ("278A.T", False), ("-", False), ("", False), ("brk.b", False),
])
def test_is_us_symbol(sym, ok):
    assert etf_holdings_rs.is_us_symbol(sym) is ok


def test_yf_symbol_round_trip():
    assert etf_holdings_rs.to_yf_symbol("BRK.B") == "BRK-B"
    assert etf_holdings_rs.from_yf_symbol("BRK-B") == "BRK.B"
    assert etf_holdings_rs.to_yf_symbol("NVDA") == "NVDA"
