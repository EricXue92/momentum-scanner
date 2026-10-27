"""12M RS table staleness: detect a table one session behind, poll for the
fresh one, and never cache a stale table as today's.

rs-log publishes `rs_stocks.csv` between ~01:30Z and ~04:00Z; us-eod fetches
at ~02:00Z, so in 2026-10 it got the previous session's table on 14/19 days
(CART 2026-10-27: gated on 10-23 data at 90 while the 10-26 table said 94).
The CSV has no date column; its `Price` column is the session close, which
is matched against yfinance closes of a few mega caps.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import rs_rating

_ET = ZoneInfo("America/New_York")

# Raw closes of the reference tickers (yfinance auto_adjust=False).
_CLOSES = pd.DataFrame(
    {
        "AAPL": [348.22, 347.73, 343.91],
        "MSFT": [521.08, 524.51, 532.60],
        "NVDA": [212.21, 210.96, 216.00],
        "AMZN": [250.38, 248.00, 249.09],
        "GOOGL": [324.22, 320.21, 316.80],
    },
    index=pd.to_datetime(["2026-10-22", "2026-10-23", "2026-10-26"]),
)


def _csv(session: str) -> str:
    """rs_stocks.csv slice whose Price column is `session`'s close."""
    row = _CLOSES.loc[session]
    lines = ["Rank,Ticker,Percentile,Price"]
    for i, t in enumerate(_CLOSES.columns):
        lines.append(f"{i + 1},{t},{90 + i},{row[t]}")
    pct = 94 if session == "2026-10-26" else 90
    lines.append(f"99,CART,{pct},1.0")
    return "\n".join(lines) + "\n"


_STALE = _csv("2026-10-23")
_FRESH = _csv("2026-10-26")


class _Resp:
    def __init__(self, body: str):
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


class _Clock:
    """Fake monotonic clock advanced by the fake sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(rs_rating.time, "sleep", c.sleep)
    monkeypatch.setattr(rs_rating.time, "monotonic", c.monotonic)
    return c


def _serve(monkeypatch: pytest.MonkeyPatch, bodies: list[str]) -> list[int]:
    """urlopen returns `bodies` in order (last one repeats); returns a call
    counter list."""
    calls = [0]

    def _urlopen(*_a, **_k):
        body = bodies[min(calls[0], len(bodies) - 1)]
        calls[0] += 1
        return _Resp(body)

    monkeypatch.setattr(rs_rating, "urlopen", _urlopen)
    return calls


# --- table_data_session -------------------------------------------------------


def test_data_session_is_the_date_whose_closes_match_price() -> None:
    assert rs_rating.table_data_session(_STALE, _CLOSES) == date(2026, 10, 23)
    assert rs_rating.table_data_session(_FRESH, _CLOSES) == date(2026, 10, 26)


def test_data_session_none_without_price_column() -> None:
    assert rs_rating.table_data_session("Ticker,Percentile\nAAPL,95\n", _CLOSES) is None


def test_data_session_none_when_no_close_matches() -> None:
    off = _STALE.replace("347.73", "1.0").replace("524.51", "1.0").replace(
        "210.96", "1.0"
    ).replace("248.0", "1.0").replace("320.21", "1.0")
    assert rs_rating.table_data_session(off, _CLOSES) is None


# --- completed sessions -------------------------------------------------------


def test_partial_bar_of_open_us_session_is_not_a_completed_session() -> None:
    frame = pd.concat(
        [_CLOSES, pd.DataFrame(_CLOSES.iloc[[-1]].values, columns=_CLOSES.columns,
                               index=pd.to_datetime(["2026-10-27"]))]
    )
    during = datetime(2026, 10, 27, 11, 0, tzinfo=_ET)
    after = datetime(2026, 10, 27, 16, 30, tzinfo=_ET)
    assert rs_rating.latest_completed_session(frame, during) == date(2026, 10, 26)
    assert rs_rating.latest_completed_session(frame, after) == date(2026, 10, 27)


# --- fetch_rs_table polling -----------------------------------------------------


def test_stale_table_is_polled_until_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_fetch_reference_closes", lambda: _CLOSES)
    calls = _serve(monkeypatch, [_STALE, _STALE, _FRESH])

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27")

    assert table is not None and table["CART"] == 94
    assert calls[0] == 3
    assert clock.sleeps == [rs_rating._STALE_POLL_SEC] * 2
    cache = tmp_path / "state" / "rs_rating_2026_10_27.csv"
    assert cache.read_text(encoding="utf-8") == _FRESH


def test_stale_table_used_after_deadline_but_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_fetch_reference_closes", lambda: _CLOSES)
    _serve(monkeypatch, [_STALE])

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27")

    # Gate on the stale table rather than none (no hard-fail) ...
    assert table is not None and table["CART"] == 90
    assert clock.now >= rs_rating._STALE_MAX_WAIT_SEC
    assert clock.now < rs_rating._STALE_MAX_WAIT_SEC + rs_rating._STALE_POLL_SEC
    # ... but a same-day rerun must refetch instead of reading it back.
    assert not (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_fresh_table_is_not_polled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_fetch_reference_closes", lambda: _CLOSES)
    calls = _serve(monkeypatch, [_FRESH])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27")["CART"] == 94
    assert calls[0] == 1 and clock.sleeps == []
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_unavailable_reference_closes_skip_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """yfinance down → behave as before the check existed: use + cache."""
    monkeypatch.setattr(rs_rating, "_fetch_reference_closes", lambda: None)
    calls = _serve(monkeypatch, [_STALE])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27")["CART"] == 90
    assert calls[0] == 1 and clock.sleeps == []
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_table_without_reference_prices_skips_yfinance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Nothing to match → don't pay for (or depend on) a yfinance call."""

    fetched = []
    monkeypatch.setattr(
        rs_rating, "_fetch_reference_closes", lambda: fetched.append(1) or _CLOSES
    )
    _serve(monkeypatch, ["Ticker,Percentile\nAAPL,95\n"])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27") == {"AAPL": 95}
    assert fetched == []
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_reference_fetch_raising_skips_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    def _boom():
        raise RuntimeError("yfinance exploded")

    monkeypatch.setattr(rs_rating, "_fetch_reference_closes", _boom)
    _serve(monkeypatch, [_STALE])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27")["CART"] == 90
    assert clock.sleeps == []


def test_refetch_failure_while_polling_keeps_the_stale_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    from urllib.error import URLError

    monkeypatch.setattr(rs_rating, "_fetch_reference_closes", lambda: _CLOSES)
    calls = [0]

    def _urlopen(*_a, **_k):
        calls[0] += 1
        if calls[0] == 1:
            return _Resp(_STALE)
        raise URLError("offline")

    monkeypatch.setattr(rs_rating, "urlopen", _urlopen)

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27")
    assert table is not None and table["CART"] == 90
    assert not (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()
