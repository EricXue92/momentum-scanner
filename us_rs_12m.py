"""IBD-style 12-month Relative Strength for the US universe, computed cloud-side.

Replaces the Fred6725/rs-log table (→ decisions 2026-11-14). Same formula as
upstream `rs_ranking.py`:

    strength = 0.4·perf(63) + 0.2·perf(126) + 0.2·perf(189) + 0.2·perf(252)
    RS       = (1 + strength_stock) / (1 + strength_SPY) × 100   (2 dp, truncated)
    percentile = pd.qcut(RS, 100, labels=False, duplicates="drop")

`perf(n)` is the return over the last n sessions (first → last close of the
window, like upstream's `quarters_perf`). Dropped: < 120 bars, RS >= 590.

Deliberate differences from upstream:
  * **Date-aligned.** Every stock is reindexed onto SPY's trading calendar, so
    a window covers the same sessions for the stock and the benchmark.
    Upstream aligned by tail position: a stock missing its latest bar was
    scored on a window shifted one day back.
  * **Universe filter by security name**, not by Yahoo sector/industry
    (upstream needs one `.info` request per ticker and silently caches
    all-`n/a` rows when throttled). See `is_common_equity`.
  * **Latest-bar validation** lives in the cloud script: nothing is published
    unless SPY's last bar is `expected_session()` and the universe has it too.

This module only computes; the cloud entrypoint is
`scripts/compute_us_rs_3m_cloud.py`, the local reader is `rs_rating.py`.
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)

logger = logging.getLogger(__name__)

# Upstream: n * int(252 / 4) bars per quarter, most recent quarter double weight.
BARS_PER_QUARTER = 63
WEIGHTS_12M: tuple[tuple[int, float], ...] = ((1, 0.4), (2, 0.2), (3, 0.2), (4, 0.2))
MIN_BARS = 120
MAX_RS = 590.0
# A name whose last real bar is this many sessions behind SPY's is not
# trading (halted for good / delisted but still on the list): not ranked.
MAX_STALE_SESSIONS = 5

# Published column names match the rs-log CSV, so `rs_rating.fetch_rs_table`,
# the report's RS lookup and the ≤ 3-day-old cached rs-log files all parse
# the same way. `Session` is new: the trading day the closes belong to.
COL_TICKER = "Ticker"
COL_RS = "Relative Strength"
COL_PERCENTILE = "Percentile"
COL_SESSION = "Session"

_NASDAQ_TRADED_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt",
    "https://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqtraded.txt",
)
_UNIVERSE_RETRY_DELAY_SEC = 10.0
# The list is ~12-13K lines; a body this short is an error page.
_MIN_UNIVERSE_SIZE = 3000

_ET = ZoneInfo("America/New_York")
_US_CLOSE_HOUR = 16


# --- universe ----------------------------------------------------------------

# Non-common-equity issues, recognised from nasdaqtrader's "Security Name".
# Checked against the 2026-11-14 list (6929 alpha-only non-ETF symbols).
_NOT_EQUITY_RE = re.compile(
    r"""
      \bWarrants?\b                                   # warrants, and units "consisting of ... warrant"
    | (?<!Common\ )(?<!Partnership\ )\bUnits?\.?\s*$  # SPAC / corporate units — not MLP "Common Units"
    | \bRights?\.?\s*$ | \ -\ Rights?\b                # SPAC rights
    | %                                               # coupon: notes, bonds, preferreds
    | \bdue\b                                         # dated debt, ETNs
    | \bETNs?\b | Exchange[-\ ]Traded\ Notes?
    | \b(?:Senior|Subordinated)\ (?:Notes|Debentures)\b
    | STRATS | CorTS | PPlus | Corp(?:orate)?\ Backed | Trust\ Certificates | Repackaged
    | Depositary\ Shares?\b.*\b1/\d                   # fractional interests in a preferred
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PREFERRED_RE = re.compile(r"\bPreferred\b|\bPreference\b|\bPfd\b", re.IGNORECASE)
# "Preferred" in the name of a real equity: a closed-end fund that invests in
# preferreds, or an ADR whose underlying is a preferred share (AVAL, CIB).
_PREFERRED_OK_RE = re.compile(r"\bFund\b|American\ Depositary|\bADRs?\b", re.IGNORECASE)


