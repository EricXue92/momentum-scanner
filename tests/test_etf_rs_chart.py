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
import etf_holdings_rs
import pytest


@pytest.fixture(autouse=True)
def _no_holdings_rs_network(monkeypatch):
    """run_etf_rs now runs the holdings 3M step, whose default path goes
    online; stub it to "nothing" unless a test installs its own fake."""
    monkeypatch.setattr(etf_holdings_rs, "compute_extras", lambda *a, **k: {})
    import etf_rs_leaders
    monkeypatch.setattr(etf_rs_leaders, "_fetch_daily_bars", lambda tickers: None)
    monkeypatch.setattr(etf_rs_leaders, "_fetch_rs_3m_table", lambda output_dir, today: None)
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
        "s12": [None] * len(hist), "p12": [None] * len(hist),  # no 12M history given
        "strong": "",  # no extras given
    }
    assert data["series"][0]["holdings"] == ""


def test_render_embeds_the_strongest_holdings_line_per_series():
    hist = etf_rs_chart.score_history(_klines(), ["BBB", "AAA"], "SPY")
    html = etf_rs_chart.render_html(
        hist, {}, {}, "SPY", date(2026, 9, 12),
        extras={"AAA": "3M强: X > Y > Z", "ZZZ": "3M强: ignored"},
    )
    data = _payload(html)
    assert [s["strong"] for s in data["series"]] == ["", "3M强: X > Y > Z"]
    # the ranking list renders it under the holdings of each row, set off
    # from the holdings by a rule above it
    assert "S[k].strong" in html
    rule = re.search(r"\.row \.st \{([^}]*)\}", html).group(1)
    assert "border-top: 1px solid var(--down)" in rule   # red rule
    assert "font-size: 13.5px" in rule                    # larger than the 12px holdings
    assert "margin-top: 8px" in rule and "padding-top: 6px" in rule


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


def test_write_trend_chart_passes_extras_through(tmp_path):
    out = etf_rs_chart.write_trend_chart(
        _klines(), ["AAA", "BBB"], "SPY", {}, {}, tmp_path, date(2026, 9, 12),
        extras={"BBB": "3M强: Q (前十大)"},
    )
    data = _payload(out.read_text())
    assert {s["t"]: s["strong"] for s in data["series"]} == {"AAA": "", "BBB": "3M强: Q (前十大)"}


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


def test_run_passes_the_holdings_line_to_the_chart(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: _klines())
    monkeypatch.setattr(etf_holdings_rs, "compute_extras",
                        lambda *a, **k: {"AAA": "3M强: X > Y"})
    etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 12))
    chart = tmp_path / "Reports" / "ETF" / "etf_rs_trend.html"
    strong = {s["t"]: s["strong"] for s in _payload(chart.read_text())["series"]}
    assert strong["AAA"] == "3M强: X > Y" and strong["BBB"] == ""


def test_run_chart_failure_does_not_lose_the_ranking(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: _klines())

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(etf_rs_chart, "write_trend_chart", boom)
    out = etf_rs.run_etf_rs(_cfg(), tmp_path, date(2026, 9, 12))
    assert out is not None and out.exists()


# --- 12M window alongside 3M (replaced the 1M weekly ladder 2026-11-24) ---


def test_12m_ladder_is_the_ibd_quarter_ladder_in_bars():
    assert etf_rs_chart.WEIGHTS_12M_BARS == [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]


def test_history_12m_is_scorable_from_bar_253_and_matches_the_ranking_arithmetic():
    klines = {"AAA": _walk(1, n=300), "SPY": _walk(9, n=300)}
    h12 = etf_rs_chart.score_history(klines, ["AAA"], "SPY", weights_bars=etf_rs_chart.WEIGHTS_12M_BARS)
    h3 = etf_rs_chart.score_history(klines, ["AAA"], "SPY")
    assert len(h12) == 300 - 252 and len(h3) == 300 - 63
    day = h12.index[-1]
    expected = etf_rs.rank_etfs(_truncate(klines, day), ["AAA"], "SPY",
                                weights_bars=etf_rs_chart.WEIGHTS_12M_BARS).loc["AAA", "raw_score"]
    assert math.isclose(h12.loc[day, "AAA"], expected)


