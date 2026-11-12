"""Tests for scripts/compute_us_rs_3m_cloud.py — the GH Actions entrypoint.

These tests fully monkeypatch out the external dependencies (Fred6725 CSV
fetch, yfinance batch download, SPY fetch) so the unit tests are fast and
deterministic. The script itself is small — its real integration test is
the workflow's first `workflow_dispatch` run.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

# scripts/ is not a package; tests need a path hack to import.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import compute_us_rs_3m_cloud as cloud  # noqa: E402


def _flat_then_jump(start: float, jump_pct: float, n: int = 70) -> pd.DataFrame:
    closes = [start] * (n - 1) + [start * (1 + jump_pct / 100)]
    return pd.DataFrame({
        "time_key": pd.date_range(end="2026-05-21", periods=n, freq="B"),
        "close": closes,
    })


def test_main_writes_csv_and_exits_zero_when_coverage_above_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: 3/3 universe scored → CSV written → exit 0."""
    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **kw: {"AAA": 99, "BBB": 90, "CCC": 50})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **kw: _flat_then_jump(400.0, 0))
    monkeypatch.setattr(
        cloud, "fetch_us_klines_yf",
        lambda tickers, **kw: {t: _flat_then_jump(100.0, jump_pct=5 + i * 5) for i, t in enumerate(tickers)},
    )
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "data" / "us_rs_3m")
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))

    exit_code = cloud.main()

    assert exit_code == 0
    csv = tmp_path / "data" / "us_rs_3m" / "2026-05-22.csv"
    assert csv.exists()
    df = pd.read_csv(csv, index_col="ticker")
    assert set(df.index) == {"AAA", "BBB", "CCC"}
    assert "raw_score" in df.columns
    assert "rs_percentile" in df.columns


def test_main_exits_nonzero_when_coverage_below_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """1 of 5 universe tickers scored = 20% coverage → exit 1, no CSV."""
    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **kw: {"A": 99, "B": 90, "C": 50, "D": 30, "E": 10})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **kw: _flat_then_jump(400.0, 0))
    monkeypatch.setattr(
        cloud, "fetch_us_klines_yf",
        lambda tickers, **kw: {"A": _flat_then_jump(100.0, 5)},  # 1/5 only
    )
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "data" / "us_rs_3m")
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))

    exit_code = cloud.main()

    assert exit_code == 1
    csv = tmp_path / "data" / "us_rs_3m" / "2026-05-22.csv"
    assert not csv.exists(), "no CSV should be written when coverage guard trips"


def test_main_exits_nonzero_when_fred6725_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fred6725 CSV unavailable → exit 1, no compute attempted."""
    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **kw: None)
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "data" / "us_rs_3m")
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))

    # If main() doesn't short-circuit on None universe, the test would also
    # need to mock _fetch_spy_kline / fetch_us_klines_yf. The assertion that
    # exit_code == 1 with NO further mocks means the short-circuit works.
    exit_code = cloud.main()

    assert exit_code == 1
    csv = tmp_path / "data" / "us_rs_3m" / "2026-05-22.csv"
    assert not csv.exists()


def test_main_prunes_files_older_than_retention_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a successful write, files older than 14 days are deleted."""
    data_dir = tmp_path / "data" / "us_rs_3m"
    data_dir.mkdir(parents=True)
    # 13 days old → keep; 15 days old → prune; non-date filename → keep.
    (data_dir / "2026-05-09.csv").write_text("ticker,raw_score,rs_percentile\n")  # 13 days old
    (data_dir / "2026-05-07.csv").write_text("ticker,raw_score,rs_percentile\n")  # 15 days old
    (data_dir / "README.md").write_text("docs")  # non-date, keep

    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **kw: {"AAA": 99, "BBB": 90, "CCC": 50})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **kw: _flat_then_jump(400.0, 0))
    monkeypatch.setattr(
        cloud, "fetch_us_klines_yf",
        lambda tickers, **kw: {t: _flat_then_jump(100.0, 5 + i * 5) for i, t in enumerate(tickers)},
    )
    monkeypatch.setattr(cloud, "_DATA_DIR", data_dir)
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))

    exit_code = cloud.main()

    assert exit_code == 0
    assert (data_dir / "2026-05-22.csv").exists(), "today's file written"
    assert (data_dir / "2026-05-09.csv").exists(), "13-day-old file kept"
    assert not (data_dir / "2026-05-07.csv").exists(), "15-day-old file pruned"
    assert (data_dir / "README.md").exists(), "non-date file untouched"


