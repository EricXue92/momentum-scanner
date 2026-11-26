from datetime import date as _date

import pandas as pd
import pytest

from hk_rs import (
    WEIGHTS_12M,
    WEIGHTS_3M,
    _extract_rs_line,
    _score_from_kline,
    cache_path,
    compute_rs_table,
    filter_by_rs,
    load_cache,
    save_cache,
)


def _flat_then_jump(start_price: float, jump_pct: float, n: int = 260):
    closes = [start_price] * (n - 1) + [start_price * (1 + jump_pct / 100)]
    return pd.DataFrame({
        "time_key": pd.date_range(end="2026-05-05", periods=n, freq="B"),
        "close": closes,
    })


def test_compute_rs_table_relative_to_hsi():
    # Universe of 5 tickers with monotonically increasing 12mo returns.
    klines = {
        f"HK.000{i:02d}": _flat_then_jump(100.0, jump_pct=10 + i * 5)
        for i in range(1, 6)
    }
    # HSI flat → relative score = absolute score, percentiles 0..99 should
    # rank ascending by jump_pct.
    hsi = _flat_then_jump(20000.0, jump_pct=0)
    table = compute_rs_table(klines, hsi)

    # Index should match input codes; rs_percentile in [0, 99]
    assert set(table.index) == set(klines.keys())
    assert table["rs_percentile"].between(0, 99).all()
    # Highest jump should get the highest percentile
    top = table["rs_percentile"].idxmax()
    assert top == "HK.00005"


def test_filter_by_rs_passthrough_for_missing():
    table = pd.DataFrame({"rs_percentile": [95, 50]}, index=["HK.AAA", "HK.BBB"])
    out = filter_by_rs(["HK.AAA", "HK.BBB", "HK.CCC"], table, threshold=90)
    # AAA passes; BBB fails; CCC missing → kept (matches rs_rating.py policy)
    assert set(out) == {"HK.AAA", "HK.CCC"}


def test_filter_by_rs_none_table_passthrough():
    out = filter_by_rs(["HK.AAA", "HK.BBB"], None, threshold=90)
    assert out == ["HK.AAA", "HK.BBB"]


def test_score_from_kline_default_weights_unchanged():
    # 260 行平盘 + 末尾跳 +20% → 12M 分数应等于 0.4·0.2 + 0.2·0.2 + 0.2·0.2 + 0.2·0.2 = 0.20
    df = _flat_then_jump(100.0, jump_pct=20, n=260)
    score, reason = _score_from_kline(df)  # 默认 WEIGHTS_12M
    assert reason == "ok"
    assert abs(score - 0.20) < 1e-9


def test_score_from_kline_3m_weights_short_history_ok():
    # 70 行平盘 + 末尾跳 +10% → 3M 算法仅需 max(3)*21 + 1 = 64 行
    # 分数 = 0.4·0.1 + 0.3·0.1 + 0.3·0.1 = 0.10
    df = _flat_then_jump(100.0, jump_pct=10, n=70)
    score, reason = _score_from_kline(df, weights=WEIGHTS_3M)
    assert reason == "ok"
    assert abs(score - 0.10) < 1e-9


def test_score_from_kline_3m_weights_each_lookback_0_4_0_3_0_3():
    # R21 / R42 / R63 各不相同, 才能把三个权重分开:
    # RS_3M = 0.4·R21 + 0.3·R42 + 0.3·R63 (2026-11-26 起; 之前 0.5 / 0.3 / 0.2)
    closes = [90.0] * 70
    closes[-64] = 80.0    # R63 = 50%
    closes[-43] = 100.0   # R42 = 20%
    closes[-22] = 110.0   # R21 = 120/110 - 1
    closes[-1] = 120.0
    df = pd.DataFrame({
        "time_key": pd.date_range(end="2026-05-05", periods=70, freq="B"),
        "close": closes,
    })
    score, reason = _score_from_kline(df, weights=WEIGHTS_3M)
    assert reason == "ok"
    expected = 0.4 * (120 / 110 - 1) + 0.3 * 0.20 + 0.3 * 0.50
    assert abs(score - expected) < 1e-12


