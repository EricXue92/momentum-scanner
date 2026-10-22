"""Daily SMA50 prune of the US cross-day master (``eod_seen_US.txt``).

Runs at the top of the us-eod pipeline (before the master is loaded): any
ticker whose close has been below its SMA50 for ``consecutive_days`` completed
trading days in a row AND whose latest close is below the prior day's close
(still declining, not rebounding under the line) is removed from the master,
so it can re-qualify on a future EOD run once it recovers. The master is
backed up first (``eod_seen_US.txt.bak.<stamp>``, same scheme as rs-line-audit).

Soft-fail by design: a total yfinance failure skips the prune with a warning;
tickers with missing data or fewer than ``sma_period`` bars are KEPT. The one
exception is a delisting: a name with no daily bar for
``delisted_after_sessions`` sessions (confirmed by a separate re-download) is
removed too — it can never re-qualify, and was judged on weeks-old closes.
US only — the HK master is untouched.
"""

import logging

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from rs_line_audit import _prune_master

logger = logging.getLogger("momentum_scanner")

_LABEL = "sma50-prune"


class MasterCloses(dict):
    """``{ticker: close series}`` as ``_fetch_daily_closes`` returns it, plus
    ``.stale`` = ``{ticker: (last close date, sessions behind)}`` for the names
    ``drop_stale_series`` left out. A plain dict (test fakes) means "no stale
    info" — nothing is treated as delisted."""

    stale: dict[str, tuple[pd.Timestamp, int]]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale = {}


def _fetch_daily_closes(tickers: list[str]) -> "MasterCloses | None":
    """Batch-download daily closes for the master's tickers.

    Returns {ticker: close series} (absent/empty tickers omitted) as a
    ``MasterCloses`` carrying the stale names, or None when the batch download
    failed entirely. Monkeypatched in tests.
    """
    from main import _yf_download_with_retry  # local import: main imports us

    data = _yf_download_with_retry(
        tickers, period="6mo", progress=False, group_by="ticker", threads=False
    )
    if data is None or data.empty:
        return None
    single = len(tickers) == 1
    raw: dict[str, pd.Series] = {}
    for t in tickers:
        try:
            series = data["Close"] if single else data[t]["Close"]
        except (KeyError, IndexError):
            continue
        if series.notna().any():
            raw[t] = series
    stale: dict[str, tuple[pd.Timestamp, int]] = {}
    raw = drop_stale_series(raw, stale_out=stale)
    filled = fill_missing_last_close(raw)
    out = MasterCloses({t: s.dropna() for t, s in filled.items() if s.notna().any()})
    out.stale = stale
    return out


# A trailing NaN Close for ONE session is Yahoo's daily-row lag (filled from
# intraday below). Two or more sessions with no bar while the rest of the
# batch has them is a delisting / merger / halt, not lag.
_MAX_LAG_SESSIONS = 1


def drop_stale_series(
    closes: dict[str, pd.Series],
    stale_out: dict[str, tuple[pd.Timestamp, int]] | None = None,
) -> dict[str, pd.Series]:
    """Leave out tickers whose last valid close is more than
    ``_MAX_LAG_SESSIONS`` sessions behind the batch's latest session.

    A "session" is a date on which at least half of the tickers have a close,
    so one name's stray extra row cannot make the whole master look stale,
    and the all-NaN last row of a Yahoo-lag day does not count either.

    The batch frame's index is the union of every ticker's dates, so a name
    that stopped trading carries trailing NaN rows; a plain ``dropna`` then
    evaluates SMA50 / EMA20 on its last real (weeks-old) closes — QRVO and
    FBRX 2026-10-17. Excluded names are reported, and recorded in
    ``stale_out`` as ``(last close date, sessions behind)`` when given; only
    ``prune_us_master``'s delisting rule removes any of them from the master.
    """
    if not closes:
        return closes
    valid = pd.concat(closes.values(), axis=1).notna().sum(axis=1)
    session_index = valid.index[valid >= max(1, len(closes) / 2)]
    stale: dict[str, pd.Timestamp] = {}
    out: dict[str, pd.Series] = {}
    for t, s in closes.items():
        last = s.last_valid_index()
        lag = int((session_index > last).sum()) if last is not None else 0
        if lag > _MAX_LAG_SESSIONS:
            stale[t] = pd.Timestamp(last)
            if stale_out is not None:
                stale_out[t] = (pd.Timestamp(last), lag)
        else:
            out[t] = s
    if stale:
        logger.warning(
            f"[{_LABEL}] no daily bar for >{_MAX_LAG_SESSIONS} session(s) for "
            f"{len(stale)} ticker(s) — delisted/halted? excluded from the SMA50/EMA20 "
            "checks: "
            + ", ".join(f"{t} (last {d.date()})" for t, d in sorted(stale.items()))
        )
    return out