def test_csv_has_rs_line_columns(tmp_path, monkeypatch):
    """The published US CSV gains the three rs_line columns."""
    n = 80
    idx = pd.bdate_range("2026-01-01", periods=n)
    def _kl(start_price, slope):
        return pd.DataFrame({"time_key": idx,
                             "close": [start_price + slope * i for i in range(n)]})
    klines = {"AAA": _kl(100, 1.0), "BBB": _kl(200, -1.0)}  # AAA up, BBB down
    spy = pd.DataFrame({"time_key": idx, "close": [100.0] * n})

    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **k: {"AAA": 90, "BBB": 80})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: spy)
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", lambda *a, **k: klines)
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud, "_today", lambda: __import__("datetime").date(2026, 5, 27))

    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "2026-05-27.csv", index_col="ticker")
    for col in ("rs_below_ma", "rs_days_below_ma", "rs_frac_below_ma"):
        assert col in out.columns
    assert int(out.loc["BBB", "rs_below_ma"]) == 1   # falling line below MA
    assert int(out.loc["AAA", "rs_below_ma"]) == 0   # rising line above MA


def test_csv_has_a_market_wide_1m_percentile_column(tmp_path, monkeypatch):
    """The 1M weekly ladder is scored on the same klines, no extra download."""
    n = 80
    idx = pd.bdate_range("2026-01-01", periods=n)
    def _kl(last_pct):
        closes = [100.0] * (n - 6) + [100.0 * (1 + last_pct / 100)] * 6
        return pd.DataFrame({"time_key": idx, "close": closes})
    klines = {"STRONG": _kl(40), "MID": _kl(10), "WEAK": _kl(-20)}
    spy = pd.DataFrame({"time_key": idx, "close": [100.0] * n})
    fetches = []
    def fake_klines(*a, **k):
        fetches.append(a)
        return klines

    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **k: {"STRONG": 90, "MID": 80, "WEAK": 10})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: spy)
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", fake_klines)
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 11, 4))

    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "2026-11-04.csv", index_col="ticker")
    assert out.loc["STRONG", "rs_percentile_1m"] == 99
    assert out.loc["STRONG", "rs_percentile_1m"] > out.loc["MID", "rs_percentile_1m"] > out.loc["WEAK", "rs_percentile_1m"]
    assert len(fetches) == 1


def test_a_1m_scoring_failure_does_not_cost_the_3m_table(tmp_path, monkeypatch):
    real = cloud.compute_us_rs_3m_table
    def fail_on_1m(klines, spy, weights_bars=None):
        if weights_bars is not None:
            raise ValueError("boom")
        return real(klines, spy)
    monkeypatch.setattr(cloud, "compute_us_rs_3m_table", fail_on_1m)
    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **k: {"AAA": 99})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: _flat_then_jump(400.0, 0))
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", lambda t, **k: {"AAA": _flat_then_jump(100.0, 5)})
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 11, 4))

    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "2026-11-04.csv", index_col="ticker")
    assert "rs_percentile" in out.columns
    assert "rs_percentile_1m" not in out.columns


def test_universe_fetch_does_not_wait_for_a_fresh_table(monkeypatch):
    """2026-11-11/12: the freshness poll (<= 60 min) consumed the job's
    60-min GH timeout, so no 3M table was published before us-eod."""
    seen: dict = {}

    def fake_fetch(*a, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(cloud, "fetch_rs_table", fake_fetch)
    assert cloud.main() == 1
    assert seen.get("wait_for_fresh") is False


def test_spy_failure_publishes_nothing(monkeypatch, tmp_path):
    """Without SPY the table's raw_score would be absolute, while the local
    IPO gate subtracts its own SPY score before ranking against it. No table
    is better: the local run walks back to yesterday's coherent one."""
    fetched: list = []
    monkeypatch.setattr(cloud, "fetch_rs_table", lambda *a, **kw: {"AAA": 99, "BBB": 90})
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **kw: None)
    monkeypatch.setattr(
        cloud, "fetch_us_klines_yf", lambda tickers, **kw: fetched.append(tickers) or {}
    )
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "data" / "us_rs_3m")
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))

    assert cloud.main() == 1
    assert fetched == []  # no point paying for the universe download
    assert not (tmp_path / "data" / "us_rs_3m" / "2026-05-22.csv").exists()