def is_common_equity(security_name: str) -> bool:
    """False for units, warrants, rights, preferreds, notes/bonds, ETNs and
    structured trust certificates. Unknown / empty names are kept."""
    name = security_name or ""
    if _NOT_EQUITY_RE.search(name):
        return False
    if _PREFERRED_RE.search(name) and not _PREFERRED_OK_RE.search(name):
        return False
    return True


def parse_universe(text: str) -> list[str]:
    """Tickers from a `nasdaqtraded.txt` body: non-ETF, non-test, alpha-only
    symbols (upstream's rule — class shares like BRK.B stay out and are
    kept-as-missing by the gate) that `is_common_equity`. Sorted."""
    # na_filter off: the tickers "NA" and "NAN" are symbols, not missing values.
    df = pd.read_csv(io.StringIO(text), sep="|", dtype=str, na_filter=False)
    df = df[
        (df["ETF"] == "N")
        & (df["Test Issue"] == "N")
        & df["Symbol"].str.fullmatch(r"[A-Z]+")
    ]
    names = df["Security Name"]
    return sorted(df.loc[names.map(is_common_equity), "Symbol"].unique())


def fetch_universe(timeout: float = 30.0) -> list[str] | None:
    """Download and parse the nasdaqtrader list (primary URL, then the FTP
    mirror, each tried twice). None on failure — never raises."""
    for attempt in range(2):
        for url in _NASDAQ_TRADED_URLS:
            try:
                req = Request(url, headers={"User-Agent": "momentum-scanner/1.0"})
                with urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                universe = parse_universe(text)
            except Exception as e:  # network, decode, parser, missing column
                logger.warning(f"[US RS 12M] universe fetch failed ({url}): {type(e).__name__}: {e}")
                continue
            if len(universe) >= _MIN_UNIVERSE_SIZE:
                return universe
            logger.warning(
                f"[US RS 12M] universe from {url} has only {len(universe)} tickers; ignoring"
            )
        if attempt == 0:
            time.sleep(_UNIVERSE_RETRY_DELAY_SEC)
    return None


# --- trading calendar --------------------------------------------------------


def _new_year_observance(dt: datetime) -> datetime:
    """NYSE: New Year's Day on a Sunday is observed Monday; on a Saturday it
    is NOT observed (the preceding Friday is the prior year's last session)."""
    return sunday_to_monday(dt)


class _NYSEHolidays(AbstractHolidayCalendar):
    """Regular NYSE full-day closures. Unscheduled closures (a day of
    mourning) are not here: on such a day the cloud job fails its SPY check
    and publishes nothing, and the local run walks back to the previous
    file — which is the latest session anyway."""

    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=_new_year_observance),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01",
                observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    ts = pd.Timestamp(d)
    return ts not in _NYSEHolidays().holidays(start=ts, end=ts)


def expected_session(now_et: datetime | None = None) -> date:
    """The latest US session whose close has passed: today (ET) from 16:00
    on a trading day, otherwise the previous trading day."""
    now_et = now_et or datetime.now(_ET)
    d = now_et.date()
    if now_et.hour < _US_CLOSE_HOUR:
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


# --- compute -----------------------------------------------------------------


def _close_series(kline: pd.DataFrame | None) -> pd.Series | None:
    """`time_key`/`close` frame → float Series on a normalised, tz-naive,
    unique date index. None when unusable."""
    if kline is None or getattr(kline, "empty", True):
        return None
    if "time_key" not in kline or "close" not in kline:
        return None
    idx = pd.DatetimeIndex(pd.to_datetime(kline["time_key"]))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s = pd.Series(kline["close"].astype(float).values, index=idx.normalize()).dropna()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s if not s.empty else None


