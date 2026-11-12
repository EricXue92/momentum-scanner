"""Tests for etf_rs_leaders — the cumulative TradingView watchlist of the
strongest constituents of the top-N ETFs in the daily 1M ranking file."""

import tomllib
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import etf_holdings_rs
import etf_rs
import etf_rs_leaders
from cleanup import cleanup_old_outputs

TODAY = date(2026, 10, 31)


_REAL_FETCH_DAILY_BARS = etf_rs_leaders._fetch_daily_bars   # the autouse stub below replaces it


@pytest.fixture(autouse=True)
def _no_prune_network(monkeypatch):
    """prune_master's default fetch hits yfinance; the wiring tests below
    only need the add step."""
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda tickers: None)
    monkeypatch.setattr(etf_rs_leaders, "_fetch_rs_1m_table", lambda output_dir, today: None)

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
    # The trend + 1M RS entry gates are off here; their own tests turn them on.
    return {"enabled": True, "top_etfs": 2, "entry_above_sma": [],
            "entry_min_rs_percentile_1m": 0, **kw}


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
            "holdings": {}, "leaders": {"enabled": True, "top_etfs": 1, "entry_above_sma": [],
                                        "entry_min_rs_percentile_1m": 0}}


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
    monkeypatch.setattr(etf_rs_leaders, "run_leaders", boom)
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


def _bars(tail_closes: list[float], tail_lows: list[float], base_days: int = 56,
          volume: float = 10_000_000.0):
    """base_days flat closes at 100 (SMA50 = 100), then the given tail;
    constant daily volume (default 10M shares → ~$1B/day)."""
    import pandas as pd
    closes = [100.0] * base_days + list(tail_closes)
    lows = [99.0] * base_days + list(tail_lows)
    idx = pd.bdate_range(end="2026-10-30", periods=len(closes))
    return (pd.Series(closes, index=idx), pd.Series(lows, index=idx),
            pd.Series([volume] * len(closes), index=idx))


def _split(d):
    """No High series: these fixtures exercise the other gates, ADR% skips them."""
    return etf_rs_leaders.Bars({t: c for t, (c, _, _) in d.items()},
                               {t: l for t, (_, l, _) in d.items()},
                               {t: v for t, (_, _, v) in d.items()},
                               {})


