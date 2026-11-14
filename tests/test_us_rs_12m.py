"""us_rs_12m: universe filter, trading calendar and the 12M RS formula.

The formula is Fred6725/relative-strength's `rs_ranking.py` (the source this
table replaced on 2026-11-14); `_upstream_rs` below is that code verbatim,
so the tests pin "same numbers as upstream when the data is clean" and the
one deliberate difference: windows are aligned by date, not tail position.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

import us_rs_12m as m


# --- upstream reference (rs_ranking.py) --------------------------------------


def _quarters_perf(closes: pd.Series, n: int) -> float:
    length = min(len(closes), n * int(252 / 4))
    prices = closes.tail(length)
    pct_chg = prices.pct_change().dropna()
    perf_cum = (pct_chg + 1).cumprod() - 1
    return perf_cum.tail(1).item()


def _upstream_strength(closes: pd.Series) -> float:
    q = [_quarters_perf(closes, n) for n in (1, 2, 3, 4)]
    return 0.4 * q[0] + 0.2 * q[1] + 0.2 * q[2] + 0.2 * q[3]


def _upstream_rs(closes: pd.Series, ref: pd.Series) -> float:
    rs = (1 + _upstream_strength(closes)) / (1 + _upstream_strength(ref)) * 100
    return int(rs * 100) / 100


# --- fixtures ----------------------------------------------------------------

_IDX = pd.bdate_range(end="2026-11-13", periods=380)


def _kline(closes, idx=_IDX) -> pd.DataFrame:
    closes = list(closes)
    return pd.DataFrame({"time_key": idx[-len(closes):], "close": closes})


def _walk(seed: int, n: int = 380, drift: float = 0.0005) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(100 * np.cumprod(1 + rng.normal(drift, 0.02, n)))


_SPY = _kline(_walk(0, drift=0.0004))


# --- formula -----------------------------------------------------------------


def test_matches_upstream_on_clean_full_history() -> None:
    klines = {f"T{i}": _kline(_walk(i + 1)) for i in range(30)}
    table = m.compute_us_rs_12m_table(klines, _SPY)
    assert len(table) == 30
    for t, k in klines.items():
        assert table.loc[t, m.COL_RS] == pytest.approx(
            _upstream_rs(k["close"], _SPY["close"]), abs=0.011
        ), t


def test_matches_upstream_for_a_name_listed_mid_window() -> None:
    """150 bars: the 189- and 252-session legs start at its first bar, SPY
    keeps its full windows — upstream's min(len, n*63)."""
    k = _kline(_walk(7, n=150))
    table = m.compute_us_rs_12m_table({"NEW": k, "OLD": _kline(_walk(8))}, _SPY)
    assert table.loc["NEW", m.COL_RS] == pytest.approx(
        _upstream_rs(k["close"], _SPY["close"]), abs=0.011
    )


def test_most_recent_quarter_weighs_double() -> None:
    flat = [100.0] * 380
    early = flat.copy()
    early[-200:] = [110.0] * 200       # +10% inside the 252 leg only
    late = flat.copy()
    late[-30:] = [110.0] * 30          # +10% inside every leg
    spy = _kline(flat)
    table = m.compute_us_rs_12m_table({"EARLY": _kline(early), "LATE": _kline(late)}, spy)
    assert table.loc["EARLY", m.COL_RS] == pytest.approx(102.0, abs=0.011)  # 0.2 * 10%
    assert table.loc["LATE", m.COL_RS] == pytest.approx(110.0, abs=0.011)   # 1.0 * 10%


def test_windows_are_aligned_by_date_not_by_tail_position() -> None:
    """A stock missing SPY's latest bar. By tail position (upstream) its
    63-bar window is one session earlier than SPY's; by date it is the same
    window with the last close carried forward."""
    closes = _walk(3)
    full = _kline(closes)
    missing_last = _kline(closes[:-1], idx=_IDX[:-1])
    table = m.compute_us_rs_12m_table({"X": missing_last, "Y": full}, _SPY)

    carried = pd.Series(closes[:-1] + [closes[-2]])
    assert table.loc["X", m.COL_RS] == pytest.approx(
        _upstream_rs(carried, _SPY["close"]), abs=0.011
    )
    tail_aligned = _upstream_rs(pd.Series(closes[:-1]), _SPY["close"])
    assert table.loc["X", m.COL_RS] != pytest.approx(tail_aligned, abs=0.011)


def test_interior_gap_carries_the_previous_close() -> None:
    closes = _walk(4)
    k = _kline(closes).drop(index=[300, 301]).reset_index(drop=True)
    table = m.compute_us_rs_12m_table({"GAP": k, "REF": _kline(closes)}, _SPY)
    assert table.loc["GAP", m.COL_RS] == table.loc["REF", m.COL_RS]


