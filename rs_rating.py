#!/usr/bin/env python3
"""IBD-style Relative Strength Rating fetcher.

Pulls the daily-published RS percentile table from Fred6725/rs-log
(itself the GitHub-Actions output of Fred6725/relative-strength) and
exposes a filter helper for the EOD pipeline.

The .txt files remain the primary artifact — RS is an extra screen on
top of Finviz output. If the CSV cannot be fetched, every filter call
becomes a logged no-op so the pipeline still produces results.
"""

from __future__ import annotations

import csv
import http.client
import io
import logging
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

RS_CSV_URL = "https://raw.githubusercontent.com/Fred6725/rs-log/main/output/rs_stocks.csv"

# Retry schedule for the HTTPS GET. The launchd run fires shortly after a
# pmset-driven wake, where Wi-Fi / DNS sometimes isn't ready for the first
# minute or two. Three attempts spread across ~40s ride through that window
# without turning a brief blip into a skipped RS gate for the day.
_FETCH_RETRY_DELAYS = (10.0, 30.0)

# Max staleness for the local-cache fallback when today's fetch fails.
# US EOD runs Tue-Sat, so a Mon-holiday + Tue-failure gap is at worst 2 days;
# 3 gives one day of safety margin. Beyond that the percentile drift makes
# "no gate" more honest than "wrong gate".
_FALLBACK_MAX_AGE_DAYS = 3

# Staleness check. rs-log publishes between ~01:30Z and ~04:00Z and us-eod
# fetches at ~02:00Z, so in 2026-10 it got the previous session's table on
# 14/19 days. The CSV has no date column; its `Price` is the session close,
# matched against these mega caps' raw yfinance closes. A table behind the
# latest completed session is re-fetched every _STALE_POLL_SEC (also rides
# out raw.githubusercontent's ~5 min CDN cache) for up to _STALE_MAX_WAIT_SEC,
# then used anyway with a WARNING but never cached as today's.
_REFERENCE_TICKERS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL")
_PRICE_MATCH_REL_TOL = 0.001
_STALE_POLL_SEC = 300.0
_STALE_MAX_WAIT_SEC = 3600.0
_ET = ZoneInfo("America/New_York")
_US_CLOSE_HOUR = 16

# IncompleteRead (truncated body) is an HTTPException, not an OSError; a
# garbage body raises UnicodeDecodeError. Both used to escape and abort
# us-eod before any .txt was written.
_FETCH_ERRORS = (URLError, TimeoutError, OSError,
                 http.client.HTTPException, UnicodeDecodeError)


def _find_fallback_cache(
    state_dir: Path, today: str, today_filename: str
) -> tuple[Path, int] | None:
    """Pick the most recent rs_rating_*.csv within _FALLBACK_MAX_AGE_DAYS
    of `today`. Returns (path, age_days) or None.

    `today_filename` is the cache name for today; it's excluded from
    candidates because if it were readable we wouldn't be in the
    fallback path. `today` is the underscore-formatted YYYY_MM_DD string
    callers already pass to `fetch_rs_table`.
    """
    try:
        today_date = datetime.strptime(today, "%Y_%m_%d").date()
    except ValueError:
        logger.warning(f"[RS Rating] today={today!r} not parseable; no fallback")
        return None
    if not state_dir.exists():
        return None
    best: tuple[Path, int] | None = None
    for p in state_dir.glob("rs_rating_*.csv"):
        if p.name == today_filename:
            continue
        stem_date = p.stem.removeprefix("rs_rating_")
        try:
            file_date = datetime.strptime(stem_date, "%Y_%m_%d").date()
        except ValueError:
            continue
        delta = (today_date - file_date).days
        # Negative delta = future-dated file (clock skew); skip
        if delta < 0 or delta > _FALLBACK_MAX_AGE_DAYS:
            continue
        if best is None or delta < best[1]:
            best = (p, delta)
    return best


def _fetch_reference_closes():
    """Raw daily closes (auto_adjust=False — the table's Price is unadjusted)
    of _REFERENCE_TICKERS over the last ~10 days, or None. Indirection so
    tests can monkeypatch it."""
    from main import _yf_download_with_retry  # lazy: main imports rs_rating

    data = _yf_download_with_retry(
        list(_REFERENCE_TICKERS), period="10d", interval="1d",
        auto_adjust=False, progress=False, threads=True,
    )
    if data is None or data.empty or "Close" not in data.columns.get_level_values(0):
        return None
    return data["Close"]


