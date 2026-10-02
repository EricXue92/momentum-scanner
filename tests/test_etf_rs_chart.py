"""Tests for etf_rs_chart — per-day 3M RS score history + the trend HTML.

yfinance is never hit: klines are synthetic, the fetch layer is monkeypatched.
"""

import json
import math
import random
import re
from datetime import date

import pandas as pd

import etf_rs
import etf_rs_chart

_END = "2026-09-11"


def _walk(seed: int, n: int = 120, end: str = _END) -> pd.DataFrame:
    """Seeded random walk so every day has a distinct score."""
    rng = random.Random(seed)
    closes, px = [], 100.0
    for _ in range(n):
        px *= 1 + rng.uniform(-0.03, 0.03)
        closes.append(px)
    return pd.DataFrame({
        "time_key": pd.bdate_range(end=end, periods=n),
        "close": closes,
    })


def _klines() -> dict[str, pd.DataFrame]:
    return {"AAA": _walk(1), "BBB": _walk(2), "CCC": _walk(3), "SPY": _walk(9)}


def _truncate(klines: dict[str, pd.DataFrame], day: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {t: df[df["time_key"] <= day] for t, df in klines.items()}


# --- score_history (pure logic) ---


def test_history_last_day_matches_todays_ranking():
    klines = _klines()
    hist = etf_rs_chart.score_history(klines, ["AAA", "BBB", "CCC"], "SPY")
    table = etf_rs.rank_etfs(klines, ["AAA", "BBB", "CCC"], "SPY")
    assert hist.index[-1] == pd.Timestamp(_END)
    for t in ["AAA", "BBB", "CCC"]:
        assert math.isclose(hist[t].iloc[-1], table.loc[t, "raw_score"], abs_tol=1e-12)


def test_history_every_day_equals_ranking_on_klines_truncated_to_that_day():
    klines = _klines()
    hist = etf_rs_chart.score_history(klines, ["AAA", "BBB", "CCC"], "SPY")
    for day in (hist.index[0], hist.index[20], hist.index[-2]):
        table = etf_rs.rank_etfs(_truncate(klines, day), ["AAA", "BBB", "CCC"], "SPY")
        for t in ["AAA", "BBB", "CCC"]:
            assert math.isclose(hist.loc[day, t], table.loc[t, "raw_score"], abs_tol=1e-12)


def test_history_starts_on_the_first_day_the_benchmark_is_scorable():
    hist = etf_rs_chart.score_history(_klines(), ["AAA"], "SPY")
    # 120 bars, the 3M score needs 64 → 57 scorable days
    assert len(hist) == 57
    assert hist.index[0] == pd.bdate_range(end=_END, periods=120)[63]


def test_short_history_ticker_is_nan_until_its_64th_bar():
    klines = _klines()
    klines["NEW"] = _walk(4, n=80)
    hist = etf_rs_chart.score_history(klines, ["AAA", "NEW"], "SPY")
    assert hist["NEW"].notna().sum() == 17  # 80 - 63
    assert hist["NEW"].iloc[:-17].isna().all()
    assert hist["AAA"].notna().all()


def test_never_scorable_or_unfetched_ticker_is_left_out():
    klines = _klines()
    klines["TINY"] = _walk(5, n=40)
    hist = etf_rs_chart.score_history(klines, ["AAA", "TINY", "GONE"], "SPY")
    assert list(hist.columns) == ["AAA"]


def test_ticker_missing_a_day_carries_its_last_bar_like_the_daily_run():
    klines = _klines()
    gap_day = klines["AAA"]["time_key"].iloc[-5]
    klines["AAA"] = klines["AAA"][klines["AAA"]["time_key"] != gap_day].reset_index(drop=True)
    hist = etf_rs_chart.score_history(klines, ["AAA", "BBB"], "SPY")
    table = etf_rs.rank_etfs(_truncate(klines, gap_day), ["AAA", "BBB"], "SPY")
    assert math.isclose(hist.loc[gap_day, "AAA"], table.loc["AAA", "raw_score"], abs_tol=1e-12)


def test_missing_benchmark_falls_back_to_absolute_scores():
    klines = _klines()
    del klines["SPY"]
    hist = etf_rs_chart.score_history(klines, ["AAA", "BBB"], "SPY")
    table = etf_rs.rank_etfs(klines, ["AAA", "BBB"], "SPY")
    assert len(hist) == 57
    assert math.isclose(hist["AAA"].iloc[-1], table.loc["AAA", "raw_score"], abs_tol=1e-12)


def test_percentile_history_matches_the_daily_table_on_every_day():
    klines = _klines()
    klines["DDD"], klines["EEE"] = _walk(5), _walk(6)
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    hist = etf_rs_chart.score_history(klines, tickers, "SPY")
    pct = etf_rs_chart.percentile_history(hist)
    for day in (hist.index[0], hist.index[30], hist.index[-1]):
        table = etf_rs.rank_etfs(_truncate(klines, day), tickers, "SPY")
        assert {t: pct.loc[day, t] for t in tickers} == table["rs_percentile"].to_dict()


def test_percentile_history_ranks_only_the_tickers_scored_that_day():
    klines = _klines()
    klines["NEW"] = _walk(4, n=80)
    hist = etf_rs_chart.score_history(klines, ["AAA", "BBB", "NEW"], "SPY")
    pct = etf_rs_chart.percentile_history(hist)
    first = pct.iloc[0]
    assert pd.isna(first["NEW"])
    assert sorted([first["AAA"], first["BBB"]]) == [50, 99]  # 2 scored: 1/2 and 2/2 of 99
    assert sorted(pct.iloc[-1].tolist()) == [33, 66, 99]


def test_max_days_keeps_the_most_recent_rows():
    hist = etf_rs_chart.score_history(_klines(), ["AAA"], "SPY", max_days=10)
    assert len(hist) == 10 and hist.index[-1] == pd.Timestamp(_END)


def test_nothing_scorable_returns_empty_frame():
    assert etf_rs_chart.score_history({}, ["AAA"], "SPY").empty
    assert etf_rs_chart.score_history({"AAA": _walk(1, n=30)}, ["AAA"], "SPY").empty


def test_tz_aware_time_keys_are_accepted():
    klines = _klines()
    for df in klines.values():
        df["time_key"] = df["time_key"].dt.tz_localize("America/New_York")
    hist = etf_rs_chart.score_history(klines, ["AAA"], "SPY")
    assert hist.index[-1] == pd.Timestamp(_END) and hist.index.tz is None


# --- render / write ---


def _payload(html: str) -> dict:
    m = re.search(r'<script id="etf-rs-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "data block missing"
    return json.loads(m.group(1))


def test_render_embeds_series_in_given_order_with_names_holdings_and_percent_scores():
    hist = etf_rs_chart.score_history(_klines(), ["BBB", "AAA"], "SPY")
    html = etf_rs_chart.render_html(
        hist, {"AAA": "甲", "BBB": "乙"}, {"AAA": "X、Y"}, "SPY", date(2026, 9, 12),
    )
    data = _payload(html)
    assert data["generated"] == "2026-09-12"
    assert data["benchmark"] == "SPY"
    assert data["dates"][-1] == _END and len(data["dates"]) == 57
    assert [s["t"] for s in data["series"]] == ["BBB", "AAA"]
    assert data["series"][1] == {
        "t": "AAA", "name": "甲", "holdings": "X、Y",
        "s": [round(v * 100, 2) for v in hist["AAA"]],
        "p": [int(v) for v in etf_rs_chart.percentile_history(hist)["AAA"]],
    }
    assert data["series"][0]["holdings"] == ""


def test_render_writes_unscored_days_as_null():
    klines = _klines()
    klines["NEW"] = _walk(4, n=80)
    hist = etf_rs_chart.score_history(klines, ["AAA", "NEW"], "SPY")
    data = _payload(etf_rs_chart.render_html(hist, {}, {}, "SPY", date(2026, 9, 12)))
    new = data["series"][1]
    assert new["s"][0] is None and new["s"][-1] is not None
    assert new["p"][0] is None and new["p"][-1] in (50, 99)


def test_render_cannot_be_broken_out_of_by_config_text():
    hist = etf_rs_chart.score_history(_klines(), ["AAA"], "SPY")
    html = etf_rs_chart.render_html(
        hist, {"AAA": "</script><script>alert(1)</script>"}, {}, "SPY", date(2026, 9, 12),
    )
    assert "</script><script>alert(1)" not in html
    assert _payload(html)["series"][0]["name"] == "</script><script>alert(1)</script>"


def test_write_trend_chart_overwrites_one_undated_file(tmp_path):
    klines = _klines()
    out = etf_rs_chart.write_trend_chart(
        klines, ["AAA", "BBB"], "SPY", {"AAA": "甲"}, {}, tmp_path, date(2026, 9, 12),
    )
    assert out == tmp_path / "Reports" / "ETF" / "etf_rs_trend.html"
    again = etf_rs_chart.write_trend_chart(
        klines, ["AAA"], "SPY", {}, {}, tmp_path, date(2026, 9, 13),
    )
    assert again == out and [p.name for p in out.parent.iterdir()] == ["etf_rs_trend.html"]
    assert [s["t"] for s in _payload(out.read_text())["series"]] == ["AAA"]


def test_write_trend_chart_without_history_writes_nothing(tmp_path):
    out = etf_rs_chart.write_trend_chart({}, ["AAA"], "SPY", {}, {}, tmp_path, date(2026, 9, 12))
    assert out is None and not (tmp_path / "Reports").exists()


# --- run_etf_rs wiring (soft step) ---


def _cfg():
    return {
        "enabled": True,
        "tickers": {"AAA": "甲", "BBB": "乙", "CCC": "丙"},
        "holdings": {"AAA": "X、Y"},
        "benchmark": "SPY",
    }


def test_run_writes_chart_in_ranking_order(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: _klines())
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 12))
    ranked = [line.split()[1] for line in out.read_text().splitlines()]
    chart = tmp_path / "Reports" / "ETF" / "etf_rs_trend.html"
    assert [s["t"] for s in _payload(chart.read_text())["series"]] == ranked


def test_run_chart_failure_does_not_lose_the_ranking(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: _klines())

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(etf_rs_chart, "write_trend_chart", boom)
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 12))
    assert out is not None and out.exists()
