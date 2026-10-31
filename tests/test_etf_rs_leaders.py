"""Tests for etf_rs_leaders — the cumulative TradingView watchlist of the
strongest constituents of the top-N ETFs in the daily 1M ranking file."""

from datetime import date
from pathlib import Path

import pytest

import etf_holdings_rs
import etf_rs
import etf_rs_leaders
from cleanup import cleanup_old_outputs

TODAY = date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _no_prune_network(monkeypatch):
    """prune_master's default fetch hits yfinance; the wiring tests below
    only need the add step."""
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda tickers: None)

RANKING = """\
1. XLV 🟢↑2 - 医疗保健 | LLY、JNJ
   1M强: REGN > LLY > GILD > AMGN > VRTX
2. OIH 新 - 油服 | SLB、BKR
   1M强: RIG > VAL > PTEN > NBR > WFRD
3. IBIT = - 比特币 | 不适用
4. XLE 🔴↓1 - 能源 | XOM
   1M强: APA > COP > HAL > TRGP > LLY
5. DRAM = - 存储 | MU
   1M强: MU (前十大)
"""


def _tv(tmp_path: Path) -> Path:
    return tmp_path / "TV" / "US"


def _write_ranking(tmp_path: Path, text: str = RANKING, day: date = TODAY) -> Path:
    _tv(tmp_path).mkdir(parents=True, exist_ok=True)
    p = _tv(tmp_path) / f"{day:%Y_%m_%d}_ETF_rs_1m.txt"
    p.write_text(text, encoding="utf-8")
    return p


# --- parsing ---


@pytest.mark.parametrize("top,source", [
    (["REGN", "LLY", "BRK.B"], "stockanalysis"),
    (["MU"], "yf_top10"),
    (["A", "B", "C", "D", "E"], "stockanalysis-cache:2026-10-30"),
])
def test_parse_extra_inverts_format_extra(top, source):
    line = etf_holdings_rs.format_extra(top, source)
    assert etf_rs_leaders.parse_extra(line) == top
    assert etf_rs_leaders.parse_extra("   " + line) == top   # as written in the .txt


def test_parse_extra_rejects_other_lines():
    assert etf_rs_leaders.parse_extra("1. XLV 🟢↑2 - 医疗保健 | LLY、JNJ") == []
    assert etf_rs_leaders.parse_extra("") == []


def test_parse_ranking_pairs_each_etf_row_with_its_continuation_line():
    assert etf_rs_leaders.parse_ranking(RANKING) == [
        ("XLV", ["REGN", "LLY", "GILD", "AMGN", "VRTX"]),
        ("OIH", ["RIG", "VAL", "PTEN", "NBR", "WFRD"]),
        ("IBIT", []),
        ("XLE", ["APA", "COP", "HAL", "TRGP", "LLY"]),
        ("DRAM", ["MU"]),
    ]


def test_candidates_take_the_top_etfs_in_order_and_dedupe():
    entries = etf_rs_leaders.parse_ranking(RANKING)
    # IBIT is inside the top 4 and contributes nothing — no back-fill from #5.
    assert etf_rs_leaders.candidates(entries, top_etfs=4) == [
        "REGN", "LLY", "GILD", "AMGN", "VRTX",
        "RIG", "VAL", "PTEN", "NBR", "WFRD",
        "APA", "COP", "HAL", "TRGP",          # LLY already in
    ]
    assert etf_rs_leaders.candidates(entries, top_etfs=1) == ["REGN", "LLY", "GILD", "AMGN", "VRTX"]


def test_load_master_accepts_commas_and_newlines_and_dedupes(tmp_path):
    p = tmp_path / "m.txt"
    p.write_text("A,B, C\nD,,A\n", encoding="utf-8")
    assert etf_rs_leaders.load_master(p) == ["A", "B", "C", "D"]
    assert etf_rs_leaders.load_master(tmp_path / "missing.txt") == []


# --- update_leaders ---


def _cfg(**kw):
    return {"enabled": True, "top_etfs": 2, **kw}


