"""Tests for etf_rs — daily 3M RS ranking of a fixed ETF list vs SPY.

yfinance is never hit: the kline fetch layer is monkeypatched.
"""

from datetime import date
from pathlib import Path

import pandas as pd

import etf_rs
import etf_holdings_rs
import pytest


@pytest.fixture(autouse=True)
def _no_holdings_rs_network(monkeypatch):
    """run_etf_rs now runs the holdings 1M step, whose default path goes
    online; stub it to "nothing" unless a test installs its own fake."""
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: {})


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
    assert out.read_text() == "1. BBB - 同名\n2. CCC - 丙\n"


# --- write_ranking ---


def test_write_ranking_one_per_line_strongest_first_with_names(tmp_path):
    table = pd.DataFrame(
        {"raw_score": [0.2, 0.1, 0.0], "rs_percentile": [99, 50, 1]},
        index=["BBB", "AAA", "CCC"],
    )
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, {}, tmp_path, date(2026, 9, 15))
    assert out == tmp_path / "TV" / "US" / "2026_09_15_ETF_rs_3m.txt"
    # unnamed ticker → bare symbol line
    assert out.read_text() == "1. BBB - 乙\n2. AAA - 甲\n3. CCC\n"


def test_write_ranking_appends_top_holdings(tmp_path):
    table = pd.DataFrame(
        {"raw_score": [0.2, 0.1, 0.0], "rs_percentile": [99, 50, 1]},
        index=["BBB", "AAA", "CCC"],
    )
    holdings = {"BBB": "X、Y、Z", "CCC": "不适用: 期货"}  # AAA has none
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, holdings, tmp_path, date(2026, 9, 15))
    assert out.read_text() == "1. BBB - 乙 | X、Y、Z\n2. AAA - 甲\n3. CCC | 不适用: 期货\n"


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
    assert out is not None and out.read_text() == "1. BBB - 乙\n2. AAA - 甲\n3. CCC - 丙\n"


def test_run_reads_holdings_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(20), "SPY": _kline(10)})
    cfg = _cfg(tickers={"AAA": "甲", "BBB": "乙"},
               holdings={"aaa ": " P、Q、R、S、T ", "BBB": ""})
    out = etf_rs.run_etf_rs(cfg, tmp_path, date(2026, 9, 15))
    assert out.read_text() == "1. BBB - 乙\n2. AAA - 甲 | P、Q、R、S、T\n"


def test_run_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert etf_rs.run_etf_rs(_cfg(enabled=False), tmp_path, date(2026, 9, 15)) is None
    assert etf_rs.run_etf_rs(_cfg(tickers={}), tmp_path, date(2026, 9, 15)) is None


def test_run_fetch_failure_soft_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {})
    assert etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15)) is None
    assert not (tmp_path / "TV" / "US" / "2026_09_15_ETF_rs_3m.txt").exists()


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
    assert out.read_text() == "1. AAA\n"


# --- rank change vs previous snapshot ---


def _write_prev(tmp_path: Path, stamp: str, text: str) -> Path:
    d = tmp_path / "TV" / "US"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stamp}_ETF_rs_3m.txt"
    p.write_text(text)
    return p


def test_rank_delta_marker():
    assert etf_rs.rank_delta_marker(None, 1) == "新"
    assert etf_rs.rank_delta_marker(3, 3) == "="
    assert etf_rs.rank_delta_marker(5, 2) == "🟢↑3"
    assert etf_rs.rank_delta_marker(2, 7) == "🔴↓5"


def test_read_previous_ranks_uses_latest_file_before_today(tmp_path):
    _write_prev(tmp_path, "2026_09_10", "OLD - 旧\n")
    _write_prev(tmp_path, "2026_09_14", "BBB - 乙 | X、Y\nAAA - 甲\nCCC\n")
    _write_prev(tmp_path, "2026_09_15", "TODAY - 今天\n")  # same-day rerun: ignored
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) == {"BBB": 1, "AAA": 2, "CCC": 3}


