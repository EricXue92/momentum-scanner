"""yfinance `group_by="ticker"` returns a (ticker, field) MultiIndex even for a
single ticker (yfinance 1.x), but the `single` branches in main.py index
`data["Close"]` / `data["Volume"]` flat. Without normalisation a one-hit day
(e.g. TheSetup → ACVA 2026-09-11) drops its only candidate with
"failed to process ..., dropping"."""
import pandas as pd
import pytest

import main


def _multiindex_frame(ticker: str, volumes: list[int]) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01", periods=len(volumes), freq="B")
    cols = pd.MultiIndex.from_product([[ticker], ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(index=idx, columns=cols, dtype=float)
    df[(ticker, "Close")] = 10.0
    df[(ticker, "High")] = 10.5
    df[(ticker, "Low")] = 9.5
    df[(ticker, "Open")] = 10.0
    df[(ticker, "Volume")] = volumes
    return df


def test_flatten_single_ticker_frame_unwraps_multiindex():
    df = _multiindex_frame("ACVA", [100] * 25)
    flat = main._flatten_single_ticker_frame(df, ["ACVA"])
    assert list(flat.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert flat["Volume"].iloc[-1] == 100


def test_flatten_single_ticker_frame_leaves_batch_untouched():
    a = _multiindex_frame("AAA", [1] * 3)
    b = _multiindex_frame("BBB", [2] * 3)
    df = pd.concat([a, b], axis=1)
    assert main._flatten_single_ticker_frame(df, ["AAA", "BBB"]) is df


def test_flatten_single_ticker_frame_leaves_flat_untouched():
    df = _multiindex_frame("ACVA", [100] * 3)["ACVA"]
    assert main._flatten_single_ticker_frame(df, ["ACVA"]) is df


def test_filter_relative_volume_keeps_single_ticker_from_multiindex(monkeypatch):
    # 20 quiet days then one 5x day → rvol 5 ≥ 3.
    df = _multiindex_frame("ACVA", [1_000_000] * 20 + [5_000_000])
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: df)
    monkeypatch.setattr(main, "_retry_sparse_in_batch", lambda *a, **k: False)
    assert main.filter_relative_volume(["ACVA"], min_rvol=3, days=20) == ["ACVA"]