def test_first_run_writes_master_and_new_file_comma_separated(tmp_path):
    ranking = _write_ranking(tmp_path)
    new = etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert new == ["REGN", "LLY", "GILD", "AMGN", "VRTX", "RIG", "VAL", "PTEN", "NBR", "WFRD"]
    master = (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8")
    assert master == "REGN,LLY,GILD,AMGN,VRTX,RIG,VAL,PTEN,NBR,WFRD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == master


def test_next_day_appends_only_new_names_and_dates_the_new_file(tmp_path):
    etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, _write_ranking(tmp_path))
    nxt = date(2026, 11, 3)
    ranking = _write_ranking(tmp_path, RANKING.replace("REGN > LLY > GILD", "NEW1 > LLY > NEW2"), nxt)
    assert etf_rs_leaders.update_leaders(_cfg(), tmp_path, nxt, ranking) == ["NEW1", "NEW2"]
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == (
        "REGN,LLY,GILD,AMGN,VRTX,RIG,VAL,PTEN,NBR,WFRD,NEW1,NEW2\n")
    assert (_tv(tmp_path) / "2026_11_03_ETF_rs_new.txt").read_text(encoding="utf-8") == "NEW1,NEW2\n"
    # the earlier day's file is untouched
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8").startswith("REGN,")


def test_nothing_new_writes_no_new_file_and_leaves_master_alone(tmp_path):
    etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, _write_ranking(tmp_path))
    master = _tv(tmp_path) / "ETF_rs_leaders.txt"
    stamp = master.stat().st_mtime_ns
    nxt = date(2026, 11, 3)
    assert etf_rs_leaders.update_leaders(_cfg(), tmp_path, nxt, _write_ranking(tmp_path, day=nxt)) == []
    assert not (_tv(tmp_path) / "2026_11_03_ETF_rs_new.txt").exists()
    assert master.stat().st_mtime_ns == stamp


def test_same_day_rerun_keeps_the_first_new_file_but_still_grows_the_master(tmp_path):
    etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, _write_ranking(tmp_path))
    ranking = _write_ranking(tmp_path, RANKING.replace("RIG > VAL", "LATE > VAL"))
    assert etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, ranking) == ["LATE"]
    new_file = (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8")
    assert "LATE" not in new_file and new_file.startswith("REGN,")
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8").endswith(",WFRD,LATE\n")


def test_master_order_survives_a_hand_edited_newline_master(tmp_path):
    _tv(tmp_path).mkdir(parents=True)
    (_tv(tmp_path) / "ETF_rs_leaders.txt").write_text("LLY\nRIG\n", encoding="utf-8")
    new = etf_rs_leaders.update_leaders(_cfg(top_etfs=1), tmp_path, TODAY, _write_ranking(tmp_path))
    assert new == ["REGN", "GILD", "AMGN", "VRTX"]
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "LLY,RIG,REGN,GILD,AMGN,VRTX\n"


def test_disabled_or_missing_ranking_does_nothing(tmp_path):
    ranking = _write_ranking(tmp_path)
    assert etf_rs_leaders.update_leaders(_cfg(enabled=False), tmp_path, TODAY, ranking) == []
    assert etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, None) == []
    assert etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, tmp_path / "nope.txt") == []
    assert not (_tv(tmp_path) / "ETF_rs_leaders.txt").exists()


def test_top_etfs_defaults_to_ten(tmp_path):
    rows = "".join(f"{i}. E{i} = - n{i}\n   1M强: S{i}\n" for i in range(1, 13))
    ranking = _write_ranking(tmp_path, rows)
    new = etf_rs_leaders.update_leaders({"enabled": True}, tmp_path, TODAY, ranking)
    assert new == [f"S{i}" for i in range(1, 11)]


# --- run_etf_rs wiring (soft step after the 1M file) ---


def _kline(bias: float, n: int = 90):
    import pandas as pd
    closes = [100.0 * (1 + (bias / 100.0 + 0.001 * (i % 3))) ** (i / 10) for i in range(n)]
    return pd.DataFrame({"time_key": pd.bdate_range(end="2026-10-30", periods=n), "close": closes})


