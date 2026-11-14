"""Tests for scripts/compute_us_rs_3m_cloud.py — the GH Actions entrypoint.

These tests fully monkeypatch out the external dependencies (nasdaqtrader
universe, yfinance batch download, SPY fetch) so the unit tests are fast and
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


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real sleeps, no writes into the repo's data/, and the expected
    session = whatever the fake SPY's last bar is (the fixtures use fixed
    historical dates)."""
    monkeypatch.setattr(cloud.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cloud, "_DATA_DIR_12M", tmp_path / "isolated_12m")
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "isolated_3m")
    monkeypatch.setattr(cloud, "expected_session", lambda: _SESSION[0])


# Last bar of the fake klines in a test; `_flat_then_jump` ends on 2026-05-21.
_SESSION = [date(2026, 5, 21)]


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch):
    def _set(d: date) -> None:
        monkeypatch.setattr(cloud, "expected_session", lambda: d)
    return _set


def _flat_then_jump(start: float, jump_pct: float, n: int = 150) -> pd.DataFrame:
    closes = [start] * (n - 1) + [start * (1 + jump_pct / 100)]
    return pd.DataFrame({
        "time_key": pd.date_range(end="2026-05-21", periods=n, freq="B"),
        "close": closes,
    })


def test_main_writes_csv_and_exits_zero_when_coverage_above_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Happy path: 3/3 universe scored → CSV written → exit 0."""
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["AAA", "BBB", "CCC"])
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
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["A", "B", "C", "D", "E"])
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


def test_main_exits_nonzero_without_a_universe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """nasdaqtrader down and nothing published before → exit 1, no compute."""
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: None)
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

    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["AAA", "BBB", "CCC"])
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


def test_csv_has_rs_line_columns(tmp_path, monkeypatch, session):
    """The published US CSV gains the three rs_line columns."""
    n = 150
    idx = pd.bdate_range("2026-01-01", periods=n)
    session(idx[-1].date())
    def _kl(start_price, slope):
        return pd.DataFrame({"time_key": idx,
                             "close": [start_price + slope * i for i in range(n)]})
    klines = {"AAA": _kl(100, 1.0), "BBB": _kl(200, -1.0)}  # AAA up, BBB down
    spy = pd.DataFrame({"time_key": idx, "close": [100.0] * n})

    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["AAA", "BBB"])
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


def test_csv_has_a_market_wide_1m_percentile_column(tmp_path, monkeypatch, session):
    """The 1M weekly ladder is scored on the same klines, no extra download."""
    n = 150
    idx = pd.bdate_range("2026-01-01", periods=n)
    session(idx[-1].date())
    def _kl(last_pct):
        closes = [100.0] * (n - 6) + [100.0 * (1 + last_pct / 100)] * 6
        return pd.DataFrame({"time_key": idx, "close": closes})
    klines = {"STRONG": _kl(40), "MID": _kl(10), "WEAK": _kl(-20)}
    spy = pd.DataFrame({"time_key": idx, "close": [100.0] * n})
    fetches = []
    def fake_klines(*a, **k):
        fetches.append(a)
        return klines

    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["STRONG", "MID", "WEAK"])
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
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["AAA"])
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: _flat_then_jump(400.0, 0))
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", lambda t, **k: {"AAA": _flat_then_jump(100.0, 5)})
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 11, 4))

    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "2026-11-04.csv", index_col="ticker")
    assert "rs_percentile" in out.columns
    assert "rs_percentile_1m" not in out.columns


def test_spy_failure_publishes_nothing(monkeypatch, tmp_path):
    """Without SPY the table's raw_score would be absolute, while the local
    IPO gate subtracts its own SPY score before ranking against it. No table
    is better: the local run walks back to yesterday's coherent one."""
    fetched: list = []
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **kw: ["AAA", "BBB"])
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **kw: None)
    monkeypatch.setattr(
        cloud, "fetch_us_klines_yf", lambda tickers, **kw: fetched.append(tickers) or {}
    )
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "data" / "us_rs_3m")
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))

    assert cloud.main() == 1
    assert fetched == []  # no point paying for the universe download
    assert not (tmp_path / "data" / "us_rs_3m" / "2026-05-22.csv").exists()


# --- 12M table + latest-bar validation (2026-11-14) -------------------------


def _trend(start: float, daily: float, n: int = 300, end: str = "2026-05-21") -> pd.DataFrame:
    return pd.DataFrame({
        "time_key": pd.bdate_range(end=end, periods=n),
        "close": [start * (1 + daily) ** i for i in range(n)],
    })