def latest_completed_session(closes, now_et: datetime | None = None) -> date | None:
    """Latest date with any reference close, excluding the bar of a US
    session still in progress (a manual run during US hours)."""
    now_et = now_et or datetime.now(_ET)
    dates = [ts.date() for ts in closes.dropna(how="all").index]
    if now_et.hour < _US_CLOSE_HOUR:
        dates = [d for d in dates if d != now_et.date()]
    return max(dates) if dates else None


def _reference_prices(csv_text: str) -> dict[str, float]:
    """{ticker: Price} of the _REFERENCE_TICKERS rows; empty when the table
    has no usable Price column."""
    prices: dict[str, float] = {}
    try:
        for row in csv.DictReader(io.StringIO(csv_text)):
            t = (row.get("Ticker") or "").strip().upper()
            if t in _REFERENCE_TICKERS:
                try:
                    prices[t] = float(row.get("Price") or "")
                except ValueError:
                    continue
    except csv.Error:
        return {}
    return prices


def table_data_session(csv_text: str, closes) -> date | None:
    """Session whose reference closes match the table's `Price` column
    (majority vote over _REFERENCE_TICKERS), or None when undeterminable."""
    prices = _reference_prices(csv_text)
    votes: Counter[date] = Counter()
    for t, price in prices.items():
        if t not in closes.columns:
            continue
        for ts, close in closes[t].dropna().items():
            if abs(close - price) <= _PRICE_MATCH_REL_TOL * close:
                votes[ts.date()] += 1
    if not votes:
        return None
    session, n = votes.most_common(1)[0]
    return session if n * 2 > len(prices) else None


