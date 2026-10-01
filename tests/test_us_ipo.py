import math

import numpy as np
import pandas as pd
import pytest


def _make_kline(closes: list[float], highs=None, lows=None, volumes=None) -> pd.DataFrame:
    n = len(closes)
    highs = highs if highs is not None else [c * 1.02 for c in closes]
    lows = lows if lows is not None else [c * 0.98 for c in closes]
    volumes = volumes if volumes is not None else [1_000_000.0] * n
    return pd.DataFrame({
        "time_key": pd.date_range(end="2026-05-21", periods=n, freq="B"),
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_build_ipo_metrics_short_history_returns_nans():
    from us_ipo import _build_ipo_metrics
    klines = {"NEW": _make_kline([25.0] * 15)}
    caps = {"NEW": 1e9}
    metrics = _build_ipo_metrics(klines, caps)
    row = metrics.loc["NEW"]
    assert row["market_cap"] == 1e9
    assert row["last_price"] == 25.0
    assert math.isnan(row["avg_vol_20d"])
    assert math.isnan(row["sma50"])
    assert row["above_sma50"] is False
    assert row["above_sma200"] is False


def test_build_ipo_metrics_20day_metrics_populated():
    from us_ipo import _build_ipo_metrics
    klines = {"OK": _make_kline([20.0] * 25)}
    caps = {"OK": 5e9}
    metrics = _build_ipo_metrics(klines, caps)
    row = metrics.loc["OK"]
    assert row["avg_vol_20d"] == 1_000_000.0
    assert abs(row["avg_dollar_vol_20d"] - 20.0 * 1_000_000.0) < 1e-6
    assert row["adr_pct"] > 0  # (high - low) / close ≠ 0


def test_build_ipo_metrics_sma50_populated_at_50_days():
    from us_ipo import _build_ipo_metrics
    klines = {"OK": _make_kline([20.0] * 55)}
    caps = {"OK": 5e9}
    metrics = _build_ipo_metrics(klines, caps)
    row = metrics.loc["OK"]
    assert not math.isnan(row["sma50"])
    assert math.isnan(row["sma200"])  # < 200 days


def test_build_ipo_metrics_missing_cap_is_nan():
    from us_ipo import _build_ipo_metrics
    klines = {"NOCAP": _make_kline([25.0] * 30)}
    caps = {}  # no cap for NOCAP
    metrics = _build_ipo_metrics(klines, caps)
    row = metrics.loc["NOCAP"]
    assert math.isnan(row["market_cap"])


def test_build_ipo_metrics_skips_single_row():
    """1-row klines must be skipped (len(df) < 2 guard, parity with hk_eod)."""
    from us_ipo import _build_ipo_metrics
    klines = {"ONEROW": _make_kline([25.0] * 1)}
    caps = {"ONEROW": 1e9}
    metrics = _build_ipo_metrics(klines, caps)
    assert metrics.empty


def _us_settings_default():
    return {
        "min_market_cap": 300_000_000,
        "min_price": 10.0,
        "min_avg_volume": 500_000,
        "min_dollar_volume": 100_000_000,
        "min_adr_percent": 4.0,
        "min_rs_percentile_3m": 90,
    }


def test_filter_drops_when_history_below_20_days():
    from us_ipo import filter_us_ipo_candidates
    klines = {"NEW1": _make_kline([25.0] * 15)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines,
        finviz_caps={"NEW1": 5e9},
        rs_table_3m_full=None,
        spy_kline=None,
        settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["min_history"] == 1


def test_filter_drops_when_cap_below_threshold():
    from us_ipo import filter_us_ipo_candidates
    klines = {"SMALL": _make_kline([25.0] * 25)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines,
        finviz_caps={"SMALL": 100_000_000},
        rs_table_3m_full=None,
        spy_kline=None,
        settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["cap"] == 1


def test_filter_drops_when_cap_missing():
    from us_ipo import filter_us_ipo_candidates
    klines = {"NOCAP": _make_kline([25.0] * 25)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines,
        finviz_caps={},
        rs_table_3m_full=None,
        spy_kline=None,
        settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["cap"] == 1


def test_filter_drops_when_price_below_threshold():
    from us_ipo import filter_us_ipo_candidates
    klines = {"PENNY": _make_kline([5.0] * 25)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines,
        finviz_caps={"PENNY": 5e9},
        rs_table_3m_full=None,
        spy_kline=None,
        settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["price"] == 1


def test_filter_drops_when_avg_vol_below_threshold():
    from us_ipo import filter_us_ipo_candidates
    klines = {"LOWVOL": _make_kline([25.0] * 25, volumes=[100_000.0] * 25)}  # 100K < 500K
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"LOWVOL": 5e9},
        rs_table_3m_full=None, spy_kline=None, settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["avg_vol"] == 1


def test_filter_drops_when_dvol_below_threshold():
    from us_ipo import filter_us_ipo_candidates
    # price=$15 × avg_vol=600K = $9M dollar vol < $100M
    klines = {"LOWDV": _make_kline([15.0] * 25, volumes=[600_000.0] * 25)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"LOWDV": 5e9},
        rs_table_3m_full=None, spy_kline=None, settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["dvol"] == 1


def test_filter_drops_when_adr_below_threshold():
    from us_ipo import filter_us_ipo_candidates
    # Flat closes with tiny H/L spread → ADR < 4%
    closes = [25.0] * 25
    highs = [25.05] * 25
    lows = [24.95] * 25
    klines = {"FLAT": _make_kline(closes, highs=highs, lows=lows, volumes=[5_000_000.0] * 25)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"FLAT": 5e9},
        rs_table_3m_full=None, spy_kline=None, settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["adr"] == 1


def test_filter_drops_when_below_sma50():
    from us_ipo import filter_us_ipo_candidates
    # First 49 closes = 30 (avg pulls sma50 high), last close = 25 below sma50
    closes = [30.0] * 49 + [25.0]
    klines = {"DEC": _make_kline(closes, highs=[c * 1.05 for c in closes],
                                  lows=[c * 0.95 for c in closes],
                                  volumes=[5_000_000.0] * 50)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"DEC": 5e9},
        rs_table_3m_full=None, spy_kline=None, settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["sma50"] == 1


def test_filter_keeps_clean_20_to_49_day_ticker():
    from us_ipo import filter_us_ipo_candidates
    # 30 行,price=25,vol=1M,wide H/L → 全部 ≥20-day gate pass; < 50 → SMA 跳过
    closes = [25.0] * 30
    highs = [27.0] * 30
    lows = [23.0] * 30
    klines = {"FRESH": _make_kline(closes, highs=highs, lows=lows,
                                    volumes=[5_000_000.0] * 30)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"FRESH": 5e9},
        rs_table_3m_full=None, spy_kline=None, settings=_us_settings_default(),
    )
    assert kept == ["FRESH"]
    assert sum(drops.values()) == 0


def test_filter_skips_rs_gate_when_below_64_days():
    """Tickers with 30 days history don't trigger the RS gate even if
    the table would have rejected them."""
    from us_ipo import filter_us_ipo_candidates
    closes = [25.0] * 30
    klines = {"YOUNG": _make_kline(closes, highs=[27.0] * 30, lows=[23.0] * 30,
                                    volumes=[5_000_000.0] * 30)}
    # Table has YOUNG with a very low raw_score — but len < 64 so the gate
    # shouldn't even fire.
    rs_table = pd.DataFrame({
        "raw_score": [0.5, 0.4, 0.3],
        "rs_percentile": [99, 95, 90],
    }, index=["A", "B", "C"])
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"YOUNG": 5e9},
        rs_table_3m_full=rs_table,
        spy_kline=_make_kline([400.0] * 80),
        settings=_us_settings_default(),
    )
    assert kept == ["YOUNG"]
    assert drops["rs_3m"] == 0


def test_filter_passes_rs_gate_when_score_at_high_percentile():
    """A 70-day IPO with +20% jump beats most Fred6725 scores → ≥90 percentile → kept."""
    from us_ipo import filter_us_ipo_candidates
    # 70 行 with strong jump at the very end relative to 21d ago
    closes = [100.0] * 50 + [120.0] * 20
    klines = {"WINNER": _make_kline(closes, highs=[c * 1.05 for c in closes],
                                     lows=[c * 0.95 for c in closes],
                                     volumes=[10_000_000.0] * 70)}
    # Fred6725-like distribution: scores -0.1 to 0.05 → WINNER's much higher
    # score lands in the top
    rs_table = pd.DataFrame({
        "raw_score": np.linspace(-0.1, 0.05, 100),
        "rs_percentile": np.linspace(0, 99, 100).astype(int),
    }, index=[f"F{i:03d}" for i in range(100)])
    spy = _make_kline([400.0] * 80)
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"WINNER": 5e9},
        rs_table_3m_full=rs_table, spy_kline=spy,
        settings=_us_settings_default(),
    )
    assert kept == ["WINNER"]
    assert drops["rs_3m"] == 0