def kline_last_date(kline: pd.DataFrame | None) -> date | None:
    s = _close_series(kline)
    return s.index[-1].date() if s is not None else None


def _strength(wide: pd.DataFrame) -> pd.Series:
    """Weighted ladder return per column of a date-indexed close frame
    (NaN before a column's first bar; no NaN after it)."""
    last = wide.iloc[-1]
    total = pd.Series(0.0, index=wide.columns)
    for quarters, weight in WEIGHTS_12M:
        window = wide.iloc[-quarters * BARS_PER_QUARTER:]
        # First available close inside the window: a name listed mid-window
        # is measured from its first bar, as upstream's min(len, n*63) does.
        first = window.bfill().iloc[0]
        first = first.where(first > 0)
        total = total + weight * (last / first - 1.0)
    return total


def _percentile(rs: pd.Series) -> pd.Series:
    """Upstream's `pd.qcut(rs, 100, labels=False, duplicates="drop")`. On a
    degenerate input (a handful of names, all-equal values) qcut raises or
    yields NaN; fall back to a rank percentile so the table stays integer."""
    try:
        pct = pd.qcut(rs, 100, labels=False, duplicates="drop")
        if not pct.isna().any():
            return pct.astype(int)
    except ValueError:
        pass
    return (rs.rank(method="average", pct=True) * 99).round().astype(int)


def compute_us_rs_12m_table(
    klines: dict[str, pd.DataFrame],
    spy_kline: pd.DataFrame | None,
) -> pd.DataFrame:
    """Return a DataFrame indexed by ticker (strongest first) with columns
    `Relative Strength`, `Percentile` (0-99) and `Session` (ISO date of SPY's
    last bar). Empty frame when SPY is unusable or nothing scores.

    ``klines`` values are the `time_key`/`close` frames of
    ``us_rs_3m.fetch_us_klines_yf``. Bars after SPY's last date are ignored;
    a gap inside a stock's history carries the previous close forward.
    """
    empty = pd.DataFrame(columns=[COL_RS, COL_PERCENTILE, COL_SESSION])
    spy = _close_series(spy_kline)
    if spy is None or len(spy) < 2:
        logger.error("[US RS 12M] SPY series unusable — no table")
        return empty
    calendar = spy.index
    session = calendar[-1]

    series: dict[str, pd.Series] = {}
    for ticker, kline in klines.items():
        s = _close_series(kline)
        if s is not None:
            series[ticker] = s
    if not series:
        return empty

    raw = pd.DataFrame(series).reindex(calendar)
    bars = raw.notna().sum()
    stale_cutoff = calendar[max(len(calendar) - 1 - MAX_STALE_SESSIONS, 0)]
    last_bar = raw.apply(pd.Series.last_valid_index)
    wide = raw.ffill()

    spy_strength = float(_strength(spy.to_frame("SPY")).iloc[0])
    strength = _strength(wide)
    rs = (1.0 + strength) / (1.0 + spy_strength) * 100.0
    # Upstream truncates: int(rs * 100) / 100.
    rs = (rs * 100).apply(lambda v: int(v) / 100 if pd.notna(v) else v)

    short = bars < MIN_BARS
    stale = last_bar < stale_cutoff
    invalid = rs.isna() & ~short
    too_strong = (rs >= MAX_RS) & ~short & ~stale
    keep = ~short & ~stale & rs.notna() & (rs < MAX_RS)
    logger.info(
        f"[US RS 12M] session {session.date()}: {int(keep.sum())}/{len(series)} ranked "
        f"(short_history {int(short.sum())}, stale {int((stale & ~short).sum())}, "
        f"rs>={MAX_RS:.0f} {int(too_strong.sum())}, invalid {int(invalid.sum())})"
    )
    rs = rs[keep]
    if rs.empty:
        return empty

    table = pd.DataFrame({COL_RS: rs})
    table[COL_PERCENTILE] = _percentile(rs)
    table[COL_SESSION] = session.date().isoformat()
    table = table.sort_values(COL_RS, ascending=False, kind="stable")
    table.index.name = COL_TICKER
    return table