def _download_csv(timeout: float, retry_delays: tuple[float, ...]) -> str:
    """GET RS_CSV_URL with retries; raises the last error on failure."""
    req = Request(RS_CSV_URL, headers={"User-Agent": "momentum-scanner/1.0"})
    attempts = len(retry_delays) + 1
    for i in range(attempts):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except _FETCH_ERRORS as e:
            if i == attempts - 1:
                raise
            delay = retry_delays[i]
            logger.warning(
                f"[RS Rating] Fetch attempt {i + 1}/{attempts} failed "
                f"({e}); retrying in {delay:.0f}s"
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def _wait_for_fresh_table(csv_text: str, timeout: float) -> tuple[str, bool]:
    """Return (csv_text, cacheable). Polls while the table is behind the
    latest completed session; cacheable is False only when it never caught
    up. Any failure of the check itself → (csv_text, True), as before."""
    if not _reference_prices(csv_text):
        logger.warning("[RS Rating] Freshness check skipped (no reference prices in table)")
        return csv_text, True
    try:
        closes = _fetch_reference_closes()
    except Exception as e:  # yfinance raises a zoo of types
        logger.warning(f"[RS Rating] Freshness check skipped ({e})")
        return csv_text, True
    if closes is None:
        logger.warning("[RS Rating] Freshness check skipped (no reference closes)")
        return csv_text, True
    latest = latest_completed_session(closes)
    session = table_data_session(csv_text, closes)
    if latest is None or session is None:
        logger.warning(
            f"[RS Rating] Freshness check skipped (table session={session}, "
            f"latest session={latest})"
        )
        return csv_text, True
    if session >= latest:
        logger.info(f"[RS Rating] Table is current (session {session})")
        return csv_text, True

    logger.warning(
        f"[RS Rating] Table is stale (session {session} < {latest}); polling "
        f"every {_STALE_POLL_SEC / 60:.0f} min for up to "
        f"{_STALE_MAX_WAIT_SEC / 60:.0f} min"
    )
    start = time.monotonic()
    while time.monotonic() - start + _STALE_POLL_SEC <= _STALE_MAX_WAIT_SEC:
        time.sleep(_STALE_POLL_SEC)
        try:
            text = _download_csv(timeout, ())
        except _FETCH_ERRORS as e:
            logger.warning(f"[RS Rating] Re-fetch failed ({e}); still waiting")
            continue
        new_session = table_data_session(text, closes)
        if new_session is not None and new_session >= latest:
            waited = (time.monotonic() - start) / 60
            logger.info(
                f"[RS Rating] Fresh table after {waited:.0f} min (session {new_session})"
            )
            return text, True
    logger.warning(
        f"[RS Rating] Still stale after {_STALE_MAX_WAIT_SEC / 60:.0f} min — "
        f"gating on session {session} (not cached; a rerun re-fetches)"
    )
    return csv_text, False


def fetch_rs_table(
    output_dir: Path, today: str, timeout: float = 30.0
) -> dict[str, int] | None:
    """Download rs_stocks.csv and return {ticker: percentile (0-99)}.

    A table behind the latest completed US session is polled for up to
    _STALE_MAX_WAIT_SEC (`_wait_for_fresh_table`). Same-day re-runs are
    offline (cached at output/state/rs_rating_<today>.csv; a table that
    stayed stale is not cached). Returns None on any failure
    (network, parse, missing columns) so callers can degrade gracefully.
    """
    cache_path = output_dir / "state" / f"rs_rating_{today}.csv"

    csv_text: str | None = None
    if cache_path.exists():
        try:
            csv_text = cache_path.read_text(encoding="utf-8")
            logger.info(f"[RS Rating] Using cached CSV: {cache_path}")
        except OSError as e:
            logger.warning(f"[RS Rating] Cache read failed ({e}); refetching")

    if csv_text is None:
        attempts = len(_FETCH_RETRY_DELAYS) + 1
        last_err: Exception | None = None
        try:
            csv_text = _download_csv(timeout, _FETCH_RETRY_DELAYS)
        except _FETCH_ERRORS as e:
            last_err = e
        if csv_text is None:
            logger.warning(
                f"[RS Rating] Fetch failed after {attempts} attempts ({last_err}); "
                "trying local cache fallback"
            )
            fallback = _find_fallback_cache(
                cache_path.parent, today, cache_path.name
            )
            if fallback is None:
                logger.warning(
                    f"[RS Rating] No fallback within {_FALLBACK_MAX_AGE_DAYS} days; "
                    "RS filter will be skipped"
                )
                return None
            fb_path, age_days = fallback
            try:
                csv_text = fb_path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(
                    f"[RS Rating] Fallback cache {fb_path.name} read failed: {e}; "
                    "RS filter will be skipped"
                )
                return None
            logger.warning(
                f"[RS Rating] Using stale fallback cache: {fb_path.name} "
                f"({age_days} day(s) old)"
            )
        else:
            csv_text, cacheable = _wait_for_fresh_table(csv_text, timeout)
            if cacheable:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(csv_text, encoding="utf-8")
                except OSError as e:
                    logger.warning(f"[RS Rating] Cache write failed: {e}")

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        if (
            not reader.fieldnames
            or "Ticker" not in reader.fieldnames
            or "Percentile" not in reader.fieldnames
        ):
            logger.warning(
                f"[RS Rating] Unexpected CSV columns {reader.fieldnames}; "
                "expected Ticker + Percentile"
            )
            return None
        table: dict[str, int] = {}
        for row in reader:
            t = (row.get("Ticker") or "").strip().upper()
            p = (row.get("Percentile") or "").strip()
            if not t or not p:
                continue
            try:
                table[t] = int(float(p))
            except ValueError:
                continue
        if not table:
            logger.warning("[RS Rating] CSV parsed but no rows extracted")
            return None
        logger.info(
            f"[RS Rating] Loaded {len(table)} tickers (IBD percentile 0-99)"
        )
        return table
    except csv.Error as e:
        logger.warning(f"[RS Rating] CSV parse failed: {e}")
        return None


def filter_by_rs(
    tickers: list[str],
    rs_table: dict[str, int] | None,
    min_percentile: int,
    label: str,
) -> list[str]:
    """Drop tickers with RS percentile below `min_percentile`.

    - rs_table is None      → no-op, return input (one-line warning).
    - min_percentile <= 0   → no-op, return input.
    - Ticker not in table   → KEPT (likely a new IPO without 12mo history;
                              dropping silently would surprise the user).
    """
    if not tickers:
        return tickers
    if rs_table is None:
        logger.warning(
            f"{label} RS table unavailable, skipping RS >= {min_percentile} filter"
        )
        return tickers
    if min_percentile <= 0:
        return tickers

    kept: list[str] = []
    dropped = 0
    missing = 0
    for t in tickers:
        rs = rs_table.get(t.upper())
        if rs is None:
            kept.append(t)
            missing += 1
        elif rs >= min_percentile:
            kept.append(t)
        else:
            dropped += 1
    logger.info(
        f"{label} {len(kept)} after RS >= {min_percentile} "
        f"(dropped {dropped}, kept-as-missing {missing})"
    )
    return kept
