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


# --- Task 2: constituent source chain + daily cache ---

TODAY = date(2026, 10, 31)
PAGE = ('<table id="main-table"><tr><th>No.</th><th>Symbol</th></tr>'
        '<tr><td>1</td><td>AAA</td></tr><tr><td>2</td><td>TLV:ZZZ</td></tr></table>')
C = etf_holdings_rs.Constituents


def _cfg(**over):
    base = {"enabled": True, "top_n": 5, "cache_max_age_days": 7, "request_delay_sec": 0}
    base.update(over)
    return base


def test_today_cache_hit_skips_network(tmp_path):
    etf_holdings_rs.save_cache(tmp_path, TODAY, {"XLK": C(["AAA"], "stockanalysis")})
    calls = []
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(),
        fetch_page=lambda u: calls.append(u) or PAGE, yf_top10=lambda e: None)
    assert calls == []
    assert out["XLK"].symbols == ["AAA"] and out["XLK"].source == "stockanalysis"


def test_live_fetch_is_cached_with_raw_symbols(tmp_path):
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: PAGE, yf_top10=lambda e: None)
    assert out["XLK"].symbols == ["AAA", "TLV:ZZZ"]   # foreign kept in cache; filtered at ranking
    assert out["XLK"].source == "stockanalysis"
    assert etf_holdings_rs.load_cache(tmp_path, TODAY)["XLK"].symbols == ["AAA", "TLV:ZZZ"]


def test_fetch_url_uses_lowercase_slug(tmp_path):
    seen = []
    etf_holdings_rs.fetch_constituents(
        ["BRK.B"], TODAY, tmp_path, _cfg(),
        fetch_page=lambda u: seen.append(u) or PAGE, yf_top10=lambda e: None)
    assert seen == ["https://stockanalysis.com/etf/brk.b/holdings/"]


def test_fetch_failure_walks_back_to_recent_cache(tmp_path):
    etf_holdings_rs.save_cache(tmp_path, date(2026, 10, 27), {"XLK": C(["OLD"], "stockanalysis")})
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: None, yf_top10=lambda e: ["T1"])
    assert out["XLK"].symbols == ["OLD"]
    assert out["XLK"].source == "stockanalysis-cache:2026-10-27"


def test_cache_older_than_max_age_is_not_used(tmp_path):
    etf_holdings_rs.save_cache(tmp_path, date(2026, 10, 23), {"XLK": C(["OLD"], "stockanalysis")})
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(cache_max_age_days=7),
        fetch_page=lambda u: None, yf_top10=lambda e: ["T1"])
    assert out["XLK"].symbols == ["T1"] and out["XLK"].source == "yf_top10"


def test_yf_top10_is_not_cached(tmp_path):
    etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: None, yf_top10=lambda e: ["T1"])
    assert "XLK" not in etf_holdings_rs.load_cache(tmp_path, TODAY)


def test_all_sources_fail_means_etf_absent(tmp_path):
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: None, yf_top10=lambda e: None)
    assert out == {}


def test_empty_parse_result_not_cached(tmp_path):
    empty = '<table id="main-table"><tr><th>No.</th><th>Symbol</th></tr></table>'
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: empty, yf_top10=lambda e: ["T1"])
    assert out["XLK"].source == "yf_top10"
    assert "XLK" not in etf_holdings_rs.load_cache(tmp_path, TODAY)


def test_corrupt_cache_is_ignored(tmp_path):
    p = etf_holdings_rs.cache_path(tmp_path, TODAY)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    out = etf_holdings_rs.fetch_constituents(
        ["XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: PAGE, yf_top10=lambda e: None)
    assert out["XLK"].source == "stockanalysis"


def test_yf_top10_exception_only_drops_that_etf(tmp_path):
    def boom(e):
        if e == "DXYZ":
            raise RuntimeError("no funds_data")
        return ["T1"]
    out = etf_holdings_rs.fetch_constituents(
        ["DXYZ", "XLK"], TODAY, tmp_path, _cfg(), fetch_page=lambda u: None, yf_top10=boom)
    assert set(out) == {"XLK"}


def test_sleep_between_live_requests_only(tmp_path):
    slept = []
    etf_holdings_rs.fetch_constituents(
        ["A", "B"], TODAY, tmp_path, _cfg(request_delay_sec=0.8),
        fetch_page=lambda u: PAGE, yf_top10=lambda e: None, sleep=slept.append)
    assert slept == [0.8]   # one pause between two live requests, none before the first
