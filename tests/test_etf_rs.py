"""Tests for etf_rs — daily 3M RS ranking of a fixed ETF list vs SPY.

yfinance is never hit: the kline fetch layer is monkeypatched.
"""

from datetime import date
from pathlib import Path

import pandas as pd

import etf_rs


def _kline(total_return_pct: float, n: int = 90) -> pd.DataFrame:
    """Flat series that jumps by ``total_return_pct`` on the last bar, so the
    3M score is exactly (0.5+0.3+0.2) * return."""
    closes = [100.0] * (n - 1) + [100.0 * (1 + total_return_pct / 100)]
    return pd.DataFrame({
        "time_key": pd.bdate_range(end="2026-09-11", periods=n),
        "close": closes,
    })


def _cfg(**over):
    base = {
        "enabled": True,
        "tickers": {"AAA": "甲", "BBB": "乙", "CCC": "丙"},
        "benchmark": "SPY",
    }
    base.update(over)
    return base


# --- rank_etfs (pure logic) ---


def test_ranking_is_strongest_first_and_relative_to_benchmark():
    klines = {
        "AAA": _kline(5),
        "BBB": _kline(20),
        "CCC": _kline(-3),
        "SPY": _kline(10),
    }
    table = etf_rs.rank_etfs(klines, ["AAA", "BBB", "CCC"], "SPY")
    assert list(table.index) == ["BBB", "AAA", "CCC"]
    # raw_score is benchmark-relative: BBB = 0.20 - 0.10
    assert abs(table.loc["BBB", "raw_score"] - 0.10) < 1e-9
    assert table.loc["BBB", "rs_percentile"] == 99
    assert table.loc["CCC", "rs_percentile"] == 33
    assert "SPY" not in table.index


def test_short_history_ticker_is_excluded_not_padded():
    klines = {"AAA": _kline(5), "BBB": _kline(1, n=40), "SPY": _kline(0)}
    table = etf_rs.rank_etfs(klines, ["AAA", "BBB"], "SPY")
    assert list(table.index) == ["AAA"]


def test_missing_benchmark_falls_back_to_absolute_scores():
    klines = {"AAA": _kline(5), "BBB": _kline(2)}
    table = etf_rs.rank_etfs(klines, ["AAA", "BBB"], "SPY")
    assert list(table.index) == ["AAA", "BBB"]
    assert abs(table.loc["AAA", "raw_score"] - 0.05) < 1e-9


# --- collapse_same_name ---


def test_collapse_keeps_strongest_of_same_name_only():
    table = pd.DataFrame(
        {"raw_score": [0.3, 0.2, 0.1, 0.0], "rs_percentile": [99, 66, 33, 1]},
        index=["GDXU", "GDX", "NUGT", "ZZZ"],
    )
    names = {"GDXU": "黄金矿业 杠杆", "NUGT": "黄金矿业 杠杆", "GDX": "黄金矿业"}
    kept, dropped = etf_rs.collapse_same_name(table, names)
    assert list(kept.index) == ["GDXU", "GDX", "ZZZ"]  # ZZZ unnamed → never collapsed
    assert dropped == [("NUGT", "GDXU")]


def test_run_collapses_same_name_in_file(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(20), "CCC": _kline(-3), "SPY": _kline(10)})
    cfg = _cfg(tickers={"AAA": "同名", "BBB": "同名", "CCC": "丙"})
    out = etf_rs.run_etf_rs(cfg, tmp_path, date(2026, 9, 15))
    assert out.read_text() == "BBB - 同名\nCCC - 丙\n"


# --- write_ranking ---


def test_write_ranking_one_per_line_strongest_first_with_names(tmp_path):
    table = pd.DataFrame(
        {"raw_score": [0.2, 0.1, 0.0], "rs_percentile": [99, 50, 1]},
        index=["BBB", "AAA", "CCC"],
    )
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, {}, tmp_path, date(2026, 9, 15))
    assert out == tmp_path / "TV" / "US" / "2026_09_15_ETF_rs.txt"
    # unnamed ticker → bare symbol line
    assert out.read_text() == "BBB - 乙\nAAA - 甲\nCCC\n"


def test_write_ranking_appends_top_holdings(tmp_path):
    table = pd.DataFrame(
        {"raw_score": [0.2, 0.1, 0.0], "rs_percentile": [99, 50, 1]},
        index=["BBB", "AAA", "CCC"],
    )
    holdings = {"BBB": "X、Y、Z", "CCC": "不适用: 期货"}  # AAA has none
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, holdings, tmp_path, date(2026, 9, 15))
    assert out.read_text() == "BBB - 乙 | X、Y、Z\nAAA - 甲\nCCC | 不适用: 期货\n"


def test_write_ranking_empty_table_writes_nothing(tmp_path):
    out = etf_rs.write_ranking(pd.DataFrame(columns=["raw_score", "rs_percentile"]),
                               {}, {}, tmp_path, date(2026, 9, 15))
    assert out is None
    assert not (tmp_path / "TV" / "US").exists()


# --- run_etf_rs (wiring, soft-fail) ---


def test_run_fetches_list_plus_benchmark_and_writes_file(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fake_fetch(tickers, **kw):
        seen.append(list(tickers))
        return {"AAA": _kline(5), "BBB": _kline(20), "CCC": _kline(-3), "SPY": _kline(10)}

    monkeypatch.setattr(etf_rs, "_fetch_klines", fake_fetch)
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15))
    assert seen == [["AAA", "BBB", "CCC", "SPY"]]
    assert out is not None and out.read_text() == "BBB - 乙\nAAA - 甲\nCCC - 丙\n"


def test_run_reads_holdings_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(20), "SPY": _kline(10)})
    cfg = _cfg(tickers={"AAA": "甲", "BBB": "乙"},
               holdings={"aaa ": " P、Q、R、S、T ", "BBB": ""})
    out = etf_rs.run_etf_rs(cfg, tmp_path, date(2026, 9, 15))
    assert out.read_text() == "BBB - 乙\nAAA - 甲 | P、Q、R、S、T\n"


def test_run_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert etf_rs.run_etf_rs(_cfg(enabled=False), tmp_path, date(2026, 9, 15)) is None
    assert etf_rs.run_etf_rs(_cfg(tickers={}), tmp_path, date(2026, 9, 15)) is None


def test_run_fetch_failure_soft_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {})
    assert etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15)) is None
    assert not (tmp_path / "TV" / "US" / "2026_09_15_ETF_rs.txt").exists()


def test_run_dedups_and_strips_config_tickers(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fake_fetch(tickers, **kw):
        seen.append(list(tickers))
        return {"AAA": _kline(5), "SPY": _kline(1)}

    monkeypatch.setattr(etf_rs, "_fetch_klines", fake_fetch)
    etf_rs.run_etf_rs(_cfg(tickers={"AAA ": "甲", "aaa": "重复", " SPY": "基准"}),
                      tmp_path, date(2026, 9, 15))
    assert seen == [["AAA", "SPY"]]


def test_run_accepts_plain_list_without_names(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines",
                        lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(1)})
    out = etf_rs.run_etf_rs(_cfg(tickers=["AAA"]), tmp_path, date(2026, 9, 15))
    assert out.read_text() == "AAA\n"
