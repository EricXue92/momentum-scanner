import pandas as pd
import pytest

from hk_eod import build_metrics_frame, apply_strategy_filters, dedup_by_priority


def _make_kline(closes, volumes=None, highs=None, lows=None):
    n = len(closes)
    if volumes is None:
        volumes = [1_000_000] * n
    if highs is None:
        highs = [c * 1.02 for c in closes]
    if lows is None:
        lows = [c * 0.98 for c in closes]
    dates = pd.date_range(end="2026-05-05", periods=n, freq="B")
    return pd.DataFrame({
        "time_key": dates,
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_build_metrics_frame_basic():
    # 252 days of flat $50 close, then today's close at $52.5 (gap +5%, RVol 2x)
    closes = [50.0] * 251 + [52.5]
    volumes = [1_000_000] * 251 + [2_000_000]
    klines = {"HK.00001": _make_kline(closes, volumes=volumes)}
    caps = {"HK.00001": 5_000_000_000.0}
    df = build_metrics_frame(klines, caps)

    row = df.loc["HK.00001"]
    assert int(row["n_rows"]) == len(closes)  # 252
    assert row["market_cap"] == 5_000_000_000.0
    assert row["last_price"] == pytest.approx(52.5)
    assert row["prev_close"] == pytest.approx(50.0)
    assert row["gap_pct"] == pytest.approx(5.0)
    assert row["rvol"] == pytest.approx(2.0)
    assert row["avg_vol_20d"] == pytest.approx(1_050_000)  # 19×1M + 1×2M / 20
    assert row["avg_dollar_vol_20d"] == pytest.approx(52.5 * 1_050_000)
    assert row["sma50"] == pytest.approx((49 * 50.0 + 52.5) / 50)
    assert row["sma200"] == pytest.approx((199 * 50.0 + 52.5) / 200)
    assert row["above_sma50"] is True or row["above_sma50"] == True
    assert row["above_sma200"] is True or row["above_sma200"] == True
    assert row["perf_4w"] == pytest.approx(5.0)  # 20 trading days back was 50.0


def test_build_metrics_frame_skips_short_history():
    # Only 30 days of data — not enough for SMA50 or SMA200, but should still
    # return a row with NaN for those columns.
    closes = [50.0] * 29 + [52.0]
    klines = {"HK.00002": _make_kline(closes)}
    caps = {"HK.00002": 1_000_000_000.0}
    df = build_metrics_frame(klines, caps)
    row = df.loc["HK.00002"]
    assert pd.isna(row["sma200"])
    assert row["above_sma200"] is False
    # 30 < 50 so SMA50 should also be NaN
    assert pd.isna(row["sma50"])
    assert row["above_sma50"] is False


def test_apply_strategy_filters_baseline_and_priority_inputs():
    df = pd.DataFrame([
        # Passes everything + gap 5% + rvol 3 → EarningsGap, HighVolume, GapUp, RS
        dict(market_cap=1e9, last_price=25.0, prev_close=23.81, gap_pct=5.0,
             rvol=3.0, avg_vol_20d=1e6, avg_dollar_vol_20d=2e8, adr_pct=5.0,
             sma50=22.0, sma200=20.0, above_sma50=True, above_sma200=True,
             perf_4w=10.0, perf_13w=20.0, perf_26w=30.0, perf_ytd=40.0,
             perf_52w=50.0, consecutive_up_days=2),
        # Cap too small → drops out everywhere
        dict(market_cap=1e8, last_price=25.0, prev_close=23.81, gap_pct=5.0,
             rvol=3.0, avg_vol_20d=1e6, avg_dollar_vol_20d=2e8, adr_pct=5.0,
             sma50=22.0, sma200=20.0, above_sma50=True, above_sma200=True,
             perf_4w=10.0, perf_13w=20.0, perf_26w=30.0, perf_ytd=40.0,
             perf_52w=50.0, consecutive_up_days=2),
    ], index=["HK.00001", "HK.00002"])

    settings = dict(min_market_cap=3e8, min_dollar_volume=1e8,
                    min_avg_volume=5e5, min_adr_percent=4.0, min_price=20.0)
    longs = [
        {"key": "earnings_gap", "min_relative_volume": 3, "min_gap_percent": 3.0},
        {"key": "high_volume", "min_relative_volume": 3},
        {"key": "gap_up", "min_gap_percent": 5.0},
    ]
    leaders = [
        {"min_perf_4w": 30}, {"min_perf_13w": 50}, {"min_perf_26w": 100},
        {"min_perf_ytd": 100}, {"min_perf_52w": 150},
    ]
    out = apply_strategy_filters(df, settings, longs, leaders, rs_enabled=True)

    assert out["EarningsGap"] == ["HK.00001"]
    assert out["HighVolume"] == ["HK.00001"]
    assert out["GapUp"] == ["HK.00001"]
    assert out["Leaders"] == []  # perf_4w 10 < 30, etc.
    assert out["RS"] == ["HK.00001"]


def test_apply_strategy_filters_rs_requires_day_up():
    """RS 组要求当日收红 (last_price > prev_close), 对齐美股 [rs] 的
    ta_perf_dup; 其他组不受当日涨跌影响。"""
    df = pd.DataFrame([
        # Full baseline + strong perf, but closed DOWN today → Leaders yes, RS no
        dict(market_cap=1e9, last_price=25.0, prev_close=26.0, gap_pct=0.0,
             rvol=1.0, avg_vol_20d=1e6, avg_dollar_vol_20d=2e8, adr_pct=5.0,
             sma50=22.0, sma200=20.0, above_sma50=True, above_sma200=True,
             perf_4w=40.0, perf_13w=60.0, perf_26w=120.0, perf_ytd=120.0,
             perf_52w=160.0, consecutive_up_days=0),
        # Same but closed UP today → Leaders and RS
        dict(market_cap=1e9, last_price=25.0, prev_close=24.0, gap_pct=0.0,
             rvol=1.0, avg_vol_20d=1e6, avg_dollar_vol_20d=2e8, adr_pct=5.0,
             sma50=22.0, sma200=20.0, above_sma50=True, above_sma200=True,
             perf_4w=40.0, perf_13w=60.0, perf_26w=120.0, perf_ytd=120.0,
             perf_52w=160.0, consecutive_up_days=1),
    ], index=["HK.00003", "HK.00004"])

    settings = dict(min_market_cap=3e8, min_dollar_volume=1e8,
                    min_avg_volume=5e5, min_adr_percent=4.0, min_price=20.0)
    longs = [
        {"key": "earnings_gap", "min_relative_volume": 3, "min_gap_percent": 3.0},
        {"key": "high_volume", "min_relative_volume": 3},
        {"key": "gap_up", "min_gap_percent": 5.0},
    ]
    leaders = [
        {"min_perf_4w": 30}, {"min_perf_13w": 50}, {"min_perf_26w": 100},
        {"min_perf_ytd": 100}, {"min_perf_52w": 150},
    ]
    out = apply_strategy_filters(df, settings, longs, leaders, rs_enabled=True)

    assert out["Leaders"] == ["HK.00003", "HK.00004"]
    assert out["RS"] == ["HK.00004"]


def test_dedup_by_priority_strips_lower_priority_duplicates():
    raw = {
        "EarningsGap": ["A", "B"],
        "HighVolume":  ["B", "C"],     # B already in EG → dropped
        "GapUp":       ["C", "D"],     # C already in HV → dropped
        "Leaders":     ["D", "E"],     # D already in GU → dropped
        "RS":          ["A", "F"],     # A already in EG → dropped
    }
    out = dedup_by_priority(raw)
    assert out == {
        "EarningsGap": ["A", "B"],
        "HighVolume":  ["C"],
        "GapUp":       ["D"],
        "Leaders":     ["E"],
        "RS":          ["F"],
    }


def test_dedup_by_priority_excludes_rs_when_priority_is_subset():
    """When the caller passes a 4-strategy priority subset (RS omitted),
    RS must be absent from the output dict — the caller is responsible
    for splicing it back in untouched. This pins the contract used by
    run_hk_eod to carve RS out of within-day priority dedup."""
    raw = {
        "EarningsGap": ["A"],
        "HighVolume":  ["B"],
        "GapUp":       ["C"],
        "Leaders":     ["D", "E"],
        "RS":          ["A", "D", "F"],  # would normally be dropped to ["F"]
    }
    out = dedup_by_priority(
        raw,
        priority=["EarningsGap", "HighVolume", "GapUp", "Leaders"],
    )
    assert "RS" not in out
    assert out == {
        "EarningsGap": ["A"],
        "HighVolume":  ["B"],
        "GapUp":       ["C"],
        "Leaders":     ["D", "E"],
    }


from hk_eod import filter_hk_ipo_candidates


def _ipo_metrics_row(
    n_rows,
    market_cap=5e9,
    last_price=25.0,
    avg_vol_20d=1_000_000.0,
    avg_dollar_vol_20d=2.5e8,
    adr_pct=5.0,
    sma50=22.0,
    sma200=20.0,
    above_sma50=True,
    above_sma200=True,
):
    """Returns a metrics-frame row dict that PASSES all baseline gates.

    ``n_rows`` is the k-line history depth — the IPO filter buckets on this
    column (formerly derived from the raw klines dict's ``len(df)``)."""
    return dict(
        n_rows=n_rows,
        market_cap=market_cap,
        last_price=last_price,
        avg_vol_20d=avg_vol_20d,
        avg_dollar_vol_20d=avg_dollar_vol_20d,
        adr_pct=adr_pct,
        sma50=sma50,
        sma200=sma200,
        above_sma50=above_sma50,
        above_sma200=above_sma200,
    )


def _hk_settings_default():
    return {
        "min_market_cap": 300_000_000,
        "min_price": 20.0,
        "min_avg_volume": 500_000,
        "min_dollar_volume": 100_000_000,
        "min_adr_percent": 3.5,
        "min_rs_percentile_longs_3m": 90,
    }


def test_filter_hk_ipo_drops_when_history_below_20_days():
    # n_rows=15 < 20 → 立刻砍掉，不进入任何 metric 闸门
    metrics = pd.DataFrame.from_dict(
        {"HK.NEW1": _ipo_metrics_row(n_rows=15)}, orient="index"
    )
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=None, hk_settings=_hk_settings_default()
    )
    assert kept == []
    assert drops["min_history"] == 1


def test_filter_hk_ipo_keeps_20_to_49_days_without_rs_check():
    # n_rows=30 — 过 20-day floor，未达 64 天 RS 触发线 → 走现有阶梯放行
    # 短历史：SMA50/SMA200 都是 NaN，above_sma 字段为 False（与 build_metrics_frame 一致）
    metrics = pd.DataFrame.from_dict(
        {"HK.MID": _ipo_metrics_row(
            n_rows=30,
            sma50=float("nan"), sma200=float("nan"),
            above_sma50=False, above_sma200=False,
        )},
        orient="index",
    )
    # 即使 rs_table_3m 里把 HK.MID 设成 RS=10，<64 天的 ticker 也不走 RS 检查
    rs_table = pd.DataFrame({"rs_percentile": [10]}, index=["HK.MID"])
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=rs_table, hk_settings=_hk_settings_default()
    )
    assert kept == ["HK.MID"]
    assert drops["rs_3m"] == 0
    assert drops["rs_3m_missing"] == 0