def test_filter_drops_when_rs_score_below_threshold():
    """A 70-day IPO scoring lower than the 90th-percentile cutoff → drop."""
    from us_ipo import filter_us_ipo_candidates
    # Use rising closes so last > sma50 (passes all other gates, reaches RS gate)
    closes = [20.0] * 50 + [25.0] * 20
    klines = {"LOSER": _make_kline(closes, highs=[c * 1.08 for c in closes],
                                    lows=[c * 0.92 for c in closes],
                                    volumes=[10_000_000.0] * 70)}
    # Fred6725 distribution all well above 0 → LOSER's small positive score
    # (last/past21 - 1 ≈ 0.25 raw) - spy_score(0) ≈ 0.25, but distribution
    # is [0.5, 1.0] → lands at near 0 percentile → drops
    rs_table = pd.DataFrame({
        "raw_score": np.linspace(0.5, 1.0, 100),
        "rs_percentile": np.linspace(0, 99, 100).astype(int),
    }, index=[f"F{i:03d}" for i in range(100)])
    spy = _make_kline([400.0] * 80)
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"LOSER": 5e9},
        rs_table_3m_full=rs_table, spy_kline=spy,
        settings=_us_settings_default(),
    )
    assert kept == []
    assert drops["rs_3m"] == 1