def test_3m_weights_are_the_same_ladder_in_both_markets():
    # US (us_rs_3m, 也是 ETF / IPO 用的那一份) 与 HK 各有一个常量, 必须一起改
    import us_rs_3m
    assert WEIGHTS_3M == us_rs_3m.WEIGHTS_3M


def test_score_from_kline_3m_weights_reject_when_below_min_rows():
    # 60 行 < 64 → short_history
    df = _flat_then_jump(100.0, jump_pct=10, n=60)
    score, reason = _score_from_kline(df, weights=WEIGHTS_3M)
    assert score is None
    assert reason == "short_history"


def test_compute_rs_table_with_3m_weights_uses_shorter_history():
    # 5 个 ticker 各有 70 行历史 (< 253，12M 会全部 short_history rejected)，
    # 但 3M 仅需 64 行 → 全部可打分。
    klines = {
        f"HK.000{i:02d}": _flat_then_jump(100.0, jump_pct=5 + i * 2, n=70)
        for i in range(1, 6)
    }
    hsi = _flat_then_jump(20000.0, jump_pct=0, n=70)
    table = compute_rs_table(klines, hsi, weights=WEIGHTS_3M, label="3M")

    assert set(table.index) == set(klines.keys())
    assert table["rs_percentile"].between(0, 99).all()
    # 跳幅最大的应在最高百分位
    assert table["rs_percentile"].idxmax() == "HK.00005"


def test_compute_rs_table_with_3m_weights_logs_label(caplog):
    klines = {"HK.00001": _flat_then_jump(100.0, jump_pct=5, n=70)}
    hsi = _flat_then_jump(20000.0, jump_pct=0, n=70)
    with caplog.at_level("INFO"):
        compute_rs_table(klines, hsi, weights=WEIGHTS_3M, label="3M")
    assert any("[HK RS 3M]" in r.message for r in caplog.records)


def test_cache_path_default_suffix_unchanged(tmp_path):
    p = cache_path(_date(2026, 5, 21), tmp_path)
    assert p == tmp_path / "state" / "hk_rs_rating_2026-05-21.csv"


def test_cache_path_with_3m_suffix(tmp_path):
    p = cache_path(_date(2026, 5, 21), tmp_path, suffix="3m")
    assert p == tmp_path / "state" / "hk_rs_rating_3m_2026-05-21.csv"


def test_save_and_load_3m_cache_roundtrip(tmp_path):
    df = pd.DataFrame({"rs_percentile": [95, 50]}, index=["HK.00001", "HK.00002"])
    df.index.name = "code"
    save_cache(df, _date(2026, 5, 21), tmp_path, suffix="3m")

    loaded = load_cache(_date(2026, 5, 21), tmp_path, suffix="3m")
    assert loaded is not None
    assert list(loaded.index) == ["HK.00001", "HK.00002"]
    assert loaded.loc["HK.00001", "rs_percentile"] == 95

    # 默认 suffix 路径不应找到这个文件
    assert load_cache(_date(2026, 5, 21), tmp_path) is None


def test_extract_rs_line_pulls_ma_columns_and_drops_legacy():
    combined = pd.DataFrame(
        {
            "rs_percentile_12m": [85.0, 40.0],
            "rs_below_ma": [0, 1],
            "rs_days_below_ma": [0, 5],
            "rs_frac_below_ma": [0.0, 0.8],
            "rs_pct_off_high": [0.004, 0.12],  # legacy column in older CSVs — must be ignored
        },
        index=pd.Index(["HK.00001", "HK.00002"], name="code"),
    )
    out = _extract_rs_line(combined)
    assert list(out.columns) == ["rs_below_ma", "rs_days_below_ma", "rs_frac_below_ma"]
    assert "rs_pct_off_high" not in out.columns
    assert int(out.loc["HK.00002", "rs_days_below_ma"]) == 5