def _confirm_no_new_bars(
    tickers: list[str], last_seen: dict[str, pd.Timestamp]
) -> list[str]:
    """Re-download ``tickers`` on their own and return those that still have
    no close after ``last_seen[t]`` — the check before a stale name leaves the
    master, so one truncated series in the big batch cannot delete a live
    ticker. A failed download confirms nothing (returns []). A ticker with no
    data at all in a frame that came back is confirmed (Yahoo: "possibly
    delisted"). Monkeypatched in tests."""
    from main import _yf_download_with_retry  # local import: main imports us

    if not tickers:
        return []
    data = _yf_download_with_retry(
        tickers, period="6mo", progress=False, group_by="ticker", threads=False
    )
    if data is None or data.empty:
        return []
    flat = not isinstance(data.columns, pd.MultiIndex)
    confirmed = []
    for t in tickers:
        try:
            closes = (data["Close"] if flat else data[t]["Close"]).dropna()
        except (KeyError, IndexError):
            closes = pd.Series(dtype=float)
        if closes.empty or pd.Timestamp(closes.index[-1]) <= last_seen[t]:
            confirmed.append(t)
    return confirmed


def _find_delisted(
    closes: dict[str, pd.Series], master: list[str], after_sessions: int
) -> list[str]:
    """Master names ``drop_stale_series`` found ≥ ``after_sessions`` sessions
    behind, confirmed by ``_confirm_no_new_bars``. Off when ``after_sessions``
    is 0 or the closes carry no stale info (plain dict)."""
    stale = getattr(closes, "stale", None) or {}
    if after_sessions <= 0 or not stale:
        return []
    in_master = set(master)
    cands = sorted(t for t, (_, lag) in stale.items() if lag >= after_sessions and t in in_master)
    if not cands:
        return []
    confirmed = _confirm_no_new_bars(cands, {t: stale[t][0] for t in cands})
    kept = [t for t in cands if t not in confirmed]
    if kept:
        logger.warning(
            f"[{_LABEL}] stale for >= {after_sessions} sessions but the recheck "
            f"found newer bars (or failed) — kept: {','.join(kept)}"
        )
    return sorted(confirmed)


def _fetch_intraday_last_close(tickers: list[str], day: date) -> dict[str, float]:
    """Close of the last regular-session 1h bar on ``day`` per ticker, from one
    batch download. Tickers with no bar that day are omitted. Monkeypatched in
    tests."""
    from main import _yf_download_with_retry  # local import: main imports us

    data = _yf_download_with_retry(
        tickers, start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(),
        interval="1h", progress=False, group_by="ticker", threads=False,
    )
    if data is None or data.empty:
        return {}
    single = len(tickers) == 1
    out: dict[str, float] = {}
    for t in tickers:
        try:
            bars = (data["Close"] if single else data[t]["Close"]).dropna()
        except (KeyError, IndexError):
            continue
        if not bars.empty:
            out[t] = float(bars.iloc[-1])
    return out


def fill_missing_last_close(
    closes: dict[str, pd.Series],
    fetch_intraday=None,
    label: str = _LABEL,
) -> dict[str, pd.Series]:
    """Patch a trailing NaN daily Close from that day's intraday bars.

    Yahoo occasionally publishes the latest session's daily row with OHLV but a
    NaN Close for hours after the close (all tickers, 2026-09-22): a plain
    ``dropna`` then silently evaluates the prune one day stale, so CF — two
    closes below SMA50 and still falling — was kept. Tickers whose last row is
    NaN are grouped by date and filled from one 1h-bar batch per date; a
    ticker the intraday fetch cannot cover loses that row (evaluated one day
    stale, as before). Everything is logged as a warning so a stale-data day
    is visible in the EOD log. ``label`` prefixes that warning — the ETF RS
    ranking reuses this via ``us_rs_3m.fetch_us_klines_yf(fill_last_close=)``.
    """
    fetch = fetch_intraday or _fetch_intraday_last_close
    by_day: dict[date, list[str]] = {}
    for t, s in closes.items():
        if len(s) and pd.isna(s.iloc[-1]):
            by_day.setdefault(pd.Timestamp(s.index[-1]).date(), []).append(t)
    if not by_day:
        return closes
    out = dict(closes)
    for day, tickers in sorted(by_day.items()):
        got = fetch(tickers, day)
        for t in tickers:
            if t in got:
                s = out[t].copy()
                s.iloc[-1] = got[t]
                out[t] = s
            else:
                out[t] = out[t].iloc[:-1]  # no intraday bar: evaluate one day stale
        unfilled = [t for t in tickers if t not in got]
        logger.warning(
            f"[{label}] daily Close missing for {len(tickers)} ticker(s) on {day} "
            f"(Yahoo lag); filled {len(got)} from intraday last bar"
            + (f", {len(unfilled)} still missing (evaluated one day stale): "
               f"{','.join(unfilled[:10])}{'...' if len(unfilled) > 10 else ''}"
               if unfilled else "")
        )
    return out