def _patch_12m(monkeypatch, tmp_path, klines, spy=None):
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **k: sorted(klines))
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: _trend(400.0, 0.0) if spy is None else spy)
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", lambda tickers, **k: {t: klines[t] for t in tickers if t in klines})
    monkeypatch.setattr(cloud, "_DATA_DIR", tmp_path / "3m")
    monkeypatch.setattr(cloud, "_DATA_DIR_12M", tmp_path / "12m")
    monkeypatch.setattr(cloud, "_today", lambda: date(2026, 5, 22))


def test_main_publishes_the_12m_table_next_to_the_3m_one(monkeypatch, tmp_path):
    klines = {"UP": _trend(100, 0.002), "FLAT": _trend(100, 0.0), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)

    assert cloud.main() == 0
    assert (tmp_path / "3m" / "2026-05-22.csv").exists()
    out = pd.read_csv(tmp_path / "12m" / "2026-05-22.csv", index_col="Ticker")
    assert list(out.index) == ["UP", "FLAT", "DOWN"]  # strongest first
    assert list(out.columns) == ["Relative Strength", "Percentile", "Session"]
    assert set(out["Session"]) == {"2026-05-21"}  # the data day, not the run date
    assert out.loc["UP", "Percentile"] > out.loc["DOWN", "Percentile"]


def test_published_12m_table_is_readable_by_rs_rating(monkeypatch, tmp_path):
    import rs_rating

    klines = {"UP": _trend(100, 0.002), "FLAT": _trend(100, 0.0), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)
    assert cloud.main() == 0
    text = (tmp_path / "12m" / "2026-05-22.csv").read_text()
    assert rs_rating.table_data_session(text) == date(2026, 5, 21)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return text.encode()

    monkeypatch.setattr(rs_rating, "urlopen", lambda *a, **k: _Resp())
    table = rs_rating.fetch_rs_table(tmp_path, "2026_05_22", wait_for_fresh=False)
    assert set(table) == {"UP", "FLAT", "DOWN"} and table["UP"] > table["DOWN"]


def test_the_download_asks_for_18_months_with_no_end_date(monkeypatch, tmp_path):
    """yf.download's `end` is exclusive (upstream's end=today lost the day's
    close): a relative period has no end to get wrong."""
    seen: dict = {}
    klines = {"UP": _trend(100, 0.002), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)

    def fake(tickers, **k):
        seen.update(k)
        return dict(klines)

    monkeypatch.setattr(cloud, "fetch_us_klines_yf", fake)
    assert cloud.main() == 0
    assert seen["period"] == "18mo" and "end" not in seen and "start" not in seen


def test_3m_table_is_still_scored_on_six_months(monkeypatch, tmp_path):
    """The longer download must not change the 3M / RS-line inputs."""
    spans: list = []
    real = cloud.compute_us_rs_3m_table

    def spy_on(klines, spy, weights_bars=None):
        spans.append(max(len(k) for k in klines.values()))
        return real(klines, spy, weights_bars) if weights_bars else real(klines, spy)

    klines = {"UP": _trend(100, 0.002), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)
    monkeypatch.setattr(cloud, "compute_us_rs_3m_table", spy_on)
    assert cloud.main() == 0
    assert spans and all(110 <= n <= 135 for n in spans)


def test_spy_behind_the_expected_session_publishes_nothing(monkeypatch, tmp_path, session):
    """Yahoo has not posted the latest bar: wait, retry, then give up rather
    than publish yesterday's numbers under today's name."""
    klines = {"UP": _trend(100, 0.002), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)
    session(date(2026, 5, 22))  # klines end 05-21
    sleeps: list = []
    spy_calls: list = []
    monkeypatch.setattr(cloud.time, "sleep", sleeps.append)
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: spy_calls.append(1) or _trend(400.0, 0.0))
    universe_fetches: list = []
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", lambda t, **k: universe_fetches.append(t) or {})

    assert cloud.main() == 1
    assert len(spy_calls) == cloud._SPY_STALE_RETRIES + 1
    assert sleeps == [cloud._SPY_STALE_WAIT_SEC] * cloud._SPY_STALE_RETRIES
    assert universe_fetches == []
    assert not (tmp_path / "12m").exists() and not (tmp_path / "3m").exists()