def test_read_previous_ranks_none_when_no_earlier_file(tmp_path):
    _write_prev(tmp_path, "2026_09_15", "TODAY - 今天\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) is None
    assert etf_rs.read_previous_ranks(tmp_path / "nowhere", date(2026, 9, 15)) is None


def test_read_previous_ranks_parses_already_annotated_lines(tmp_path):
    _write_prev(tmp_path, "2026_09_14", "BBB 🟢↑2 - 乙 | X、Y\nAAA = - 甲\nCCC 新\n\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) == {"BBB": 1, "AAA": 2, "CCC": 3}


def test_read_previous_ranks_skips_leading_rank_number(tmp_path):
    # numbered snapshot (current format) and un-numbered legacy lines both parse
    _write_prev(tmp_path, "2026_09_14", "1. BBB 🟢↑2 - 乙 | X、Y\n2. AAA = - 甲\nCCC 新\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) == {"BBB": 1, "AAA": 2, "CCC": 3}


def test_write_ranking_annotates_rank_change_after_ticker(tmp_path):
    table = pd.DataFrame(
        {"raw_score": [0.2, 0.1, 0.0], "rs_percentile": [99, 50, 1]},
        index=["BBB", "AAA", "CCC"],
    )
    prev = {"AAA": 1, "BBB": 3}  # CCC absent → 新
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, {"BBB": "X、Y"},
                               tmp_path, date(2026, 9, 15), prev_ranks=prev)
    assert out.read_text() == "1. BBB 🟢↑2 - 乙 | X、Y\n2. AAA 🔴↓1 - 甲\n3. CCC 新\n"


def test_write_ranking_no_previous_means_no_markers(tmp_path):
    table = pd.DataFrame({"raw_score": [0.2], "rs_percentile": [99]}, index=["BBB"])
    out = etf_rs.write_ranking(table, {"BBB": "乙"}, {}, tmp_path, date(2026, 9, 15), prev_ranks=None)
    assert out.read_text() == "1. BBB - 乙\n"


def test_run_annotates_against_previous_day_file(tmp_path, monkeypatch):
    _write_prev(tmp_path, "2026_09_14", "AAA - 甲\nBBB - 乙\n")
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(20), "CCC": _kline(-3), "SPY": _kline(10)})
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15))
    assert out.read_text() == "1. BBB 🟢↑1 - 乙\n2. AAA 🔴↓1 - 甲\n3. CCC 新 - 丙\n"
    # same-day rerun compares against 09_14 again, not against today's own file
    out2 = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15))
    assert out2.read_text() == "1. BBB 🟢↑1 - 乙\n2. AAA 🔴↓1 - 甲\n3. CCC 新 - 丙\n"


# --- non-ETF symbols (crypto) via [etf_rs.yf_symbols] ---


def test_run_fetches_aliased_symbol_and_aligns_to_benchmark_days(tmp_path, monkeypatch):
    """BTCUSD is fetched as BTC-USD, reported as BTCUSD, and its 7-day-a-week
    rows are cut to the benchmark's trading days before scoring — the 3M
    offsets count rows, so weekend bars would shrink 63 rows to ~9 weeks."""
    seen: list[list[str]] = []
    days = pd.date_range(end="2026-09-11", periods=200, freq="D")  # incl. weekends
    # +50% only on the weekend bars: after alignment those rows are gone and
    # the series is flat → BTCUSD scores exactly like a flat ETF.
    btc = pd.DataFrame({
        "time_key": days,
        "close": [150.0 if d.weekday() >= 5 else 100.0 for d in days],
    })

    def fake_fetch(tickers, **kw):
        seen.append(list(tickers))
        return {"BTC-USD": btc, "AAA": _kline(5), "SPY": _kline(0)}

    monkeypatch.setattr(etf_rs, "_fetch_klines", fake_fetch)
    cfg = _cfg(tickers={"AAA": "甲", "BTCUSD": "比特币现货"},
               yf_symbols={"btcusd": "BTC-USD"})
    out = etf_rs.run_etf_rs(cfg, tmp_path, date(2026, 9, 15))
    assert seen == [["AAA", "BTC-USD", "SPY"]]
    assert out.read_text() == "1. AAA - 甲\n2. BTCUSD - 比特币现货\n"