def test_filter_hk_ipo_keeps_when_rs_3m_at_or_above_threshold():
    # n_rows=70 ≥ 64 → 触发 3M RS 闸门；表内 percentile = 95 ≥ 90 → 保留
    metrics = pd.DataFrame.from_dict(
        {"HK.A": _ipo_metrics_row(n_rows=70, sma200=float("nan"), above_sma200=False)},
        orient="index",
    )
    rs_table = pd.DataFrame({"rs_percentile": [95]}, index=["HK.A"])
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=rs_table, hk_settings=_hk_settings_default()
    )
    assert kept == ["HK.A"]
    assert drops["rs_3m"] == 0


def test_filter_hk_ipo_drops_when_rs_3m_below_threshold():
    # n_rows=70 ≥ 64 → 触发 3M RS 闸门；表内 percentile = 80 < 90 → drop
    metrics = pd.DataFrame.from_dict(
        {"HK.B": _ipo_metrics_row(n_rows=70, sma200=float("nan"), above_sma200=False)},
        orient="index",
    )
    rs_table = pd.DataFrame({"rs_percentile": [80]}, index=["HK.B"])
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=rs_table, hk_settings=_hk_settings_default()
    )
    assert kept == []
    assert drops["rs_3m"] == 1


def test_filter_hk_ipo_drops_when_64days_but_missing_from_rs_table():
    # n_rows=70 ≥ 64 → 触发 3M RS 闸门；但 ticker 不在表里 → drop（rs_3m_missing）
    # 这种情况一般是 _score_from_kline 因 zero_last/zero_past 排掉了
    metrics = pd.DataFrame.from_dict(
        {"HK.C": _ipo_metrics_row(n_rows=70, sma200=float("nan"), above_sma200=False)},
        orient="index",
    )
    rs_table = pd.DataFrame({"rs_percentile": [95]}, index=["HK.OTHER"])  # 不含 HK.C
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=rs_table, hk_settings=_hk_settings_default()
    )
    assert kept == []
    assert drops["rs_3m_missing"] == 1