def _etf_cfg():
    return {"enabled": True, "benchmark": "SPY", "tickers": {"AAA": "甲", "BBB": "乙"},
            "holdings": {}, "leaders": {"enabled": True, "top_etfs": 1}}


def test_run_etf_rs_grows_the_leaders_master_from_the_1m_top_etf(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {
        "AAA": _kline(5), "BBB": _kline(1), "SPY": _kline(0)})
    monkeypatch.setattr(etf_holdings_rs, "compute_extras",
                        lambda *a, **k: {"AAA": "1M强: X > Y", "BBB": "1M强: Z"})
    etf_rs.run_etf_rs(_etf_cfg(), tmp_path, TODAY)
    first = (_tv(tmp_path) / "2026_10_31_ETF_rs_1m.txt").read_text().split()[1]
    expected = {"AAA": "X,Y\n", "BBB": "Z\n"}[first]
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == expected
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == expected


def test_run_etf_rs_leaders_failure_costs_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(0)})
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: {"AAA": "1M强: X"})

    def boom(*a, **k):
        raise RuntimeError("leaders exploded")
    monkeypatch.setattr(etf_rs_leaders, "update_leaders", boom)
    out = etf_rs.run_etf_rs(_etf_cfg(), tmp_path, TODAY)
    assert out is not None and (_tv(tmp_path) / "2026_10_31_ETF_rs_1m.txt").exists()
    assert (tmp_path / "Reports" / "ETF" / "etf_rs_trend.html").exists()


# --- retention ---


def test_cleanup_ages_new_files_but_never_the_master(tmp_path):
    tv = _tv(tmp_path)
    tv.mkdir(parents=True)
    for name in ("ETF_rs_leaders.txt", "2026_10_31_ETF_rs_new.txt", "2026_10_20_ETF_rs_new.txt"):
        (tv / name).write_text("A\n")
    cleanup_old_outputs(tmp_path, date(2026, 10, 31))
    assert (tv / "ETF_rs_leaders.txt").exists()
    assert (tv / "2026_10_31_ETF_rs_new.txt").exists()
    assert not (tv / "2026_10_20_ETF_rs_new.txt").exists()


# --- SMA50 first-break prune ---


def _bars(tail_closes: list[float], tail_lows: list[float], base_days: int = 56):
    """base_days flat closes at 100 (SMA50 = 100), then the given tail."""
    import pandas as pd
    closes = [100.0] * base_days + list(tail_closes)
    lows = [99.0] * base_days + list(tail_lows)
    idx = pd.bdate_range(end="2026-10-30", periods=len(closes))
    return pd.Series(closes, index=idx), pd.Series(lows, index=idx)


def _split(d):
    return {t: c for t, (c, _) in d.items()}, {t: l for t, (_, l) in d.items()}