def test_align_to_benchmark_days_keeps_unaliased_and_missing_benchmark():
    days = pd.date_range(end="2026-09-11", periods=10, freq="D")
    crypto = pd.DataFrame({"time_key": days, "close": range(10)})
    bench = _kline(0, n=5)
    out = etf_rs.align_to_benchmark_days({"X": crypto, "SPY": bench}, ["X"], "SPY")
    assert list(out["X"]["time_key"]) == list(bench["time_key"])
    # no benchmark → left untouched
    same = etf_rs.align_to_benchmark_days({"X": crypto}, ["X"], "SPY")
    assert same["X"] is crypto


# --- 1M (weekly-ladder) ranking alongside the 3M file ---


def _kline_two_phase(early_pct: float, late_pct: float, n: int = 90) -> pd.DataFrame:
    """Flat 100 for the first n-21 bars, then flat at 100·(1+early) for 16
    bars, then flat at 100·(1+early)·(1+late) for the last 5 bars. The 1M
    weekly ladder sees only the last 21 bars; the 3M window sees both legs."""
    first = 100.0 * (1 + early_pct / 100)
    last = first * (1 + late_pct / 100)
    closes = [100.0] * (n - 21) + [first] * 16 + [last] * 5
    return pd.DataFrame({"time_key": pd.bdate_range(end="2026-09-11", periods=n), "close": closes})


def test_run_writes_1m_file_next_to_3m_file(tmp_path, monkeypatch):
    # AAA: big 3M, flat last week; BBB: small 3M, strong last week → 3M says
    # AAA first, 1M says BBB first.
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline_two_phase(40, 0), "BBB": _kline_two_phase(2, 8),
        "CCC": _kline_two_phase(0, -5), "SPY": _kline_two_phase(5, 1)})
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15))
    assert out == tmp_path / "TV" / "US" / "2026_09_15_ETF_rs_3m.txt"
    assert out.read_text() == "1. AAA - 甲\n2. BBB - 乙\n3. CCC - 丙\n"
    one = tmp_path / "TV" / "US" / "2026_09_15_ETF_rs_1m.txt"
    assert one.read_text() == "1. BBB - 乙\n2. AAA - 甲\n3. CCC - 丙\n"


def test_1m_markers_compare_only_against_previous_1m_file(tmp_path, monkeypatch):
    d = tmp_path / "TV" / "US"
    d.mkdir(parents=True)
    (d / "2026_09_14_ETF_rs_3m.txt").write_text("1. CCC - 丙\n2. BBB - 乙\n3. AAA - 甲\n")
    (d / "2026_09_14_ETF_rs_1m.txt").write_text("1. AAA - 甲\n2. BBB - 乙\n")
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline_two_phase(40, 0), "BBB": _kline_two_phase(2, 8),
        "CCC": _kline_two_phase(0, -5), "SPY": _kline_two_phase(5, 1)})
    etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15))
    assert (d / "2026_09_15_ETF_rs_3m.txt").read_text() == \
        "1. AAA 🟢↑2 - 甲\n2. BBB = - 乙\n3. CCC 🔴↓2 - 丙\n"
    assert (d / "2026_09_15_ETF_rs_1m.txt").read_text() == \
        "1. BBB 🟢↑1 - 乙\n2. AAA 🔴↓1 - 甲\n3. CCC 新 - 丙\n"


def test_read_previous_ranks_3m_ignores_1m_files(tmp_path):
    d = tmp_path / "TV" / "US"
    d.mkdir(parents=True)
    (d / "2026_09_14_ETF_rs_1m.txt").write_text("1. BBB - 乙\n2. AAA - 甲\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) is None
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15), stem=etf_rs._STEM_1M) == {"BBB": 1, "AAA": 2}


def test_1m_failure_does_not_cost_3m_file(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(20), "CCC": _kline(-3), "SPY": _kline(10)})
    real = etf_rs.rank_etfs

    def boom(klines, tickers, benchmark, weights_bars=None):
        if weights_bars is not None:
            raise RuntimeError("1m exploded")
        return real(klines, tickers, benchmark, weights_bars)

    monkeypatch.setattr(etf_rs, "rank_etfs", boom)
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 15))
    assert out is not None and out.exists()
    assert not (tmp_path / "TV" / "US" / "2026_09_15_ETF_rs_1m.txt").exists()