def test_filter_hk_ipo_skips_rs_gate_when_table_is_none():
    # n_rows=70 ≥ 64 但 rs_table_3m=None (HSI fetch 失败) → 回退到只有 metric 闸门
    # day-20 floor 仍生效，但 RS 闸门跳过
    metrics = pd.DataFrame.from_dict(
        {"HK.D": _ipo_metrics_row(n_rows=70, sma200=float("nan"), above_sma200=False)},
        orient="index",
    )
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=None, hk_settings=_hk_settings_default()
    )
    assert kept == ["HK.D"]
    assert drops["rs_3m"] == 0
    assert drops["rs_3m_missing"] == 0


def test_filter_hk_ipo_threshold_zero_disables_entire_3m_gate():
    # min_rs_percentile_longs_3m = 0 关闭整个 3M 闸门:
    # - percentile 任意低都不算 drop
    # - 不在表里也不算 drop
    # 这与 filter_by_rs 的 threshold<=0 短路一致.
    metrics = pd.DataFrame.from_dict(
        {
            "HK.LOW":  _ipo_metrics_row(n_rows=70, sma200=float("nan"), above_sma200=False),
            "HK.MISS": _ipo_metrics_row(n_rows=70, sma200=float("nan"), above_sma200=False),
        },
        orient="index",
    )
    rs_table = pd.DataFrame({"rs_percentile": [10]}, index=["HK.LOW"])
    settings = _hk_settings_default()
    settings["min_rs_percentile_longs_3m"] = 0
    kept, drops = filter_hk_ipo_candidates(
        metrics, rs_table_3m=rs_table, hk_settings=settings
    )
    assert set(kept) == {"HK.LOW", "HK.MISS"}
    assert drops["rs_3m"] == 0
    assert drops["rs_3m_missing"] == 0


