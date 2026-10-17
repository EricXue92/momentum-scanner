"""One download per data need, not one per consumer.

- EMA20 first-break and the SMA50 prune both read 6mo closes for the whole
  US master; us-eod fetched that batch twice back to back.
- A Longs group with `min_relative_volume` fetched the same 2mo batch twice
  (dollar-volume+ADR%, then relative volume).
Each extra batch is another chance to be throttled.
"""

from pathlib import Path

import pandas as pd
import pytest

import ema20_break
import main
import sma50_prune

TODAY = pd.Timestamp("2026-10-17").date()


def _closes(n: int = 60) -> dict[str, pd.Series]:
    idx = pd.date_range(end="2026-10-16", periods=n, freq="B")
    return {"AAA": pd.Series(100.0, index=idx), "BBB": pd.Series(50.0, index=idx)}


def _boom(*_a, **_k):
    raise AssertionError("network fetch must not happen when closes are supplied")


def test_ema20_break_uses_supplied_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(ema20_break, "_fetch_daily_closes", _boom)
    seen = tmp_path / "eod_seen_US.txt"
    seen.write_text("AAA\nBBB\n")
    out = tmp_path / "x_EMA20Break.txt"
    hits = ema20_break.write_ema20_break_list(
        seen, {"enabled": True}, out, closes=_closes()
    )
    assert hits == []  # flat series never cross; the point is: no fetch


def test_sma50_prune_uses_supplied_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(sma50_prune, "_fetch_daily_closes", _boom)
    seen = tmp_path / "eod_seen_US.txt"
    seen.write_text("AAA\nBBB\n")
    drops = sma50_prune.prune_us_master(seen, {"enabled": True}, closes=_closes())
    assert drops == []
    assert seen.read_text() == "AAA\nBBB\n"


def test_us_master_lists_fetch_once(tmp_path, monkeypatch):
    """main's wrapper fetches the master's closes once and feeds both steps."""
    calls: list[list[str]] = []

    def fake_fetch(tickers):
        calls.append(list(tickers))
        return _closes()

    monkeypatch.setattr(sma50_prune, "_fetch_daily_closes", fake_fetch)
    monkeypatch.setattr(ema20_break, "_fetch_daily_closes", _boom)
    seen = tmp_path / "state" / "eod_seen_US.txt"
    seen.parent.mkdir()
    seen.write_text("AAA\nBBB\n")
    out_dir = tmp_path / "TV" / "US"
    out_dir.mkdir(parents=True)
    config = {"ema20_break": {"enabled": True}, "sma50_prune": {"enabled": True}}
    main._run_us_master_lists(seen, config, out_dir, "2026_10_17")
    assert calls == [["AAA", "BBB"]]


def _batch(tickers: list[str]) -> pd.DataFrame:
    idx = pd.date_range(end="2026-10-16", periods=45, freq="B")
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


def test_dollar_volume_adr_accepts_prefetched_batch(monkeypatch):
    monkeypatch.setattr(main.yf, "download", _boom)
    data = _batch(["AAA", "BBB"])
    kept = main.filter_dollar_volume_and_adr_yf(
        ["AAA", "BBB"], 1.0, 1.0, batch=(data, False)
    )
    assert kept == ["AAA", "BBB"]


def test_relative_volume_accepts_prefetched_batch(monkeypatch):
    monkeypatch.setattr(main.yf, "download", _boom)
    data = _batch(["AAA", "BBB"])
    kept = main.filter_relative_volume(["AAA", "BBB"], 0.5, batch=(data, False))
    assert kept == ["AAA", "BBB"]


def test_longs_group_batch_downloads_once_with_widest_window(monkeypatch):
    calls: list[tuple[str, int]] = []

    def fake_download(tickers, period, min_rows):
        calls.append((period, min_rows))
        return _batch(list(tickers)), False

    monkeypatch.setattr(main, "_download_daily_batch", fake_download)
    cfg = {"min_relative_volume": 3, "relative_volume_days": 30}
    batch = main._longs_group_batch(["AAA", "BBB"], cfg, adr_days=20, dv_days=20)
    assert batch is not None
    assert calls == [("2mo", 31)]  # rvol needs days + 1 bars
