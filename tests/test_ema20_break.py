"""Tests for ema20_break — daily list of US master tickers whose close first
fell below EMA20 (list only, master untouched).

yfinance is never hit: the fetch layer is monkeypatched.
"""

from pathlib import Path

import pandas as pd

import ema20_break


def _series(closes: list[float]) -> pd.Series:
    idx = pd.bdate_range(end="2026-10-07", periods=len(closes))
    return pd.Series(closes, index=idx, dtype=float)


# --- find_ema20_first_breaks (pure logic) ---


def test_first_close_below_ema20_is_listed():
    closes = [100.0] * 40 + [90.0]  # yesterday on the line, today below
    assert ema20_break.find_ema20_first_breaks({"AAA": _series(closes)}) == ["AAA"]


def test_second_day_below_ema20_is_not_listed():
    closes = [100.0] * 40 + [90.0, 89.0]  # crossed yesterday, not first today
    assert ema20_break.find_ema20_first_breaks({"AAA": _series(closes)}) == []


def test_above_ema20_is_not_listed():
    closes = [100.0 + i for i in range(41)]  # steady uptrend
    assert ema20_break.find_ema20_first_breaks({"AAA": _series(closes)}) == []


def test_insufficient_history_is_skipped():
    closes = [100.0] * 10 + [90.0]
    assert ema20_break.find_ema20_first_breaks({"AAA": _series(closes)}) == []


# --- write_ema20_break_list (I/O wrapper) ---


def _setup(tmp_path: Path, monkeypatch, closes_by_ticker):
    seen = tmp_path / "eod_seen_US.txt"
    seen.write_text("\n".join(["AAA", "BBB"]) + "\n")
    out_dir = tmp_path / "TV"
    out_dir.mkdir()
    monkeypatch.setattr(ema20_break, "_fetch_daily_closes", lambda t: closes_by_ticker)
    return seen, out_dir


def test_writes_list_and_leaves_master_untouched(tmp_path, monkeypatch):
    seen, out_dir = _setup(tmp_path, monkeypatch, {
        "AAA": _series([100.0] * 40 + [90.0]),
        "BBB": _series([100.0] * 41),
    })
    out = out_dir / "2026_10_08_EMA20Break.txt"
    hits = ema20_break.write_ema20_break_list(seen, {"enabled": True}, out)
    assert hits == ["AAA"]
    assert out.read_text() == "AAA\n"
    assert seen.read_text().split() == ["AAA", "BBB"]
    assert list(tmp_path.glob("*.bak.*")) == []


def test_no_hits_writes_nothing(tmp_path, monkeypatch):
    seen, out_dir = _setup(tmp_path, monkeypatch, {"AAA": _series([100.0] * 41)})
    out = out_dir / "2026_10_08_EMA20Break.txt"
    assert ema20_break.write_ema20_break_list(seen, {"enabled": True}, out) == []
    assert not out.exists()


def test_keeps_only_newest_three_lists(tmp_path, monkeypatch):
    seen, out_dir = _setup(tmp_path, monkeypatch, {"AAA": _series([100.0] * 40 + [90.0])})
    for d in ("2026_10_01", "2026_10_02", "2026_10_06", "2026_10_07"):
        (out_dir / f"{d}_EMA20Break.txt").write_text("X\n")
    (out_dir / "2026_10_01_SMA50Pruned.txt").write_text("Y\n")  # other lists untouched
    ema20_break.write_ema20_break_list(
        seen, {"enabled": True}, out_dir / "2026_10_08_EMA20Break.txt"
    )
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "2026_10_01_SMA50Pruned.txt",
        "2026_10_06_EMA20Break.txt",
        "2026_10_07_EMA20Break.txt",
        "2026_10_08_EMA20Break.txt",
    ]


def test_fetch_failure_and_disabled_are_noops(tmp_path, monkeypatch):
    seen, out_dir = _setup(tmp_path, monkeypatch, None)
    out = out_dir / "2026_10_08_EMA20Break.txt"
    assert ema20_break.write_ema20_break_list(seen, {"enabled": True}, out) == []
    assert ema20_break.write_ema20_break_list(seen, {"enabled": False}, out) == []
    assert not out.exists()