def test_hk_rs_line_group_note():
    import pandas as pd
    from hk_eod import _rs_line_group_note
    tline = pd.DataFrame.from_dict(
        {"HK.00001": (0, 0, 0.0), "HK.00002": (1, 12, 0.85)},
        orient="index", columns=["rs_below_ma", "rs_days_below_ma", "rs_frac_below_ma"],
    )
    assert _rs_line_group_note(["HK.00001", "HK.00002"], tline) == " | RS↓1"
    assert _rs_line_group_note(["HK.00001"], tline) == ""   # none below → no note
    assert _rs_line_group_note(["HK.00001"], None) == ""     # no table → no note
    assert _rs_line_group_note([], tline) == ""              # empty group → no note


# ── hk_effective_data_day ────────────────────────────────────────────────

def test_effective_data_day_weekend_maps_to_friday():
    from datetime import datetime, date
    from zoneinfo import ZoneInfo
    from hk_eod import hk_effective_data_day
    hkt = ZoneInfo("Asia/Hong_Kong")
    # 2026-08-01 = Saturday, 2026-08-02 = Sunday → both map to Fri 2026-07-31
    sat = datetime(2026, 8, 1, 12, 33, tzinfo=hkt)
    sun = datetime(2026, 8, 2, 9, 0, tzinfo=hkt)
    assert hk_effective_data_day(sat) == date(2026, 7, 31)
    assert hk_effective_data_day(sun) == date(2026, 7, 31)


