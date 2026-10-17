"""A lone survivor of an earlier morning-gap stage must not be dropped.

`daily_data` is downloaded once for the whole discovery list, so its columns
are a (ticker, field) MultiIndex. When a later stage narrows the list to one
ticker, the filters used to infer `single = len(tickers) == 1` and index the
frame flat (`daily_data["High"]`) → KeyError → "check failed, dropping".
Observed 2026-10: PENG/DUOL (SMA), IONQ (ADR%), PAYC/IOT (avg vol),
2162.HK (HK avg vol). The frame's own shape must decide the indexing.
"""

import pandas as pd

import main
from futu_sync import GapQuote

TODAY = pd.Timestamp("2026-10-17").date()


def _batch_frame(tickers: list[str], bars: int = 260) -> pd.DataFrame:
    """Two-or-more-ticker yfinance batch: MultiIndex (ticker, field). Flat
    price at 100, range 95-105 (ADR% 10), volume 2M, last row dated TODAY."""
    idx = pd.date_range(end="2026-10-17", periods=bars, freq="B")
    cols = pd.MultiIndex.from_product(
        [tickers, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for t in tickers:
        df[(t, "Open")] = 100.0
        df[(t, "High")] = 105.0
        df[(t, "Low")] = 95.0
        df[(t, "Close")] = 100.0
        df[(t, "Volume")] = 2_000_000.0
    return df


def test_avg_volume_keeps_lone_survivor_in_batch_frame():
    data = _batch_frame(["AAA", "BBB"])
    assert main._filter_avg_volume(["AAA"], data, 1_000_000, 20, TODAY) == ["AAA"]


def test_adr_keeps_lone_survivor_in_batch_frame():
    data = _batch_frame(["AAA", "BBB"])
    assert main._filter_adr_percent(["AAA"], data, 4.0, 20, TODAY) == ["AAA"]


def test_sma_trend_keeps_lone_survivor_in_batch_frame():
    data = _batch_frame(["AAA", "BBB"])
    assert main._filter_sma_trend(["AAA"], data, TODAY) == ["AAA"]


def test_pre_market_volume_keeps_lone_survivor_in_batch_frame():
    data = _batch_frame(["AAA", "BBB"])
    quotes = {"AAA": GapQuote(price=110.0, gap=10.0, pre_volume=500_000.0)}
    assert main._filter_pre_market_volume(
        ["AAA"], data, quotes, 0.05, 20, TODAY
    ) == ["AAA"]


def test_dollar_volume_keeps_lone_survivor_when_single_flag_omitted():
    data = _batch_frame(["AAA", "BBB"])
    assert main._filter_dollar_volume_from_data(
        ["AAA"], data, 1_000_000, True, TODAY
    ) == ["AAA"]


def test_flat_single_ticker_frame_still_works():
    """A genuine one-ticker download (flattened) keeps indexing flat."""
    data = _batch_frame(["AAA"])["AAA"]
    assert main._filter_avg_volume(["AAA"], data, 1_000_000, 20, TODAY) == ["AAA"]
    assert main._filter_adr_percent(["AAA"], data, 4.0, 20, TODAY) == ["AAA"]
    assert main._filter_sma_trend(["AAA"], data, TODAY) == ["AAA"]


def _run_gap_with_lone_survivor(monkeypatch, *, hk: bool):
    """Drive run_morning_gap / run_hk_morning_gap with a two-ticker discovery
    where dollar volume knocks out BBB, leaving AAA alone for every later
    stage including the Phase 4 avg-volume loop."""
    tickers = ["AAA.HK", "BBB.HK"] if hk else ["AAA", "BBB"]
    data = _batch_frame(tickers)
    loser = tickers[1]
    data[(loser, "Volume")] = 10.0  # fails the $100M dollar-volume gate
    quotes = {t: GapQuote(price=110.0, gap=10.0) for t in tickers}

    monkeypatch.setattr(main, "_yf_download_with_retry", lambda *a, **k: data)
    monkeypatch.setattr(
        main, "intraday_cumulative_volume_futu", lambda t, *a, **k: list(t)
    )
    cfg = {
        "scan_offsets": [10],
        "min_dollar_volume": 100_000_000,
        "min_adr_percent": 4.0,
        "min_avg_volume": 1_000_000,
    }
    if hk:
        monkeypatch.setattr(main, "_get_hkt_scan_offset", lambda *a, **k: 10)
        monkeypatch.setattr(
            main, "discover_hk_morning_gap_candidates", lambda **k: quotes
        )
        return main.run_hk_morning_gap(cfg, futu_cfg={"enabled": True})
    monkeypatch.setattr(main, "_get_et_scan_offset", lambda *a, **k: 10)
    monkeypatch.setattr(main, "discover_morning_gap_candidates", lambda **k: quotes)
    return main.run_morning_gap(cfg, futu_cfg={"enabled": True})


def test_us_morning_gap_emits_lone_survivor(monkeypatch):
    offset, kept = _run_gap_with_lone_survivor(monkeypatch, hk=False)
    assert offset == 10
    assert kept == ["AAA"]


def test_hk_morning_gap_emits_lone_survivor(monkeypatch):
    offset, kept = _run_gap_with_lone_survivor(monkeypatch, hk=True)
    assert offset == 10
    assert kept == ["AAA.HK"]