def test_spy_catching_up_on_a_retry_proceeds(monkeypatch, tmp_path, session):
    klines = {"UP": _trend(100, 0.002), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)
    answers = [_trend(400.0, 0.0, end="2026-05-20"), _trend(400.0, 0.0)]
    monkeypatch.setattr(cloud, "_fetch_spy_kline", lambda **k: answers.pop(0))
    assert cloud.main() == 0


def test_partial_bar_of_an_open_session_is_trimmed(monkeypatch, tmp_path, session):
    """Manual dispatch during US hours: bars after the expected session are
    dropped from SPY and the universe alike."""
    klines = {"UP": _trend(100, 0.002, end="2026-05-22"), "DOWN": _trend(100, -0.002, end="2026-05-22")}
    _patch_12m(monkeypatch, tmp_path, klines, spy=_trend(400.0, 0.0, end="2026-05-22"))
    session(date(2026, 5, 21))
    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "12m" / "2026-05-22.csv")
    assert set(out["Session"]) == {"2026-05-21"}


def test_half_updated_universe_publishes_nothing(monkeypatch, tmp_path):
    """Upstream 2026-09-18: about half the universe on the new close, half on
    the old one. One re-fetch, and if it does not help, no table."""
    klines = {f"N{i}": _trend(100, 0.001 * (i + 1)) for i in range(5)}
    klines |= {f"O{i}": _trend(100, 0.001 * (i + 1), end="2026-05-20") for i in range(5)}
    _patch_12m(monkeypatch, tmp_path, klines)
    fetches: list = []

    def fake(tickers, **k):
        fetches.append(sorted(tickers))
        return {t: klines[t] for t in tickers}

    monkeypatch.setattr(cloud, "fetch_us_klines_yf", fake)

    assert cloud.main() == 1
    assert fetches[1] == [f"O{i}" for i in range(5)]  # only the stale ones, once
    assert len(fetches) == 2
    assert not (tmp_path / "12m").exists() and not (tmp_path / "3m").exists()


def test_stale_tickers_recovered_by_the_refetch_are_published(monkeypatch, tmp_path):
    fresh = {f"N{i}": _trend(100, 0.001 * (i + 1)) for i in range(4)}
    first = dict(fresh) | {"LATE": _trend(100, 0.003, end="2026-05-20")}
    second = {"LATE": _trend(100, 0.003)}
    _patch_12m(monkeypatch, tmp_path, first)
    answers = [first, second]
    monkeypatch.setattr(cloud, "fetch_us_klines_yf", lambda t, **k: answers.pop(0))

    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "12m" / "2026-05-22.csv", index_col="Ticker")
    assert "LATE" in out.index


def test_a_few_halted_names_do_not_block_publication(monkeypatch, tmp_path):
    klines = {f"N{i:02d}": _trend(100, 0.0005 * (i + 1)) for i in range(19)}
    klines["HALT"] = _trend(100, 0.001, end="2026-05-20")  # 1/20 = 5% <= 10%
    _patch_12m(monkeypatch, tmp_path, klines)
    assert cloud.main() == 0
    out = pd.read_csv(tmp_path / "12m" / "2026-05-22.csv", index_col="Ticker")
    assert "HALT" in out.index  # one session behind: carried forward, still ranked


def test_universe_falls_back_to_the_last_published_table(monkeypatch, tmp_path):
    klines = {"UP": _trend(100, 0.002), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)
    monkeypatch.setattr(cloud, "fetch_universe", lambda *a, **k: None)
    (tmp_path / "12m").mkdir()
    (tmp_path / "12m" / "2026-05-21.csv").write_text(
        "Ticker,Relative Strength,Percentile,Session\nUP,120,99,2026-05-20\nDOWN,80,0,2026-05-20\n"
    )
    asked: list = []
    monkeypatch.setattr(
        cloud, "fetch_us_klines_yf", lambda t, **k: asked.append(list(t)) or dict(klines)
    )
    assert cloud.main() == 0
    assert asked[0] == ["DOWN", "UP"]


def test_12m_files_are_pruned_with_the_same_retention(monkeypatch, tmp_path):
    klines = {"UP": _trend(100, 0.002), "DOWN": _trend(100, -0.002)}
    _patch_12m(monkeypatch, tmp_path, klines)
    (tmp_path / "12m").mkdir()
    (tmp_path / "12m" / "2026-05-07.csv").write_text("Ticker,Percentile\n")  # 15 days
    (tmp_path / "12m" / "README.md").write_text("docs")
    assert cloud.main() == 0
    assert not (tmp_path / "12m" / "2026-05-07.csv").exists()
    assert (tmp_path / "12m" / "README.md").exists()
