"""Tests for sma50_prune — daily prune of eod_seen_US.txt tickers whose close
has been below SMA50 for N consecutive completed days and is still declining
(latest close below the prior day's close).

yfinance is never hit: the fetch layer is monkeypatched.
"""

from pathlib import Path

import pandas as pd
import pytest

import sma50_prune


def _series(closes: list[float]) -> pd.Series:
    idx = pd.bdate_range(end="2026-08-21", periods=len(closes))
    return pd.Series(closes, index=idx, dtype=float)


# --- find_sma50_drops (pure logic) ---


def test_two_declining_closes_below_sma50_is_dropped():
    closes = [100.0] * 58 + [92.0, 90.0]  # both below SMA50, day 2 lower
    drops = sma50_prune.find_sma50_drops({"AAA": _series(closes)})
    assert drops == ["AAA"]


def test_two_below_sma50_but_flat_is_kept():
    closes = [100.0] * 58 + [90.0, 90.0]  # both below, but no further decline
    drops = sma50_prune.find_sma50_drops({"AAA": _series(closes)})
    assert drops == []


def test_two_below_sma50_but_rebounding_is_kept():
    closes = [100.0] * 58 + [88.0, 90.0]  # both below, day 2 back up
    drops = sma50_prune.find_sma50_drops({"AAA": _series(closes)})
    assert drops == []


def test_only_latest_close_below_sma50_is_kept():
    closes = [100.0] * 58 + [110.0, 90.0]  # day-2 above, only latest below
    drops = sma50_prune.find_sma50_drops({"AAA": _series(closes)})
    assert drops == []


def test_dipped_then_recovered_is_kept():
    closes = [100.0] * 58 + [90.0, 110.0]  # latest close back above SMA50
    drops = sma50_prune.find_sma50_drops({"AAA": _series(closes)})
    assert drops == []


def test_insufficient_history_is_kept():
    closes = [1.0] * 30  # < sma_period bars: SMA50 undefined -> keep
    drops = sma50_prune.find_sma50_drops({"AAA": _series(closes)})
    assert drops == []


def test_consecutive_days_knob():
    # below (and declining) for exactly 2 days: dropped at consecutive_days=2,
    # kept at 3
    closes = [100.0] * 58 + [92.0, 90.0]
    assert sma50_prune.find_sma50_drops(
        {"AAA": _series(closes)}, consecutive_days=3
    ) == []


# --- prune_us_master (I/O wrapper) ---


def _write_master(tmp_path: Path, tickers: list[str]) -> Path:
    p = tmp_path / "eod_seen_US.txt"
    p.write_text("\n".join(tickers) + "\n")
    return p