def test_filter_passes_when_rs_table_is_none():
    """rs_table_3m_full=None → 3M gate skipped (passthrough); other gates still apply."""
    from us_ipo import filter_us_ipo_candidates
    # Use rising closes so last > sma50
    closes = [20.0] * 50 + [25.0] * 20
    klines = {"PASS": _make_kline(closes, highs=[c * 1.08 for c in closes],
                                   lows=[c * 0.92 for c in closes],
                                   volumes=[10_000_000.0] * 70)}
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"PASS": 5e9},
        rs_table_3m_full=None, spy_kline=None,
        settings=_us_settings_default(),
    )
    assert kept == ["PASS"]
    assert drops["rs_3m"] == 0


def test_filter_passes_when_rs_threshold_is_zero():
    """min_rs_percentile_3m=0 → gate fully disabled."""
    from us_ipo import filter_us_ipo_candidates
    # Use rising closes so last > sma50
    closes = [20.0] * 50 + [25.0] * 20
    klines = {"X": _make_kline(closes, highs=[c * 1.08 for c in closes],
                                lows=[c * 0.92 for c in closes],
                                volumes=[10_000_000.0] * 70)}
    rs_table = pd.DataFrame({
        "raw_score": [0.5],
        "rs_percentile": [99],
    }, index=["X"])
    settings = _us_settings_default()
    settings["min_rs_percentile_3m"] = 0
    kept, drops = filter_us_ipo_candidates(
        klines=klines, finviz_caps={"X": 5e9},
        rs_table_3m_full=rs_table, spy_kline=_make_kline([400.0] * 80),
        settings=settings,
    )
    assert kept == ["X"]
    assert drops["rs_3m"] == 0