def test_read_previous_ranks_3m_falls_back_to_legacy_stem(tmp_path):
    # files written before the 2026-10-15 rename (<date>_ETF_rs.txt) still
    # serve as the previous 3M snapshot; the newest file wins regardless of stem
    d = tmp_path / "TV" / "US"
    d.mkdir(parents=True)
    (d / "2026_09_13_ETF_rs_3m.txt").write_text("1. CCC - 丙\n")
    (d / "2026_09_14_ETF_rs.txt").write_text("1. BBB - 乙\n2. AAA - 甲\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) == {"BBB": 1, "AAA": 2}
    (d / "2026_09_14_ETF_rs_3m.txt").write_text("1. AAA - 甲\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 9, 15)) == {"AAA": 1}


def test_fetch_klines_fills_nan_last_close(monkeypatch):
    # 2026-10-22: every 10-21 Close was NaN at 10:00 HKT and the ranking ran
    # on 10-20 closes; the ETF fetch must ask for the intraday fill.
    seen = {}

    def fake_fetch(tickers, **kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(etf_rs.us_rs_3m, "fetch_us_klines_yf", fake_fetch)
    etf_rs._fetch_klines(["AAA", "SPY"])
    assert seen.get("fill_last_close") is True and seen.get("period") == "1y"


# --- holdings 1M continuation line (etf_holdings_rs) ---


def test_write_ranking_appends_indented_extra_line(tmp_path):
    table = pd.DataFrame({"raw_score": [0.2, 0.1], "rs_percentile": [99, 50]}, index=["AAA", "BBB"])
    out = etf_rs.write_ranking(table, {"AAA": "甲", "BBB": "乙"}, {"AAA": "X、Y"}, tmp_path,
                               date(2026, 10, 31), extras={"AAA": "1M强: X > Y"})
    assert out.read_text() == "1. AAA - 甲 | X、Y\n   1M强: X > Y\n2. BBB - 乙\n"


def test_write_ranking_without_extras_is_unchanged(tmp_path):
    table = pd.DataFrame({"raw_score": [0.2], "rs_percentile": [99]}, index=["AAA"])
    out = etf_rs.write_ranking(table, {"AAA": "甲"}, {}, tmp_path, date(2026, 10, 31), extras={})
    assert out.read_text() == "1. AAA - 甲\n"


def test_read_previous_ranks_ignores_indented_continuation_lines(tmp_path):
    d = tmp_path / "TV" / "US"
    d.mkdir(parents=True)
    (d / "2026_10_30_ETF_rs_3m.txt").write_text(
        "1. AAA - 甲\n   1M强: X > Y\n2. BBB - 乙\n   1M强: Z\n")
    assert etf_rs.read_previous_ranks(tmp_path, date(2026, 10, 31)) == {"AAA": 1, "BBB": 2}


def test_run_passes_extras_to_both_files(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(1), "SPY": _kline(0)})
    seen = {}

    def fake(cfg, etfs, bench, today, out):
        seen["etfs"] = list(etfs)
        seen["bench_rows"] = len(bench)
        return {"AAA": "1M强: X > Y"}
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", fake)
    etf_rs.run_etf_rs(_cfg(tickers={"AAA": "甲", "BBB": "乙"}), tmp_path, date(2026, 10, 31))
    for stem in ("ETF_rs_3m", "ETF_rs_1m"):
        txt = (tmp_path / "TV" / "US" / f"2026_10_31_{stem}.txt").read_text()
        assert "\n   1M强: X > Y\n" in txt, stem
    assert seen["etfs"] == ["AAA", "BBB"] and seen["bench_rows"] == 90


def test_run_writes_files_without_extras_when_step_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(0)})

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", boom)
    assert etf_rs.run_etf_rs(_cfg(tickers={"AAA": "甲"}), tmp_path, date(2026, 10, 31)) is not None
    txt = (tmp_path / "TV" / "US" / "2026_10_31_ETF_rs_3m.txt").read_text()
    assert "1M强" not in txt and txt.startswith("1. AAA")


def test_run_holdings_rs_false_skips_step(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(0)})

    def never(*a, **k):
        raise AssertionError("compute_extras called with holdings_rs=False")
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", never)
    assert etf_rs.run_etf_rs(_cfg(tickers={"AAA": "甲"}), tmp_path, date(2026, 10, 31),
                             holdings_rs=False) is not None