def test_drop_when_two_days_below_and_close_breaks_first_day_low():
    closes, lows = _split({"A": _bars([97, 94], [95, 93])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == ["A"]


def test_keep_when_second_close_holds_above_first_day_low():
    closes, lows = _split({"A": _bars([97, 96], [95, 94])})     # 96 ≥ 95
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_keep_after_only_one_day_below():
    closes, lows = _split({"A": _bars([97], [90])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_keep_when_close_equals_first_day_low():
    closes, lows = _split({"A": _bars([97, 95], [95, 90])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_longer_streak_compares_with_the_first_break_day_not_yesterday():
    # 4 days below; last close 94.5 < first-day low 95 (drop) even though it
    # is above yesterday's low 93.
    closes, lows = _split({"A": _bars([97, 96, 95.5, 94.5], [95, 94, 93, 92])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == ["A"]
    # last close 95.2 < yesterday's low 95.5 but ≥ first-day low 95 → keep
    closes, lows = _split({"B": _bars([97, 96, 95.5, 95.2], [95, 94, 93, 95.5])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_streak_restarts_after_a_close_back_above_the_line():
    # below, above, below, below: the streak is the last two days only
    closes, lows = _split({"A": _bars([97, 101, 97, 96.5], [80, 100, 96, 95])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []            # 96.5 ≥ 96
    closes, lows = _split({"A": _bars([97, 101, 97, 95.5], [80, 100, 96, 95])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == ["A"]         # 95.5 < 96


def test_short_history_and_missing_low_are_kept():
    closes, lows = _split({"A": _bars([97, 94], [95, 93], base_days=40)})   # < 50 bars
    assert etf_rs_leaders.find_break_drops(closes, lows) == []
    c, l = _bars([97, 94], [95, 93])
    assert etf_rs_leaders.find_break_drops({"A": c}, {}) == []               # no low series
    import numpy as np
    l.iloc[-2] = np.nan
    assert etf_rs_leaders.find_break_drops({"A": c}, {"A": l}) == []         # NaN first-day low


def _master(tmp_path, names):
    _tv(tmp_path).mkdir(parents=True, exist_ok=True)
    (_tv(tmp_path) / "ETF_rs_leaders.txt").write_text(",".join(names) + "\n", encoding="utf-8")
    return _tv(tmp_path) / "ETF_rs_leaders.txt"


def test_prune_master_removes_drops_and_writes_the_pruned_file(tmp_path, monkeypatch):
    master = _master(tmp_path, ["A", "B", "C"])
    bars = {"A": _bars([97, 94], [95, 93]), "B": _bars([97, 96], [95, 94])}   # C: no data
    asked = []

    def fetch(tickers):
        asked.append(list(tickers))
        return _split(bars)
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", fetch)
    assert etf_rs_leaders.prune_master(_cfg(), tmp_path, TODAY) == ["A"]
    assert asked == [["A", "B", "C"]]
    assert master.read_text(encoding="utf-8") == "B,C\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").read_text(encoding="utf-8") == "A\n"


def test_prune_master_same_day_rerun_merges_the_pruned_file(tmp_path, monkeypatch):
    _master(tmp_path, ["A", "B"])
    (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").write_text("Z\n", encoding="utf-8")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars",
                        lambda t: _split({"A": _bars([97, 94], [95, 93])}))
    assert etf_rs_leaders.prune_master(_cfg(), tmp_path, TODAY) == ["A"]
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").read_text(encoding="utf-8") == "A,Z\n"


def test_prune_master_fetch_failure_or_nothing_to_drop_changes_nothing(tmp_path, monkeypatch):
    master = _master(tmp_path, ["A", "B"])
    stamp = master.stat().st_mtime_ns
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: None)
    assert etf_rs_leaders.prune_master(_cfg(), tmp_path, TODAY) == []
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars",
                        lambda t: _split({"A": _bars([97, 96], [95, 94])}))
    assert etf_rs_leaders.prune_master(_cfg(), tmp_path, TODAY) == []
    assert master.stat().st_mtime_ns == stamp
    assert not (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").exists()


def test_prune_master_skips_fetch_when_master_is_empty_or_prune_disabled(tmp_path, monkeypatch):
    def never(t):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", never)
    assert etf_rs_leaders.prune_master(_cfg(), tmp_path, TODAY) == []           # no master
    _master(tmp_path, ["A"])
    assert etf_rs_leaders.prune_master(_cfg(prune=False), tmp_path, TODAY) == []


def test_run_etf_rs_prunes_right_after_adding_todays_names(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: {"AAA": _kline(5), "SPY": _kline(0)})
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: {"AAA": "1M强: X > Y"})
    _master(tmp_path, ["OLD"])
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: _split({
        "OLD": _bars([97, 94], [95, 93]), "Y": _bars([97, 94], [95, 93]), "X": _bars([101, 102], [99, 100])}))
    etf_rs.run_etf_rs(_etf_cfg(), tmp_path, TODAY)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "X\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == "X,Y\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").read_text(encoding="utf-8") == "OLD,Y\n"


def test_cleanup_ages_the_pruned_file(tmp_path):
    tv = _tv(tmp_path)
    tv.mkdir(parents=True)
    (tv / "2026_10_20_ETF_rs_pruned.txt").write_text("A\n")
    cleanup_old_outputs(tmp_path, date(2026, 10, 31))
    assert not (tv / "2026_10_20_ETF_rs_pruned.txt").exists()