def test_effective_data_day_weekday_unchanged():
    from datetime import datetime, date
    from zoneinfo import ZoneInfo
    from hk_eod import hk_effective_data_day
    hkt = ZoneInfo("Asia/Hong_Kong")
    # Weekdays return the calendar date regardless of time-of-day — pre-20:00
    # incompleteness is handled by the bar-trim/use_yesterday logic, not here.
    fri_early = datetime(2026, 7, 31, 8, 0, tzinfo=hkt)
    fri_late = datetime(2026, 7, 31, 20, 5, tzinfo=hkt)
    mon = datetime(2026, 8, 3, 12, 0, tzinfo=hkt)
    assert hk_effective_data_day(fri_early) == date(2026, 7, 31)
    assert hk_effective_data_day(fri_late) == date(2026, 7, 31)
    assert hk_effective_data_day(mon) == date(2026, 8, 3)


# --- HK Shorts 3M RS gate (universe pre-filter, ≙ US Shorts 3M 单闸) ---

def _shorts_rs_table(rows: dict[str, int]) -> pd.DataFrame:
    """rows: Futu code → rs_percentile, e.g. {'HK.00700': 95}."""
    return pd.DataFrame(
        {"rs_percentile": list(rows.values())},
        index=pd.Index(list(rows.keys()), name="code"),
    )


def test_rs_gate_shorts_universe_drops_below_threshold_keeps_missing():
    from hk_eod import _rs_gate_shorts_universe
    table = _shorts_rs_table({"HK.00700": 95, "HK.00148": 40, "HK.00005": 90})
    # '9999' 不在表里 → 保留 (missing passthrough, 与 filter_by_rs 政策一致)
    codes = ["0700", "0148", "0005", "9999"]
    assert _rs_gate_shorts_universe(codes, table, 90) == ["0700", "0005", "9999"]


def test_rs_gate_shorts_universe_passthrough_on_none_or_zero_threshold():
    from hk_eod import _rs_gate_shorts_universe
    codes = ["0700", "0148"]
    assert _rs_gate_shorts_universe(codes, None, 90) == codes
    table = _shorts_rs_table({"HK.00700": 10})
    assert _rs_gate_shorts_universe(codes, table, 0) == codes
    assert _rs_gate_shorts_universe(codes, pd.DataFrame(), 90) == codes


def test_rs_gate_shorts_universe_code_format_mapping():
    from hk_eod import _rs_gate_shorts_universe
    # 4 位 yfinance/TV 码 '0700' 必须映射为 5 位 Futu 码 'HK.00700' 再查表;
    # 若映射错误, 表内低分票会因 "missing" 被误保留。
    table = _shorts_rs_table({"HK.00700": 10})
    assert _rs_gate_shorts_universe(["0700"], table, 90) == []


# --- fetch_hsi_kline_yf ---


def _hsi_grid(n=3):
    idx = pd.date_range(end="2026-09-24", periods=n, freq="B")
    return pd.DataFrame({
        "Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
        "Close": [100.0, 101.0, 102.0][:n], "Volume": [1_000] * n,
    }, index=idx)