def test_bars_after_spys_last_date_are_ignored() -> None:
    closes = _walk(5)
    ahead_idx = _IDX.append(pd.DatetimeIndex(["2026-11-16"]))
    ahead = pd.DataFrame({"time_key": ahead_idx, "close": closes + [closes[-1] * 3]})
    table = m.compute_us_rs_12m_table({"A": ahead, "B": _kline(closes)}, _SPY)
    assert table.loc["A", m.COL_RS] == table.loc["B", m.COL_RS]
    assert set(table[m.COL_SESSION]) == {"2026-11-13"}


# --- exclusions --------------------------------------------------------------


def test_fewer_than_120_bars_is_excluded() -> None:
    table = m.compute_us_rs_12m_table(
        {"SHORT": _kline(_walk(1, n=119)), "OK": _kline(_walk(2, n=120)), "LONG": _kline(_walk(3))},
        _SPY,
    )
    assert "SHORT" not in table.index and "OK" in table.index


def test_bars_are_counted_as_real_bars_not_calendar_span() -> None:
    """130 calendar sessions since listing but only 110 traded."""
    closes = _walk(1, n=130)
    k = _kline(closes).drop(index=range(40, 60)).reset_index(drop=True)
    table = m.compute_us_rs_12m_table({"THIN": k, "OK": _kline(_walk(2))}, _SPY)
    assert "THIN" not in table.index


def test_rs_at_or_above_590_is_excluded() -> None:
    rocket = [1.0] * 379 + [7.0]
    table = m.compute_us_rs_12m_table(
        {"ROCKET": _kline(rocket), "A": _kline(_walk(1)), "B": _kline(_walk(2))}, _kline([100.0] * 380)
    )
    assert "ROCKET" not in table.index and len(table) == 2


def test_name_that_stopped_trading_is_excluded() -> None:
    closes = _walk(1)
    gone = _kline(closes[:-10], idx=_IDX[:-10])            # 10 sessions behind
    halted = _kline(closes[:-3], idx=_IDX[:-3])            # 3 behind: still ranked
    table = m.compute_us_rs_12m_table(
        {"GONE": gone, "HALT": halted, "OK": _kline(closes)}, _SPY
    )
    assert "GONE" not in table.index
    assert {"HALT", "OK"} <= set(table.index)


def test_non_positive_and_empty_series_are_excluded_without_raising() -> None:
    zero = [0.0] * 200 + _walk(1, n=180)
    table = m.compute_us_rs_12m_table(
        {"ZERO": _kline(zero), "EMPTY": pd.DataFrame({"time_key": [], "close": []}),
         "NONE": None, "OK": _kline(_walk(2)), "OK2": _kline(_walk(3))},
        _SPY,
    )
    assert set(table.index) == {"OK", "OK2"}


def test_unusable_spy_gives_an_empty_table() -> None:
    assert m.compute_us_rs_12m_table({"A": _kline(_walk(1))}, None).empty
    assert m.compute_us_rs_12m_table({"A": _kline(_walk(1))}, _SPY.iloc[:0]).empty


# --- percentile --------------------------------------------------------------


def test_percentile_is_qcut_100_like_upstream() -> None:
    klines = {f"T{i:03d}": _kline(_walk(i + 1)) for i in range(250)}
    table = m.compute_us_rs_12m_table(klines, _SPY)
    expected = pd.qcut(table[m.COL_RS], 100, labels=False, duplicates="drop")
    assert (table[m.COL_PERCENTILE] == expected).all()
    assert table[m.COL_PERCENTILE].min() == 0 and table[m.COL_PERCENTILE].max() == 99
    assert table[m.COL_RS].is_monotonic_decreasing
    assert table[m.COL_PERCENTILE].dtype.kind == "i"


def test_tz_aware_time_key_is_accepted() -> None:
    k = _kline(_walk(1))
    aware = k.assign(time_key=k["time_key"].dt.tz_localize("America/New_York"))
    table = m.compute_us_rs_12m_table({"TZ": aware, "NAIVE": k}, _SPY)
    assert table.loc["TZ", m.COL_RS] == table.loc["NAIVE", m.COL_RS]


# --- universe ----------------------------------------------------------------

_HEADER = (
    "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
    "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares"
)


def _row(symbol: str, name: str, etf: str = "N", test: str = "N") -> str:
    return f"Y|{symbol}|{name}|N| |{etf}|100|{test}||{symbol}|{symbol}|N"


@pytest.mark.parametrize("name", [
    "Agilent Technologies, Inc. Common Stock",
    "Energy Transfer LP Common Units",
    "MPLX LP Common Units Representing Limited Partner Interests",
    "Brookfield Renewable Partners L.P. Limited Partnership Units",
    "VOC Energy Trust Units of Beneficial Interest",
    "WeRide Inc. - American Depositary Shares",
    "Grupo Aval Acciones y Valores S.A. ADR (Each representing 20 preferred shares)",
    "Grupo Cibest S.A. American Depositary Shares each representing four Preferred Shares",
    "Cohen & Steers Select Preferred and Income Fund, Inc. Common Stock",
    "Blackrock Core Bond Trust",
    "Rightmove Holdings Inc. Common Stock",
    "United Rentals, Inc. Common Stock",
    "",
])
def test_common_equity_is_kept(name: str) -> None:
    assert m.is_common_equity(name)


