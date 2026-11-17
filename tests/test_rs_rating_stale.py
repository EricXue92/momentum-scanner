"""12M RS table staleness: detect a table one session behind, poll for the
fresh one, and never cache a stale table as today's.

The published table carries the `Session` its closes belong to. us-eod can
run before today's file is published (cloud job late or failed), in which
case the walk-back serves the previous file — CART 2026-10-27 was gated on
10-23 data at 90 while the 10-26 table said 94 (then with rs-log as source).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import rs_rating

_ET = ZoneInfo("America/New_York")

# Latest US session whose close has passed (what the NYSE calendar says).
_LATEST = date(2026, 10, 26)
_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL")


def _csv(session: str) -> str:
    """Published-table slice for `session`."""
    lines = ["Ticker,Relative Strength,Percentile,Session"]
    for i, t in enumerate(_TICKERS):
        lines.append(f"{t},{150 - i},{90 + i},{session}")
    pct = 94 if session == "2026-10-26" else 90
    lines.append(f"CART,120.5,{pct},{session}")
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


def test_data_session_is_the_session_column() -> None:
    assert rs_rating.table_data_session(_STALE) == date(2026, 10, 23)
    assert rs_rating.table_data_session(_FRESH) == date(2026, 10, 26)


def test_data_session_none_without_session_column() -> None:
    """A cached rs-log file from before the switch has no Session."""
    assert rs_rating.table_data_session("Ticker,Percentile\nAAPL,95\n") is None


def test_data_session_none_when_unparseable() -> None:
    assert rs_rating.table_data_session("Ticker,Percentile,Session\nAAPL,95,soon\n") is None


# --- fetch_rs_table polling -----------------------------------------------------


def test_stale_table_is_polled_until_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
    calls = _serve(monkeypatch, [_STALE, _STALE, _FRESH])

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27")

    assert table is not None and table["CART"] == 94
    assert calls[0] == 3
    assert clock.sleeps == [rs_rating._STALE_POLL_SEC] * 2
    cache = tmp_path / "state" / "rs_rating_2026_10_27.csv"
    assert cache.read_text(encoding="utf-8") == _FRESH


def test_wait_for_fresh_false_skips_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
    calls = _serve(monkeypatch, [_STALE, _FRESH])

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27", wait_for_fresh=False)

    assert table is not None and table["CART"] == 90
    assert calls[0] == 1
    assert clock.sleeps == []


def test_stale_table_used_after_deadline_but_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
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
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
    calls = _serve(monkeypatch, [_FRESH])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27")["CART"] == 94
    assert calls[0] == 1 and clock.sleeps == []
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_table_without_session_skips_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """Nothing to compare (a cached rs-log file) → use + cache, no poll."""
    asked = []
    monkeypatch.setattr(
        rs_rating, "_expected_session", lambda: asked.append(1) or _LATEST
    )
    _serve(monkeypatch, ["Ticker,Percentile\nAAPL,95\n"])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27") == {"AAPL": 95}
    assert asked == []
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_calendar_raising_skips_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The check must never abort the RS gate: use + cache, as before it existed."""
    def _boom():
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr(rs_rating, "_expected_session", _boom)
    calls = _serve(monkeypatch, [_STALE])

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27")["CART"] == 90
    assert calls[0] == 1 and clock.sleeps == []
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_refetch_failure_while_polling_keeps_the_stale_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    from urllib.error import URLError

    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
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


# --- the reference is the trading calendar, not Yahoo -------------------------


def test_yahoo_nan_closes_cannot_hide_a_stale_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """2026-11-17: Yahoo served Monday's (11-16) daily row with NaN Close for
    hours. The cloud job published nothing, and the mega-cap closes this
    check used as its reference said the latest session was Friday — so
    Friday's table was "current" and us-eod gated on it with no warning.
    The reference is the NYSE calendar, which Yahoo cannot fool."""
    import main
    import us_rs_12m

    idx = pd.to_datetime(["2026-11-12", "2026-11-13", "2026-11-16"])
    yahoo = pd.DataFrame(
        {("Close", "AAPL"): [354.88, 356.27, float("nan")],
         ("Volume", "AAPL"): [4.1e7, 4.0e7, 4.5e7]},
        index=idx,
    )
    yahoo_calls: list[tuple] = []
    monkeypatch.setattr(
        main, "_yf_download_with_retry", lambda *a, **k: yahoo_calls.append(a) or yahoo
    )
    monday_evening = datetime(2026, 11, 16, 21, 0, tzinfo=_ET)
    monkeypatch.setattr(
        rs_rating, "_expected_session",
        lambda: us_rs_12m.expected_session(monday_evening),
    )
    calls = _serve(monkeypatch, [_csv("2026-11-13"), _csv("2026-11-16")])

    rs_rating.fetch_rs_table(tmp_path, "2026_11_17")

    assert clock.sleeps == [rs_rating._STALE_POLL_SEC]   # Friday's table is stale
    assert calls[0] == 2
    cache = tmp_path / "state" / "rs_rating_2026_11_17.csv"
    assert cache.read_text(encoding="utf-8") == _csv("2026-11-16")
    assert yahoo_calls == []