def test_fetch_hsi_kline_yf_accepts_flat_single_ticker_frame(monkeypatch):
    # _yf_download_with_retry flattens the (ticker, field) MultiIndex for a
    # single ticker (main._flatten_single_ticker_frame), so the frame arrives
    # with plain field columns — indexing data["^HSI"] used to KeyError here,
    # leaving the rs-line audit + cloud HK RS without an HSI benchmark.
    import main
    from hk_eod import fetch_hsi_kline_yf
    monkeypatch.setattr(main, "_yf_download_with_retry", lambda *a, **k: _hsi_grid())
    df = fetch_hsi_kline_yf(period="2y")
    assert df is not None and list(df["close"]) == [100.0, 101.0, 102.0]
    assert list(df.columns) == ["time_key", "open", "high", "low", "close", "volume"]


def test_fetch_hsi_kline_yf_still_accepts_multiindex_frame(monkeypatch):
    import main
    from hk_eod import fetch_hsi_kline_yf
    grid = _hsi_grid()
    multi = pd.concat({"^HSI": grid}, axis=1)  # (ticker, field) columns
    monkeypatch.setattr(main, "_yf_download_with_retry", lambda *a, **k: multi)
    df = fetch_hsi_kline_yf(period="2y")
    assert df is not None and list(df["close"]) == [100.0, 101.0, 102.0]


# --- filter_hk_shorts: sparse tickers inside a batch get retried ---


def _shorts_grid(n_rows=60, ramp=15):
    """OHLCV grid that passes every HK Shorts gate for a large cap: flat, then a
    steep 15-day ramp (SMA20 +20%, > SMA50, 2-week perf > 50%, ADR 10%, 3+ up days)."""
    closes = [10.0] * (n_rows - ramp) + [12.0 + 2.0 * i for i in range(ramp)]
    idx = pd.date_range(end="2026-09-24", periods=n_rows, freq="B")
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.05 for c in closes], "Low": [c * 0.95 for c in closes],
        "Close": closes, "Volume": [2_000_000] * n_rows,
    }, index=idx)


def test_filter_hk_shorts_retries_sparse_tickers_in_batch(monkeypatch):
    # 0002.HK comes back all-NaN from the batch download (transient Yahoo
    # "possibly delisted" — 20/25 such tickers had full data on retry,
    # 2026-09-25). The batch retry helper must be invoked so it is recovered
    # instead of silently dropped at phase 1.
    import main, hk_eod
    good = _shorts_grid()
    nan = good.astype(float) * float("nan")  # all-NaN columns, ticker still present
    batch_frame = pd.concat({"0001.HK": good, "0002.HK": nan}, axis=1)

    calls = []
    def fake_retry(data, tickers, period, min_rows):
        calls.append((list(tickers), period, min_rows))
        for f in ("Open", "High", "Low", "Close", "Volume"):
            data[("0002.HK", f)] = good[f]
        return False

    monkeypatch.setattr(hk_eod, "fetch_hkex_equities", lambda: ["0001", "0002"])
    monkeypatch.setattr(main, "_yf_download_with_retry", lambda *a, **k: batch_frame)
    monkeypatch.setattr(main, "_retry_sparse_in_batch", fake_retry)
    monkeypatch.setattr(main, "_get_market_cap", lambda t, **k: 20_000_000_000.0)
    monkeypatch.setattr(hk_eod.time, "sleep", lambda s: None)

    cfg = {"min_avg_volume": 1_000_000, "min_market_cap": 50_000_000,
           "min_dollar_volume": 50_000_000, "min_consecutive_up_days": 3,
           "min_adr_percent": 4.0, "adr_days": 20}
    universe, tv = hk_eod.filter_hk_shorts(cfg, futu_cfg=None, rs_table_3m=None)

    assert universe == 2
    assert calls == [(["0001.HK", "0002.HK"], "3mo", 50)]
    assert sorted(tv) == ["HKEX:1", "HKEX:2"]