@pytest.mark.parametrize("name", [
    "Lake Superior Acquisition Corp. - Units",
    "Sizzle Acquisition Corp. II - Unit",
    "Southern Company (The) 2025 Series A Corporate Units",
    "XYZ Acquisition Corp. Units, each consisting of one Class A share and one-half of one warrant",
    "BuzzFeed, Inc. - Warrant",
    "Silvia, Inc. - Warrant.",
    "East West Ave Acquisition Corp. - Rights",
    "Harvard Ave Acquisition Corporation - Rights that convert on a 1/10th of 1 basis",
    "Bank OZK - 4.625% Series A Non-Cumulative Perpetual Preferred Stock",
    "CHS Inc - Class B Cumulative Redeemable Preferred Stock, Series 4",
    "Air T, Inc. - Trust Preferred Securities",
    "Alphabet Inc. - Depositary Shares representing a 1/20th Interest in a Share of Series B Mandatory Convertible Stock",
    "Qwest Corporation - 6.500% Notes due 2051",
    "Entergy Arkansas, LLC First Mortgage Bonds, 4.875% Series Due September 1, 2066",
    "iPath Series B S&P 500 VIX Short-Term Futures ETN",
    "iPath Series B Carbon Exchange-Traded Notes",
    "Goldman Sachs Group Securities STRATS Trust for Goldman Sachs Group Securities, Series 2006-2",
    "Liberty Latin America Ltd. - 9.0% Fixed Rate Cumulative Perpetual Redeemable Series A Preference Shares",
])
def test_non_equity_is_dropped(name: str) -> None:
    assert not m.is_common_equity(name)


def test_parse_universe_applies_the_upstream_symbol_rules_and_the_name_filter() -> None:
    text = "\n".join([
        _HEADER,
        _row("AAPL", "Apple Inc. - Common Stock"),
        _row("SPY", "SPDR S&P 500 ETF Trust", etf="Y"),
        _row("ZTEST", "Test Issue Common Stock", test="Y"),
        _row("BRK.B", "Berkshire Hathaway Inc. Class B"),
        _row("BAC$A", "Bank of America Preferred"),
        _row("ABCDU", "ABCD Acquisition Corp. - Units"),
        _row("A", "Agilent Technologies, Inc. Common Stock"),
        "File Creation Time: 1114202608:00|||||||||||",
    ])
    assert m.parse_universe(text) == ["A", "AAPL"]


class _Resp:
    def __init__(self, body: str):
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


def _big_list(n: int) -> str:
    syms = [f"{chr(65 + i // 676)}{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}" for i in range(n)]
    return "\n".join([_HEADER] + [_row(s, "Some Company Inc. Common Stock") for s in syms])


def test_fetch_universe_uses_the_mirror_when_the_primary_fails(monkeypatch) -> None:
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    asked: list[str] = []

    def _urlopen(req, *a, **k):
        asked.append(req.full_url)
        if "ftp." not in req.full_url:
            raise OSError("primary down")
        return _Resp(_big_list(3500))

    monkeypatch.setattr(m, "urlopen", _urlopen)
    universe = m.fetch_universe()
    assert universe is not None and len(universe) == 3500
    assert len(asked) == 2


def test_fetch_universe_rejects_a_truncated_list_and_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)
    monkeypatch.setattr(m, "urlopen", lambda *a, **k: _Resp(_big_list(50)))
    assert m.fetch_universe() is None
    monkeypatch.setattr(m, "urlopen", lambda *a, **k: _Resp("<html>error</html>"))
    assert m.fetch_universe() is None


# --- trading calendar --------------------------------------------------------


@pytest.mark.parametrize("now, expected", [
    ("2026-11-13 20:45", "2026-11-13"),   # the job's slot: Fri evening ET
    ("2026-11-13 10:00", "2026-11-12"),   # session still open
    ("2026-11-14 12:00", "2026-11-13"),   # Saturday
    ("2026-11-16 08:00", "2026-11-13"),   # Monday pre-open
    ("2026-11-26 20:00", "2026-11-25"),   # Thanksgiving
    ("2026-04-03 20:00", "2026-04-02"),   # Good Friday
    ("2026-07-03 20:00", "2026-07-02"),   # July 4 on a Saturday → Friday closed
    ("2026-06-19 20:00", "2026-06-18"),   # Juneteenth
    ("2027-01-01 20:00", "2026-12-31"),   # New Year's Day
    ("2021-12-31 20:00", "2021-12-31"),   # Jan 1 2022 on a Saturday: NOT observed Friday
    ("2023-01-02 20:00", "2022-12-30"),   # Jan 1 2023 on a Sunday → Monday closed
])
def test_expected_session(now: str, expected: str) -> None:
    now_et = datetime.fromisoformat(now).replace(tzinfo=m._ET)
    assert m.expected_session(now_et) == date.fromisoformat(expected)