def test_render_embeds_12m_series_aligned_to_the_3m_calendar():
    klines = {"AAA": _walk(1, n=300), "BBB": _walk(2, n=300), "SPY": _walk(9, n=300)}
    h3 = etf_rs_chart.score_history(klines, ["AAA", "BBB"], "SPY")
    h12 = etf_rs_chart.score_history(klines, ["AAA", "BBB"], "SPY",
                                     weights_bars=etf_rs_chart.WEIGHTS_12M_BARS)
    data = _payload(etf_rs_chart.render_html(h3, {}, {}, "SPY", date(2026, 9, 12), history_12m=h12))
    aaa = data["series"][0]
    assert len(aaa["s12"]) == len(data["dates"]) == len(h3)
    assert aaa["s12"][0] is None                      # 3M calendar starts 189 days before 12M does
    assert aaa["s12"][-1] == round(float(h12["AAA"].iloc[-1]) * 100, 2)
    assert aaa["p12"][-1] == int(etf_rs_chart.percentile_history(h12)["AAA"].iloc[-1])


def test_write_trend_chart_starts_where_12m_does_and_keeps_3m_only_tickers(tmp_path):
    klines = {"AAA": _walk(1, n=300), "BBB": _walk(2, n=300), "SPY": _walk(9, n=300)}
    klines["NEW"] = _walk(4, n=150)   # 150 bars: 3M scorable, 12M not
    out = etf_rs_chart.write_trend_chart(
        klines, ["AAA", "BBB", "NEW"], "SPY", {}, {}, tmp_path, date(2026, 9, 12))
    data = _payload(out.read_text())
    assert [s["t"] for s in data["series"]] == ["AAA", "BBB", "NEW"]
    assert len(data["dates"]) == 300 - 252            # both windows cover the same span
    aaa, new = data["series"][0], data["series"][2]
    assert None not in aaa["s"] and None not in aaa["s12"]
    assert new["s"][-1] is not None and all(v is None for v in new["s12"])


def test_write_trend_chart_without_a_year_of_history_falls_back_to_the_3m_calendar(tmp_path):
    out = etf_rs_chart.write_trend_chart(
        _klines(), ["AAA", "BBB"], "SPY", {}, {}, tmp_path, date(2026, 9, 12))
    data = _payload(out.read_text())
    assert len(data["dates"]) == 120 - 63
    assert all(v is None for s in data["series"] for v in s["s12"])


def test_page_has_a_12m_window_and_nothing_left_of_the_1m_one():
    html = etf_rs_chart.render_html(
        etf_rs_chart.score_history(_klines(), ["AAA", "BBB"], "SPY"), {}, {}, "SPY", date(2026, 9, 12))
    for series in _payload(html)["series"]:
        assert set(series) == {"t", "name", "holdings", "strong", "s", "p", "s12", "p12"}
    for there in ("['12 个月 RS', '12m']", "3 个月 vs 12 个月 名次差", "0.4·R63 + 0.2·R126 + 0.2·R189 + 0.2·R252"):
        assert there in html, there
    for gone in ("1 个月", "1M", "'1m'", "ranks1[", ".s1[", ".p1[", "R5 "):
        assert gone not in html.replace("12 个月", "").replace("12M", ""), gone


def test_diff_view_reads_3m_ahead_of_12m_as_gaining():
    # rank difference = 12M rank − 3M rank (positive = the 3M ladder ranks it
    # higher); the 走强 group additionally needs a positive 3M excess.
    html = etf_rs_chart._TEMPLATE
    assert "ranks12[k][i] - ranks3[k][i]" in html
    assert "return S[k].s[i] > 0 ? 0 : 1;" in html


def test_run_charts_exactly_the_ranked_tickers(tmp_path, monkeypatch):
    klines = _klines()
    klines["NEW"] = _walk(4, n=40)   # below the 3M ladder's 64 bars: no line on either window
    monkeypatch.setattr(etf_rs, "_fetch_klines", lambda *a, **k: klines)
    cfg = _cfg(); cfg["tickers"]["NEW"] = "新"
    out = etf_rs.run_etf_rs(cfg, tmp_path, date(2026, 9, 12))
    ranked = [line.split()[1] for line in out.read_text().splitlines()]
    assert "NEW" not in ranked
    chart = tmp_path / "Reports" / "ETF" / "etf_rs_trend.html"
    assert [s["t"] for s in _payload(chart.read_text())["series"]] == ranked
