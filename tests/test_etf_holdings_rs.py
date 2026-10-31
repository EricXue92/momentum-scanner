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


# --- Task 3: 1M ranking within each ETF + continuation-line formatting ---


def _kline(ret_pct: float, n: int = 40) -> pd.DataFrame:
    """Flat series that jumps by ``ret_pct`` on the last bar, so the 1M score
    is exactly (0.4+0.2+0.2+0.2) * return (same trick as tests/test_etf_rs.py)."""
    closes = [100.0] * (n - 1) + [100.0 * (1 + ret_pct / 100)]
    return pd.DataFrame({
        "time_key": pd.bdate_range(end="2026-10-30", periods=n),
        "close": closes,
    })


def test_rank_holdings_strongest_first_relative_to_benchmark_top_n():
    cons = {"XLK": C(["A", "B", "C", "D", "E", "F", "TLV:X"], "stockanalysis")}
    kl = {s: _kline(r) for s, r in zip("ABCDEF", (1, 6, 3, 5, 2, 4))}
    kl["TLV:X"] = _kline(99)
    out = etf_holdings_rs.rank_holdings(cons, kl, _kline(0), top_n=5)
    assert out == {"XLK": ["B", "D", "F", "C", "E"]}   # TLV:X foreign; A is 6th, cut


def test_rank_holdings_fewer_than_top_n_lists_what_exists():
    cons = {"XLK": C(["A", "B"], "stockanalysis")}
    out = etf_holdings_rs.rank_holdings(cons, {"A": _kline(1), "B": _kline(2)}, _kline(0), 5)
    assert out == {"XLK": ["B", "A"]}


def test_rank_holdings_short_history_and_missing_klines_excluded():
    cons = {"XLK": C(["A", "B", "Z"], "stockanalysis")}
    out = etf_holdings_rs.rank_holdings(cons, {"A": _kline(1), "B": _kline(9, n=15)}, _kline(0), 5)
    assert out == {"XLK": ["A"]}


def test_rank_holdings_no_scorable_names_omits_etf():
    assert etf_holdings_rs.rank_holdings({"XLK": C(["TLV:X"], "stockanalysis")}, {}, _kline(0), 5) == {}


def test_rank_holdings_without_benchmark_uses_absolute_scores():
    cons = {"XLK": C(["A", "B"], "stockanalysis")}
    out = etf_holdings_rs.rank_holdings(cons, {"A": _kline(1), "B": _kline(2)}, None, 5)
    assert out == {"XLK": ["B", "A"]}


def test_format_extra():
    assert etf_holdings_rs.format_extra(["A", "B"], "stockanalysis") == "1M强: A > B"
    assert etf_holdings_rs.format_extra(["A"], "stockanalysis-cache:2026-10-27") == "1M强: A"
    assert etf_holdings_rs.format_extra(["A", "B"], "yf_top10") == "1M强: A > B (前十大)"


def test_compute_extras_end_to_end_maps_dot_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_holdings_rs, "fetch_constituents",
                        lambda *a, **k: {"XLF": C(["BRK.B", "JPM"], "stockanalysis")})
    asked = []
    monkeypatch.setattr(etf_holdings_rs, "_fetch_klines",
                        lambda syms: asked.extend(syms) or {"BRK-B": _kline(5), "JPM": _kline(1)})
    out = etf_holdings_rs.compute_extras(_cfg(), ["XLF"], _kline(0), TODAY, tmp_path)
    assert sorted(asked) == ["BRK-B", "JPM"]
    assert out == {"XLF": "1M强: BRK.B > JPM"}


def test_compute_extras_empty_klines_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_holdings_rs, "fetch_constituents",
                        lambda *a, **k: {"XLF": C(["JPM"], "stockanalysis")})
    monkeypatch.setattr(etf_holdings_rs, "_fetch_klines", lambda syms: {})
    assert etf_holdings_rs.compute_extras(_cfg(), ["XLF"], _kline(0), TODAY, tmp_path) == {}


def test_compute_extras_disabled_does_nothing(tmp_path, monkeypatch):
    def never(*a, **k):
        raise AssertionError("fetch_constituents called while disabled")
    monkeypatch.setattr(etf_holdings_rs, "fetch_constituents", never)
    assert etf_holdings_rs.compute_extras(_cfg(enabled=False), ["XLF"], None, TODAY, tmp_path) == {}