def test_prune_removes_drops_and_backs_up(tmp_path, monkeypatch):
    seen = _write_master(tmp_path, ["AAA", "BBB", "CCC"])
    weak = [100.0] * 58 + [92.0, 90.0]
    strong = [100.0] * 60
    monkeypatch.setattr(
        sma50_prune,
        "_fetch_daily_closes",
        lambda tickers: {
            "AAA": _series(weak),
            "BBB": _series(strong),
            "CCC": _series(strong),
        },
    )
    drops = sma50_prune.prune_us_master(seen, {"enabled": True})
    assert drops == ["AAA"]
    assert seen.read_text().split() == ["BBB", "CCC"]
    backups = list(tmp_path.glob("eod_seen_US.txt.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text().split() == ["AAA", "BBB", "CCC"]


def test_fetch_failure_skips_prune(tmp_path, monkeypatch):
    seen = _write_master(tmp_path, ["AAA", "BBB"])
    monkeypatch.setattr(sma50_prune, "_fetch_daily_closes", lambda tickers: None)
    drops = sma50_prune.prune_us_master(seen, {"enabled": True})
    assert drops == []
    assert seen.read_text().split() == ["AAA", "BBB"]
    assert list(tmp_path.glob("*.bak.*")) == []


def test_ticker_missing_from_fetch_is_kept(tmp_path, monkeypatch):
    seen = _write_master(tmp_path, ["AAA", "BBB"])
    weak = [100.0] * 58 + [92.0, 90.0]
    monkeypatch.setattr(
        sma50_prune,
        "_fetch_daily_closes",
        lambda tickers: {"AAA": _series(weak)},  # BBB absent -> keep
    )
    drops = sma50_prune.prune_us_master(seen, {"enabled": True})
    assert drops == ["AAA"]
    assert seen.read_text().split() == ["BBB"]


def test_disabled_config_is_noop(tmp_path, monkeypatch):
    seen = _write_master(tmp_path, ["AAA"])

    def _boom(tickers):  # pragma: no cover - must not be called
        raise AssertionError("fetch should not run when disabled")

    monkeypatch.setattr(sma50_prune, "_fetch_daily_closes", _boom)
    drops = sma50_prune.prune_us_master(seen, {"enabled": False})
    assert drops == []
    assert seen.read_text().split() == ["AAA"]


def test_missing_master_is_noop(tmp_path, monkeypatch):
    seen = tmp_path / "eod_seen_US.txt"  # never written
    monkeypatch.setattr(
        sma50_prune, "_fetch_daily_closes", lambda tickers: {}
    )
    drops = sma50_prune.prune_us_master(seen, {"enabled": True})
    assert drops == []
    assert not seen.exists()


# --- fill_missing_last_close (Yahoo daily Close lag; CF 2026-09-22) ---


def _series_nan_last(closes: list[float]) -> pd.Series:
    """Daily closes whose latest row is NaN (Yahoo published OHLV but no Close)."""
    idx = pd.bdate_range(end="2026-09-22", periods=len(closes) + 1)
    return pd.Series(closes + [float("nan")], index=idx, dtype=float)


def test_fill_uses_intraday_close_for_trailing_nan_only():
    calls = []

    def fake_intraday(tickers, day):
        calls.append((sorted(tickers), day))
        return {"CF": 120.59}

    closes = {"CF": _series_nan_last([123.27]), "OK": _series([100.0, 101.0])}
    out = sma50_prune.fill_missing_last_close(closes, fetch_intraday=fake_intraday)
    assert calls == [(["CF"], pd.Timestamp("2026-09-22").date())]
    assert out["CF"].iloc[-1] == 120.59 and out["CF"].index[-1] == pd.Timestamp("2026-09-22")
    assert out["OK"].equals(closes["OK"])


def test_fill_drops_the_day_when_intraday_has_nothing():
    closes = {"CF": _series_nan_last([123.27])}
    out = sma50_prune.fill_missing_last_close(closes, fetch_intraday=lambda t, d: {})
    assert list(out["CF"]) == [123.27]
    assert out["CF"].index[-1] == pd.Timestamp("2026-09-21")


def test_fill_skips_fetch_when_no_close_is_missing():
    def boom(t, d):
        raise AssertionError("must not fetch intraday")

    closes = {"OK": _series([100.0, 101.0])}
    out = sma50_prune.fill_missing_last_close(closes, fetch_intraday=boom)
    assert out["OK"].equals(closes["OK"])


def test_fetch_daily_closes_fills_nan_close_then_rule_drops(monkeypatch):
    """CF 2026-09-22: batch daily frame has OHLV but NaN Close on the last row;
    with the intraday fill the ticker sees 2 declining closes below SMA50."""
    import main

    idx = pd.bdate_range(end="2026-09-22", periods=60)
    closes = [100.0] * 58 + [92.0, float("nan")]
    frame = pd.DataFrame(
        {("CF", "Close"): closes, ("CF", "Volume"): [1e6] * 60,
         ("ZZ", "Close"): [100.0] * 60, ("ZZ", "Volume"): [1e6] * 60},
        index=idx,
    )
    monkeypatch.setattr(main, "_yf_download_with_retry", lambda *a, **k: frame)
    monkeypatch.setattr(sma50_prune, "_fetch_intraday_last_close",
                        lambda tickers, day: {"CF": 90.0})
    got = sma50_prune._fetch_daily_closes(["CF", "ZZ"])
    assert got["CF"].iloc[-1] == 90.0 and len(got["CF"]) == 60
    assert sma50_prune.find_sma50_drops(got) == ["CF"]
