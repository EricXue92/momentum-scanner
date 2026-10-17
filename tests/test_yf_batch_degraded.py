"""A degraded yfinance batch must not wipe a Longs/Leaders group silently or
flag established names as IPO candidates.

2026-10-17 10:29: one throttled batch returned no usable rows for 43 names
in Leaders 52W (ABBV, CRM, DDOG, MRNA, ...). The per-ticker loop logged
"insufficient data, dropping" for each one and pushed them all into
`ipo_drops`. The download sites called `yf.download` directly (no retry) and
ignored the rate-limit signal from `_retry_sparse_in_batch`.
"""

import pandas as pd

import main

BARS = 45


def _frame(tickers: list[str], volume: float = 2_000_000.0) -> pd.DataFrame:
    idx = pd.date_range(end="2026-10-16", periods=BARS, freq="B")
    cols = pd.MultiIndex.from_product(
        [tickers, ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(index=idx, columns=cols, dtype=float)
    for t in tickers:
        df[(t, "Open")] = 100.0
        df[(t, "High")] = 105.0
        df[(t, "Low")] = 95.0
        df[(t, "Close")] = 100.0
        df[(t, "Volume")] = volume
    return df


def _no_sleep(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)


def test_dollar_volume_adr_empty_batch_does_not_flag_ipo(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []
    monkeypatch.setattr(
        main.yf, "download", lambda *a, **k: calls.append(1) or pd.DataFrame()
    )
    ipo: set[str] = set()
    kept = main.filter_dollar_volume_and_adr_yf(
        ["AAA", "BBB", "CCC"], 1.0, 1.0, ipo_drops=ipo
    )
    assert kept == []
    assert ipo == set(), "a dead batch is not evidence of an IPO"
    assert len(calls) >= 2, "an empty batch must be retried, not accepted"


def test_dollar_volume_adr_recovers_after_throttled_batch(monkeypatch):
    """First batch: 2 of 3 tickers all-NaN (>50% sparse = throttle signal).
    Second batch after the cooldown: full. All three must survive."""
    _no_sleep(monkeypatch)
    tickers = ["AAA", "BBB", "CCC"]
    full = _frame(tickers)
    sparse = full.copy()
    for t in ("BBB", "CCC"):
        sparse[t] = float("nan")
    responses = [sparse, full]
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: responses.pop(0))
    ipo: set[str] = set()
    kept = main.filter_dollar_volume_and_adr_yf(tickers, 1.0, 1.0, ipo_drops=ipo)
    assert kept == tickers
    assert ipo == set()


def test_dollar_volume_adr_still_throttled_drops_without_ipo_flag(monkeypatch):
    """Both attempts sparse: the batch is unreliable, so the missing names are
    dropped for the day but NOT recorded as IPO candidates."""
    _no_sleep(monkeypatch)
    tickers = ["AAA", "BBB", "CCC"]
    sparse = _frame(tickers)
    for t in ("BBB", "CCC"):
        sparse[t] = float("nan")
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: sparse.copy())
    ipo: set[str] = set()
    kept = main.filter_dollar_volume_and_adr_yf(tickers, 1.0, 1.0, ipo_drops=ipo)
    assert kept == ["AAA"]
    assert ipo == set()


def test_relative_volume_empty_batch_does_not_flag_ipo(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: pd.DataFrame())
    ipo: set[str] = set()
    kept = main.filter_relative_volume(["AAA", "BBB"], min_rvol=1.0, ipo_drops=ipo)
    assert kept == []
    assert ipo == set()


def test_relative_volume_recovers_after_throttled_batch(monkeypatch):
    _no_sleep(monkeypatch)
    tickers = ["AAA", "BBB", "CCC"]
    full = _frame(tickers)
    sparse = full.copy()
    for t in ("BBB", "CCC"):
        sparse[t] = float("nan")
    responses = [sparse, full]
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: responses.pop(0))
    ipo: set[str] = set()
    kept = main.filter_relative_volume(tickers, min_rvol=0.5, ipo_drops=ipo)
    assert kept == tickers
    assert ipo == set()


def test_genuinely_short_history_is_still_flagged_ipo(monkeypatch):
    """A healthy batch where one name has 5 bars: that one IS an IPO drop."""
    _no_sleep(monkeypatch)
    tickers = ["AAA", "NEWCO"]
    df = _frame(tickers)
    df.loc[df.index[:-5], ("NEWCO", slice(None))] = float("nan")
    monkeypatch.setattr(main.yf, "download", lambda *a, **k: df)
    ipo: set[str] = set()
    kept = main.filter_dollar_volume_and_adr_yf(tickers, 1.0, 1.0, ipo_drops=ipo)
    assert kept == ["AAA"]
    assert ipo == {"NEWCO"}