def test_drop_when_two_days_below_and_close_breaks_first_day_low():
    closes, lows, _, _ = _split({"A": _bars([97, 94], [95, 93])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == ["A"]


def test_keep_when_second_close_holds_above_first_day_low():
    closes, lows, _, _ = _split({"A": _bars([97, 96], [95, 94])})     # 96 ≥ 95
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_keep_after_only_one_day_below():
    closes, lows, _, _ = _split({"A": _bars([97], [90])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_keep_when_close_equals_first_day_low():
    closes, lows, _, _ = _split({"A": _bars([97, 95], [95, 90])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_longer_streak_compares_with_the_first_break_day_not_yesterday():
    # 4 days below; last close 94.5 < first-day low 95 (drop) even though it
    # is above yesterday's low 93.
    closes, lows, _, _ = _split({"A": _bars([97, 96, 95.5, 94.5], [95, 94, 93, 92])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == ["A"]
    # last close 95.2 < yesterday's low 95.5 but ≥ first-day low 95 → keep
    closes, lows, _, _ = _split({"B": _bars([97, 96, 95.5, 95.2], [95, 94, 93, 95.5])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []


def test_streak_restarts_after_a_close_back_above_the_line():
    # below, above, below, below: the streak is the last two days only
    closes, lows, _, _ = _split({"A": _bars([97, 101, 97, 96.5], [80, 100, 96, 95])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == []            # 96.5 ≥ 96
    closes, lows, _, _ = _split({"A": _bars([97, 101, 97, 95.5], [80, 100, 96, 95])})
    assert etf_rs_leaders.find_break_drops(closes, lows) == ["A"]         # 95.5 < 96


def test_short_history_and_missing_low_are_kept():
    closes, lows, _, _ = _split({"A": _bars([97, 94], [95, 93], base_days=40)})   # < 50 bars
    assert etf_rs_leaders.find_break_drops(closes, lows) == []
    c, l, _ = _bars([97, 94], [95, 93])
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


# --- price / dollar-volume floors ---


def test_find_floor_drops_price_and_dollar_volume():
    closes, _, volumes, _ = _split({
        "CHEAP": _bars([19.5, 19.0], [19, 18]),                      # price < 20
        "THIN": _bars([101, 102], [100, 101], volume=500_000),       # ~$51M/day < 100M
        "OK": _bars([101, 102], [100, 101]),                         # $1B/day
        "NOVOL": _bars([101, 102], [100, 101]),
    })
    volumes.pop("NOVOL")
    assert etf_rs_leaders.find_floor_drops(closes, volumes, 20.0, 100_000_000) == ["CHEAP", "THIN"]
    assert etf_rs_leaders.find_floor_drops(closes, volumes, 0, 0) == []


def test_dollar_volume_is_the_20_day_average_of_close_times_volume():
    import pandas as pd
    idx = pd.bdate_range(end="2026-10-30", periods=60)
    close = pd.Series([50.0] * 60, index=idx)
    # 19 heavy days then one dead day: avg = 19/20 × 50 × 3M = $142.5M → keep
    vol = pd.Series([3_000_000.0] * 59 + [0.0], index=idx)
    assert etf_rs_leaders.find_floor_drops({"A": close}, {"A": vol}, 20.0, 100_000_000) == []
    # 20 dead days inside the window → $0 → drop, whatever came before
    vol2 = pd.Series([3_000_000.0] * 40 + [0.0] * 20, index=idx)
    assert etf_rs_leaders.find_floor_drops({"A": close}, {"A": vol2}, 20.0, 100_000_000) == ["A"]


def test_entry_gate_keeps_candidates_below_the_floors_out(tmp_path):
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CHEAP > THIN > GOOD > NODATA\n")
    bars = _split({
        "CHEAP": _bars([19.5, 19.0], [19, 18]),
        "THIN": _bars([101, 102], [100, 101], volume=500_000),
        "GOOD": _bars([101, 102], [100, 101]),
    })
    new = etf_rs_leaders.update_leaders(_cfg(min_price=20.0, min_dollar_volume=100_000_000),
                                        tmp_path, TODAY, ranking, bars=bars)
    assert new == ["GOOD", "NODATA"]          # no bars → pass through, like every other gate
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "GOOD,NODATA\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == "GOOD,NODATA\n"


def test_entry_gate_is_off_without_bars_or_with_zero_floors(tmp_path):
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CHEAP\n")
    assert etf_rs_leaders.update_leaders(_cfg(min_price=20.0), tmp_path, TODAY, ranking) == ["CHEAP"]
    bars = _split({"CHEAP": _bars([19.5, 19.0], [19, 18])})
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CHEAP2\n")
    bars = _split({"CHEAP2": _bars([19.5, 19.0], [19, 18])})
    assert etf_rs_leaders.update_leaders(_cfg(min_price=0, min_dollar_volume=0), tmp_path, TODAY,
                                        ranking, bars=bars) == ["CHEAP2"]


def test_prune_master_also_removes_existing_names_below_the_floors(tmp_path, monkeypatch):
    master = _master(tmp_path, ["CHEAP", "BREAK", "OK"])
    bars = _split({
        "CHEAP": _bars([101, 19.0], [100, 18]),                      # price floor
        "BREAK": _bars([97, 94], [95, 93]),                          # SMA50 first-break
        "OK": _bars([101, 102], [100, 101]),
    })
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: bars)
    cfg = _cfg(min_price=20.0, min_dollar_volume=100_000_000)
    assert etf_rs_leaders.prune_master(cfg, tmp_path, TODAY) == ["BREAK", "CHEAP"]
    assert master.read_text(encoding="utf-8") == "OK\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").read_text(encoding="utf-8") == "BREAK,CHEAP\n"


def test_run_leaders_downloads_once_for_master_plus_new_and_does_both_steps(tmp_path, monkeypatch):
    _master(tmp_path, ["OLD", "FADE"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: OLD > CHEAP > GOOD\n")
    asked = []

    def fetch(tickers):
        asked.append(list(tickers))
        return _split({
            "OLD": _bars([101, 102], [100, 101]),
            "FADE": _bars([101, 102], [100, 101], volume=100_000),  # $10M/day → floor
            "CHEAP": _bars([19.5, 19.0], [19, 18]),                 # rejected at entry
            "GOOD": _bars([101, 102], [100, 101]),
        })
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", fetch)
    cfg = _cfg(min_price=20.0, min_dollar_volume=100_000_000)
    etf_rs_leaders.run_leaders(cfg, tmp_path, TODAY, ranking)
    assert asked == [["OLD", "FADE", "CHEAP", "GOOD"]]
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "OLD,GOOD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == "GOOD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").read_text(encoding="utf-8") == "FADE\n"


def test_run_leaders_fetch_failure_adds_ungated_and_skips_the_prune(tmp_path, monkeypatch):
    _master(tmp_path, ["OLD"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CHEAP\n")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: None)
    etf_rs_leaders.run_leaders(_cfg(min_price=20.0), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "OLD,CHEAP\n"
    assert not (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").exists()


# --- dedup against the EOD master (state/eod_seen_US.txt) ---


def _seen(tmp_path: Path, names: list[str]) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "eod_seen_US.txt").write_text("".join(f"{n}\n" for n in names), encoding="utf-8")


def _healthy(tickers):
    return _split({t: _bars([101, 102], [100, 101]) for t in tickers})


def test_candidate_already_in_the_eod_master_is_not_added(tmp_path, monkeypatch):
    _seen(tmp_path, ["DUP"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: DUP > GOOD\n")
    asked = []
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars",
                        lambda t: asked.append(list(t)) or _healthy(t))
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "GOOD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == "GOOD\n"
    assert asked == [["GOOD"]]


def test_name_held_by_the_leaders_master_leaves_when_it_is_in_the_eod_master(tmp_path, monkeypatch):
    _master(tmp_path, ["KEEP", "DUP", "KEEP2"])
    _seen(tmp_path, ["DUP"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n")
    asked = []
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars",
                        lambda t: asked.append(list(t)) or _healthy(t))
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "KEEP,KEEP2\n"
    assert asked == [["KEEP", "KEEP2"]]
    # A duplicate is not a weak name: the first-break / floor record stays clean.
    assert not (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").exists()


def test_candidate_held_by_both_masters_does_not_come_back(tmp_path):
    _master(tmp_path, ["DUP"])
    _seen(tmp_path, ["DUP"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: DUP\n")
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "\n"
    assert not (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").exists()


def test_eod_dedup_needs_no_bars_and_still_runs_when_the_download_fails(tmp_path, monkeypatch):
    _master(tmp_path, ["KEEP", "DUP"])
    _seen(tmp_path, ["DUP"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: None)
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "KEEP\n"


def test_share_class_spellings_match_across_the_two_masters(tmp_path):
    # Finviz fills the EOD master with BRK-B; the ETF 1M line displays BRK.B.
    _seen(tmp_path, ["BRK-B"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: BRK.B > GOOD\n")
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "GOOD\n"


def test_disabled_step_leaves_the_leaders_master_alone(tmp_path):
    _master(tmp_path, ["DUP"])
    _seen(tmp_path, ["DUP"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n")
    etf_rs_leaders.run_leaders(_cfg(enabled=False), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "DUP\n"


def test_no_eod_master_means_no_dedup(tmp_path):
    # The cloud Pages build runs this chain in a scratch dir with no state/.
    _master(tmp_path, ["OLD"])
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: GOOD\n")
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "OLD,GOOD\n"


def test_run_leaders_does_not_download_when_nothing_to_check(tmp_path, monkeypatch):
    def never(t):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", never)
    ranking = _write_ranking(tmp_path, "1. E = - n\n")          # no 1M line, empty master
    etf_rs_leaders.run_leaders(_cfg(), tmp_path, TODAY, ranking)
    assert not (_tv(tmp_path) / "ETF_rs_leaders.txt").exists()


# --- ADR% floor ---


def _ohlc(ranges: list[float], close: float = 64.0):
    """Flat ``close`` with the given daily High − Low. 64 and powers-of-two
    ranges keep the percentages exact: range 2 → ADR 3.125%."""
    import pandas as pd
    idx = pd.bdate_range(end="2026-10-30", periods=len(ranges))
    c = pd.Series([close] * len(ranges), index=idx)
    r = pd.Series(ranges, index=idx)
    return c, c + r / 2, c - r / 2


def _adr_bars(**daily_range):
    """``Bars`` for names with a constant daily range over 60 sessions; price
    and $vol clear their floors ($640M/day), and flat closes never break SMA50."""
    import pandas as pd
    closes, highs, lows, vols = {}, {}, {}, {}
    for t, r in daily_range.items():
        closes[t], highs[t], lows[t] = _ohlc([r] * 60)
        vols[t] = pd.Series([10_000_000.0] * 60, index=closes[t].index)
    return etf_rs_leaders.Bars(closes, lows, vols, highs)


def _adr_of(ranges, floor):
    c, h, l = _ohlc(ranges)
    return etf_rs_leaders.find_adr_drops({"A": c}, {"A": h}, {"A": l}, floor)


def test_adr_floor_is_inclusive_and_divides_by_the_close():
    assert _adr_of([2.0] * 20, 3.125) == []        # 2 / 64 = exactly 3.125%
    assert _adr_of([2.0] * 20, 3.13) == ["A"]      # ÷ Low (63) would give 3.17% and keep it


def test_adr_averages_only_the_last_20_sessions():
    # 40 wide days (12.5%) then 20 narrow (1.5625%): only the narrow ones count
    assert _adr_of([8.0] * 40 + [1.0] * 20, 3.0) == ["A"]
    # 19 days of 6.25% and one dead last day (1%): mean 5.99%, not the last bar
    assert _adr_of([4.0] * 19 + [0.64], 3.0) == []


def test_adr_keeps_names_it_cannot_judge():
    import numpy as np
    assert _adr_of([1.0] * 19, 3.0) == []          # < 20 bars (fresh IPO)
    c, h, l = _ohlc([1.0] * 20)
    h.iloc[5] = np.nan                             # 19 usable bars
    assert etf_rs_leaders.find_adr_drops({"A": c}, {"A": h}, {"A": l}, 3.0) == []
    c, _, l = _ohlc([1.0] * 20)
    assert etf_rs_leaders.find_adr_drops({"A": c}, {}, {"A": l}, 3.0) == []   # no High series


def test_adr_floor_zero_is_off():
    assert _adr_of([1.0] * 20, 0) == []


def test_entry_gate_rejects_low_adr_at_the_default_3_5_percent(tmp_path):
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CALM > WILD\n")
    bars = _adr_bars(CALM=2.208, WILD=2.272)       # 3.45% / 3.55%
    assert etf_rs_leaders.update_leaders(_cfg(), tmp_path, TODAY, ranking, bars=bars) == ["WILD"]
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "WILD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == "WILD\n"


def test_prune_master_removes_existing_low_adr_names(tmp_path, monkeypatch):
    master = _master(tmp_path, ["CALM", "WILD"])
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: _adr_bars(CALM=1.0, WILD=4.0))
    assert etf_rs_leaders.prune_master(_cfg(), tmp_path, TODAY) == ["CALM"]
    assert master.read_text(encoding="utf-8") == "WILD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").read_text(encoding="utf-8") == "CALM\n"


def test_min_adr_percent_comes_from_config(tmp_path, monkeypatch):
    master = _master(tmp_path, ["CALM"])
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: _adr_bars(CALM=1.0))
    assert etf_rs_leaders.prune_master(_cfg(min_adr_percent=0), tmp_path, TODAY) == []
    assert master.read_text(encoding="utf-8") == "CALM\n"


def test_run_leaders_downloads_when_adr_is_the_only_active_gate(tmp_path, monkeypatch):
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CALM > WILD\n")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda t: _adr_bars(CALM=1.0, WILD=4.0))
    cfg = _cfg(min_price=0, min_dollar_volume=0, prune=False)
    etf_rs_leaders.run_leaders(cfg, tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "WILD\n"


def test_run_leaders_with_every_gate_off_does_not_download(tmp_path, monkeypatch):
    def never(t):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", never)
    ranking = _write_ranking(tmp_path, "1. E = - n\n   1M强: CALM\n")
    cfg = _cfg(min_price=0, min_dollar_volume=0, min_adr_percent=0, prune=False)
    etf_rs_leaders.run_leaders(cfg, tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "CALM\n"


@pytest.mark.parametrize("tickers", [["A"], ["A", "B"]])
def test_fetch_daily_bars_carries_the_high_series(monkeypatch, tickers):
    import pandas as pd
    import main
    idx = pd.bdate_range(end="2026-10-30", periods=3)
    one = pd.DataFrame({"High": [11.0, 12.0, 13.0], "Low": [9.0, 9.0, 9.0],
                        "Close": [10.0, 10.0, 10.0], "Volume": [1e6, 1e6, 1e6]}, index=idx)
    # Runtime shape: flat for one ticker, (ticker, field) MultiIndex for a batch
    frame = one if len(tickers) == 1 else pd.concat({t: one for t in tickers}, axis=1)
    monkeypatch.setattr(main, "_yf_download_with_retry", lambda *a, **k: frame)
    bars = _REAL_FETCH_DAILY_BARS(tickers)
    assert list(bars.highs["A"]) == [11.0, 12.0, 13.0]


# --- entry gate: price above SMA50 + SMA200, 1M RS percentile (market-wide) ---


def _series(values) -> pd.Series:
    return pd.Series([float(v) for v in values],
                     index=pd.bdate_range(end="2026-10-30", periods=len(values)))


# 260 bars. SMA50 / SMA200 at the last bar are given next to each shape.
_ABOVE_BOTH = _series(list(range(100, 360)))                       # rising
_BELOW_SMA200_ONLY = _series([200] * 200 + [100] * 59 + [110])     # SMA50 100.2, SMA200 150.3
_BELOW_SMA50_ONLY = _series([100] * 240 + [200] * 19 + [120])      # SMA50 138.4, SMA200 109.6


def _closes_bars(closes: dict[str, pd.Series]) -> etf_rs_leaders.Bars:
    return etf_rs_leaders.Bars(closes, {}, {}, {})


def _rs_table(**percentiles) -> pd.DataFrame:
    return pd.DataFrame({"rs_percentile_1m": percentiles})


_ENTRY_CFG = {"min_price": 0, "min_dollar_volume": 0, "min_adr_percent": 0,
              "entry_above_sma": [50, 200], "entry_min_rs_percentile_1m": 90}


def test_trend_gate_needs_the_close_above_both_averages():
    closes = {"UP": _ABOVE_BOTH, "U200": _BELOW_SMA200_ONLY, "U50": _BELOW_SMA50_ONLY}
    assert etf_rs_leaders.find_below_sma_drops(closes, [50, 200]) == ["U200", "U50"]


def test_trend_gate_is_strict_a_close_on_the_average_fails():
    assert etf_rs_leaders.find_below_sma_drops({"FLAT": _series([100] * 60)}, [50]) == ["FLAT"]


def test_trend_gate_does_not_judge_an_average_the_history_cannot_fill():
    # 60 rising bars: SMA50 is judged and passes, SMA200 cannot be computed.
    assert etf_rs_leaders.find_below_sma_drops({"IPO": _series(range(100, 160))}, [50, 200]) == []
    # 60 falling bars: SMA50 still catches it.
    assert etf_rs_leaders.find_below_sma_drops({"IPO": _series(range(160, 100, -1))}, [50, 200]) == ["IPO"]
    # Too short for either average → kept.
    assert etf_rs_leaders.find_below_sma_drops({"NEW": _series([100, 90, 80])}, [50, 200]) == []


def test_trend_gate_with_no_periods_is_off():
    assert etf_rs_leaders.find_below_sma_drops({"U50": _BELOW_SMA50_ONLY}, []) == []


def test_rs_gate_threshold_is_inclusive():
    table = _rs_table(STRONG=90, WEAK=89)
    assert etf_rs_leaders.find_rs_1m_drops(["STRONG", "WEAK"], table, 90) == ["WEAK"]


def test_rs_gate_keeps_names_it_cannot_judge():
    table = _rs_table(BLANK=float("nan"), WEAK=10)
    assert etf_rs_leaders.find_rs_1m_drops(["ABSENT", "BLANK"], table, 90) == []
    assert etf_rs_leaders.find_rs_1m_drops(["WEAK"], None, 90) == []
    assert etf_rs_leaders.find_rs_1m_drops(["WEAK"], pd.DataFrame({"rs_percentile": {"WEAK": 10}}), 90) == []
    assert etf_rs_leaders.find_rs_1m_drops(["WEAK"], table, 0) == []


def test_entry_gate_only_adds_names_passing_trend_and_1m_rs(tmp_path):
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: GOOD > U200 > U50 > WEAK\n")
    bars = _closes_bars({"GOOD": _ABOVE_BOTH, "U200": _BELOW_SMA200_ONLY,
                         "U50": _BELOW_SMA50_ONLY, "WEAK": _ABOVE_BOTH})
    table = _rs_table(GOOD=95, U200=95, U50=95, WEAK=50)
    new = etf_rs_leaders.update_leaders(_ENTRY_CFG, tmp_path, TODAY, ranking, bars=bars, rs_table=table)
    assert new == ["GOOD"]
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "GOOD\n"
    assert (_tv(tmp_path) / "2026_10_31_ETF_rs_new.txt").read_text(encoding="utf-8") == "GOOD\n"


def test_rs_gate_still_applies_when_the_bar_download_failed(tmp_path):
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: GOOD > WEAK\n")
    new = etf_rs_leaders.update_leaders(_ENTRY_CFG, tmp_path, TODAY, ranking, bars=None,
                                        rs_table=_rs_table(GOOD=95, WEAK=50))
    assert new == ["GOOD"]


def test_entry_gates_are_not_a_retention_rule_for_the_existing_master(tmp_path, monkeypatch):
    """A name already on the list only leaves through the floors or the SMA50
    first-break rule: under SMA200 or weak on 1M RS alone it stays."""
    _tv(tmp_path).mkdir(parents=True)
    (_tv(tmp_path) / "ETF_rs_leaders.txt").write_text("HELD,U200\n", encoding="utf-8")
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: HELD > U200\n")
    bars = _closes_bars({"HELD": _ABOVE_BOTH, "U200": _BELOW_SMA200_ONLY})
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda tickers: bars)
    monkeypatch.setattr(etf_rs_leaders, "_fetch_rs_1m_table",
                        lambda output_dir, today: _rs_table(HELD=10, U200=10))
    etf_rs_leaders.run_leaders(_ENTRY_CFG, tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "HELD,U200\n"
    assert not (_tv(tmp_path) / "2026_10_31_ETF_rs_pruned.txt").exists()


def test_run_leaders_passes_the_1m_table_to_the_entry_gate(tmp_path, monkeypatch):
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: GOOD > WEAK\n")
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars",
                        lambda tickers: _closes_bars({"GOOD": _ABOVE_BOTH, "WEAK": _ABOVE_BOTH}))
    monkeypatch.setattr(etf_rs_leaders, "_fetch_rs_1m_table",
                        lambda output_dir, today: _rs_table(GOOD=95, WEAK=50))
    etf_rs_leaders.run_leaders(_ENTRY_CFG, tmp_path, TODAY, ranking)
    assert (_tv(tmp_path) / "ETF_rs_leaders.txt").read_text(encoding="utf-8") == "GOOD\n"


def test_run_leaders_skips_the_table_fetch_when_nothing_is_new_or_the_gate_is_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(etf_rs_leaders, "_fetch_rs_1m_table",
                        lambda output_dir, today: calls.append(today))
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: HELD\n")
    _tv(tmp_path).joinpath("ETF_rs_leaders.txt").write_text("HELD\n", encoding="utf-8")
    etf_rs_leaders.run_leaders(_ENTRY_CFG, tmp_path, TODAY, ranking)          # nothing new
    assert calls == []
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: FRESH\n")
    etf_rs_leaders.run_leaders({**_ENTRY_CFG, "entry_min_rs_percentile_1m": 0}, tmp_path, TODAY, ranking)
    assert calls == []


def test_run_leaders_downloads_when_the_trend_gate_is_the_only_active_gate(tmp_path, monkeypatch):
    calls = []
    def fake(tickers):
        calls.append(list(tickers))
        return None
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", fake)
    ranking = _write_ranking(tmp_path, "1. X = - x | a\n   1M强: FRESH\n")
    cfg = {**_ENTRY_CFG, "entry_min_rs_percentile_1m": 0, "prune": False}
    etf_rs_leaders.run_leaders(cfg, tmp_path, TODAY, ranking)
    assert calls == [["FRESH"]]


def test_fetch_rs_1m_table_is_soft_on_a_loader_failure(tmp_path, monkeypatch, caplog):
    monkeypatch.undo()                     # the real wrapper, not the autouse stub
    def boom(output_dir, today):
        raise OSError("disk full")
    monkeypatch.setattr(etf_rs_leaders.us_rs_3m, "build_3m_table", boom)
    with caplog.at_level("WARNING", logger="momentum_scanner"):
        assert etf_rs_leaders._fetch_rs_1m_table(tmp_path, TODAY) is None
    assert "1M RS" in caplog.text


def test_fetch_rs_1m_table_warns_when_the_table_has_no_1m_column(tmp_path, monkeypatch, caplog):
    monkeypatch.undo()
    old = pd.DataFrame({"rs_percentile": {"AAA": 95}})
    monkeypatch.setattr(etf_rs_leaders.us_rs_3m, "build_3m_table", lambda output_dir, today: old)
    with caplog.at_level("WARNING", logger="momentum_scanner"):
        assert etf_rs_leaders._fetch_rs_1m_table(tmp_path, TODAY) is None
    assert "rs_percentile_1m" in caplog.text


def test_fetch_daily_bars_asks_for_enough_history_for_sma200(monkeypatch):
    seen = {}
    idx = pd.bdate_range(end="2026-10-30", periods=3)
    frame = pd.DataFrame({f: [1.0, 2.0, 3.0] for f in ("Open", "High", "Low", "Close", "Volume")}, index=idx)
    def fake_download(tickers, **kw):
        seen.update(kw)
        return frame
    monkeypatch.setattr("main._yf_download_with_retry", fake_download)
    _REAL_FETCH_DAILY_BARS(["AAA"])
    assert seen["period"] == "1y"


def test_entry_gate_config_ships_with_the_requested_thresholds():
    with open(Path(__file__).resolve().parents[1] / "config.toml", "rb") as f:
        leaders = tomllib.load(f)["etf_rs"]["leaders"]
    assert leaders["entry_above_sma"] == [50, 200]
    assert leaders["entry_min_rs_percentile_1m"] == 90


def test_entry_gate_defaults_match_the_config():
    assert etf_rs_leaders.DEFAULT_ENTRY_ABOVE_SMA == (50, 200)
    assert etf_rs_leaders.DEFAULT_ENTRY_MIN_RS_1M == 90


def test_fetch_daily_bars_requests_class_shares_in_yahoo_form(monkeypatch):
    """`BRK.B` (display form, as held in the master) is `BRK-B` on Yahoo.
    Sent unconverted it came back with no bars, so it passed every entry gate
    and the prune could never judge it (it sat in the master on 2026-11-12)."""
    import main
    idx = pd.bdate_range(end="2026-10-30", periods=3)
    one = pd.DataFrame({"High": [11.0, 12.0, 13.0], "Low": [9.0, 9.0, 9.0],
                        "Close": [10.0, 10.0, 10.0], "Volume": [1e6, 1e6, 1e6]}, index=idx)
    asked = []

    def fake_download(tickers, **kw):
        asked.append(list(tickers))
        return pd.concat({t: one for t in tickers}, axis=1)

    monkeypatch.setattr(main, "_yf_download_with_retry", fake_download)
    bars = _REAL_FETCH_DAILY_BARS(["BRK.B", "NVDA"])
    assert asked == [["BRK-B", "NVDA"]]
    assert set(bars.closes) == {"BRK.B", "NVDA"}       # keyed by the display form
    assert list(bars.highs["BRK.B"]) == [11.0, 12.0, 13.0]