# --- dated files: walk back when today's is not published -------------------


def _serve_by_date(monkeypatch: pytest.MonkeyPatch, published: dict[str, str]) -> list[str]:
    """urlopen serves `published[<YYYY-MM-DD>]`, 404 for any other date.
    Returns the list of requested dates."""
    from urllib.error import HTTPError

    asked: list[str] = []

    def _urlopen(req, *_a, **_k):
        day = req.full_url.rsplit("/", 1)[1].removesuffix(".csv")
        asked.append(day)
        if day not in published:
            raise HTTPError(req.full_url, 404, "Not Found", None, None)
        return _Resp(published[day])

    monkeypatch.setattr(rs_rating, "urlopen", _urlopen)
    return asked


def test_url_is_this_repos_dated_12m_file() -> None:
    url = rs_rating.RS_CSV_URL_TEMPLATE.format(date="2026-10-27")
    assert url.endswith("EricXue92/momentum-scanner/main/data/us_rs_12m/2026-10-27.csv")


def test_missing_todays_file_walks_back_without_retry_sleeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A holiday: nothing published today, yesterday's file IS the latest
    session → used and cached, no poll, and a 404 is not retried."""
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
    asked = _serve_by_date(monkeypatch, {"2026-10-27": _FRESH})

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_28")

    assert table is not None and table["CART"] == 94
    assert asked == ["2026-10-28", "2026-10-27"]
    assert clock.sleeps == []
    assert (tmp_path / "state" / "rs_rating_2026_10_28.csv").exists()


def test_late_publication_is_picked_up_by_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
    published = {"2026-10-26": _STALE}
    _serve_by_date(monkeypatch, published)
    real_sleep = clock.sleep

    def _sleep_then_publish(s: float) -> None:
        real_sleep(s)
        published["2026-10-27"] = _FRESH

    monkeypatch.setattr(rs_rating.time, "sleep", _sleep_then_publish)

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27")

    assert table is not None and table["CART"] == 94
    assert clock.sleeps == [rs_rating._STALE_POLL_SEC]


def test_table_published_100_minutes_late_is_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """2026-11-17: Yahoo got its closes back ~95 min after the 08:45
    dispatch, minutes short of the old 60-min wait from 10:00. The wait is
    120 min so a day like that still gates on the fresh table."""
    monkeypatch.setattr(rs_rating, "_expected_session", lambda: _LATEST)
    published = {"2026-10-26": _STALE}
    _serve_by_date(monkeypatch, published)
    real_sleep = clock.sleep

    def _sleep_then_maybe_publish(s: float) -> None:
        real_sleep(s)
        if clock.now >= 100 * 60:
            published["2026-10-27"] = _FRESH

    monkeypatch.setattr(rs_rating.time, "sleep", _sleep_then_maybe_publish)

    table = rs_rating.fetch_rs_table(tmp_path, "2026_10_27")

    assert table is not None and table["CART"] == 94
    assert clock.now == 100 * 60
    assert (tmp_path / "state" / "rs_rating_2026_10_27.csv").exists()


def test_us_eod_watchdog_outlasts_the_freshness_wait() -> None:
    """run_eod.sh's EOD_TIMEOUT must exceed the wait plus a full run (30 min
    of margin): a watchdog SIGTERM mid-poll leaves a day with no output and
    no trace in the python log (2026-10-28). Change the two together."""
    import re

    script = (Path(__file__).resolve().parents[1] / "scripts" / "run_eod.sh").read_text()
    m = re.search(r"^EOD_TIMEOUT=\$\{EOD_TIMEOUT:-(\d+)\}", script, re.M)
    assert m, "EOD_TIMEOUT default not found in run_eod.sh"
    assert int(m.group(1)) >= rs_rating._STALE_MAX_WAIT_SEC + 1800


def test_nothing_published_in_window_falls_back_to_local_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "rs_rating_2026_10_26.csv").write_text("Ticker,Percentile\nAAPL,95\n")
    asked = _serve_by_date(monkeypatch, {})

    assert rs_rating.fetch_rs_table(tmp_path, "2026_10_27") == {"AAPL": 95}
    assert len(asked) == rs_rating._PUBLISHED_MAX_AGE_DAYS + 1