def test_build_ipo_metrics_perf_4w_is_20_sessions_back():
    from us_ipo import _build_ipo_metrics
    # 21 行: 20 个交易日前收 20, 现价 25 → +25%
    klines = {"UP": _make_kline([20.0] + [22.0] * 19 + [25.0])}
    row = _build_ipo_metrics(klines, {"UP": 5e9}).loc["UP"]
    assert abs(row["perf_4w"] - 25.0) < 1e-9


def test_build_ipo_metrics_perf_4w_nan_at_exactly_20_rows():
    from us_ipo import _build_ipo_metrics
    klines = {"D20": _make_kline([20.0] * 19 + [25.0])}
    row = _build_ipo_metrics(klines, {"D20": 5e9}).loc["D20"]
    assert math.isnan(row["perf_4w"])


def _ipo_kline(closes):
    # 宽 H/L + 大成交量: 其余闸门全过, 只留方向闸可拦
    return _make_kline(closes, highs=[c * 1.08 for c in closes],
                       lows=[c * 0.92 for c in closes],
                       volumes=[10_000_000.0] * len(closes))


@pytest.mark.parametrize(
    "closes, want_kept",
    [
        ([30.0] * 10 + [25.0] * 20, []),           # 30 行, 4 周 -16.7%
        ([25.0] * 30, []),                         # 持平: 严格大于才放行
        ([25.0] * 10 + [25.5] * 20, ["T"]),        # 4 周 +2%
        # 63 行: 现价 28 在 SMA50 (24.4) 之上, 但 20 日前收 30 → 4 周 -6.7%
        ([20.0] * 38 + [30.0] * 10 + [28.0] * 15, []),
        ([20.0] * 19 + [25.0], []),                # 20 行算不出 perf_4w → 等 21 行
    ],
)
def test_filter_direction_gate_below_64_days(closes, want_kept):
    from us_ipo import filter_us_ipo_candidates
    kept, drops = filter_us_ipo_candidates(
        klines={"T": _ipo_kline(closes)}, finviz_caps={"T": 5e9},
        rs_table_3m_full=None, spy_kline=None,
        settings={**_us_settings_default(), "ipo_min_perf_4w": 0.0},
    )
    assert kept == want_kept
    assert sum(drops.values()) == (0 if want_kept else 1)


def test_filter_direction_gate_drop_lands_in_perf_4w_bucket():
    from us_ipo import filter_us_ipo_candidates
    _, drops = filter_us_ipo_candidates(
        klines={"T": _ipo_kline([30.0] * 10 + [25.0] * 20)}, finviz_caps={"T": 5e9},
        rs_table_3m_full=None, spy_kline=None,
        settings={**_us_settings_default(), "ipo_min_perf_4w": 0.0},
    )
    assert drops["perf_4w"] == 1


def test_filter_direction_gate_not_applied_from_64_days():
    from us_ipo import filter_us_ipo_candidates
    # 70 行: 现价 28 在 SMA50 (24.4) 之上, 但 20 日前收 30 → 4 周 -6.7%。
    # ≥64 行由 3M RS 接管 (此处表为 None → 放行)。
    closes = [20.0] * 45 + [30.0] * 10 + [28.0] * 15
    kept, drops = filter_us_ipo_candidates(
        klines={"OLD": _ipo_kline(closes)}, finviz_caps={"OLD": 5e9},
        rs_table_3m_full=None, spy_kline=None,
        settings={**_us_settings_default(), "ipo_min_perf_4w": 0.0},
    )
    assert kept == ["OLD"]
    assert drops["perf_4w"] == 0


def test_filter_direction_gate_off_when_key_unset():
    from us_ipo import filter_us_ipo_candidates
    kept, drops = filter_us_ipo_candidates(
        klines={"T": _ipo_kline([30.0] * 10 + [25.0] * 20)}, finviz_caps={"T": 5e9},
        rs_table_3m_full=None, spy_kline=None,
        settings=_us_settings_default(),
    )
    assert kept == ["T"]
    assert drops["perf_4w"] == 0