def find_sma50_drops(
    closes_by_ticker: dict[str, pd.Series],
    consecutive_days: int = 2,
    sma_period: int = 50,
) -> list[str]:
    """Tickers whose close sat below SMA50 on each of the last N completed days
    AND whose latest close is below the prior day's close (still declining — a
    ticker rebounding under the line is kept).

    The SMA is evaluated per-day (rolling, includes that day's close). Tickers
    with fewer than ``sma_period + consecutive_days - 1`` bars are kept — the
    oldest checked day must have a defined SMA.
    """
    drops = []
    for ticker, closes in closes_by_ticker.items():
        sma = closes.rolling(sma_period).mean()
        tail_close = closes.iloc[-consecutive_days:]
        tail_sma = sma.iloc[-consecutive_days:]
        if len(tail_close) < consecutive_days or tail_sma.isna().any():
            continue  # insufficient history -> keep
        if not (tail_close.values < tail_sma.values).all():
            continue
        if closes.iloc[-1] >= closes.iloc[-2]:
            continue  # flat/rebounding under the line -> keep
        drops.append(ticker)
    return sorted(drops)


def _write_pruned_list(pruned_path: Path, drops: list[str]) -> None:
    """Record today's pruned tickers in ``<date>_SMA50Pruned.txt`` (comma-sep,
    TradingView-importable). Merges with an existing file: a same-day rerun
    only sees names the earlier run left in the master, so overwriting would
    lose the earlier prunes. Nothing pruned -> nothing written. Soft-fail —
    the master prune has already happened and the log lists the names."""
    if not drops:
        return
    try:
        earlier = (
            [t.strip() for t in pruned_path.read_text().split(",") if t.strip()]
            if pruned_path.exists() else []
        )
        merged = sorted(set(earlier) | set(drops))
        pruned_path.write_text(",".join(merged) + "\n")
        logger.info(
            f"[{_LABEL}] {len(drops)} pruned today ({len(merged)} total) "
            f"-> {pruned_path}"
        )
    except OSError as e:
        logger.warning(f"[{_LABEL}] could not write {pruned_path}: {e}")


def prune_us_master(
    seen_path: Path, cfg: dict, pruned_path: Path | None = None,
    closes: dict[str, pd.Series] | None = None,
) -> list[str]:
    """Prune SMA50 breakdowns from the US master. Returns the dropped tickers.

    No-op when disabled, when the master is missing/empty, or when the batch
    download fails (warned, master untouched). ``pruned_path``, when given,
    receives the day's dropped tickers (see ``_write_pruned_list``).
    ``closes`` (``{ticker: close series}``) skips the download — us-eod
    fetches the master once and shares it with the EMA20 break list.
    """
    if not cfg.get("enabled", False):
        return []
    if not seen_path.exists():
        logger.info(f"[{_LABEL}] master missing, nothing to prune: {seen_path}")
        return []
    tickers = [ln.strip() for ln in seen_path.read_text().splitlines() if ln.strip()]
    if not tickers:
        logger.info(f"[{_LABEL}] master empty, nothing to prune")
        return []

    consecutive_days = int(cfg.get("consecutive_days", 2))
    if closes is None:
        closes = _fetch_daily_closes(tickers)
    if closes is None:
        logger.warning(
            f"[{_LABEL}] yfinance batch download failed — skipping prune, "
            "master untouched"
        )
        return []

    drops = find_sma50_drops(closes, consecutive_days=consecutive_days)
    delisted = _find_delisted(
        closes, tickers, int(cfg.get("delisted_after_sessions", 0))
    )
    if delisted:
        stale = closes.stale
        logger.info(
            f"[{_LABEL}] delisted (no daily bar for >= "
            f"{int(cfg['delisted_after_sessions'])} sessions, rechecked) — "
            "removed from the master: "
            + ", ".join(f"{t} (last {stale[t][0].date()})" for t in delisted)
        )
    missing = len(tickers) - len(closes) - len(delisted)
    logger.info(
        f"[{_LABEL}] checked {len(closes)}/{len(tickers)} tickers "
        f"(missing data: {missing}, kept) | close < SMA50 for "
        f"{consecutive_days} consecutive day(s) + still declining: {len(drops)}"
        + (f" -> {','.join(drops)}" if drops else "")
    )
    _prune_master(seen_path, sorted(set(drops) | set(delisted)), label=_LABEL)
    if pruned_path is not None:
        _write_pruned_list(pruned_path, drops)
    return drops
