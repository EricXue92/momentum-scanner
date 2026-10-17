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
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

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


def fetch_rs_table(
    output_dir: Path, today: str, timeout: float = 30.0
) -> dict[str, int] | None:
    """Download rs_stocks.csv and return {ticker: percentile (0-99)}.

    Same-day re-runs are offline (cached at
    output/state/rs_rating_<today>.csv). Returns None on any failure
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
        req = Request(RS_CSV_URL, headers={"User-Agent": "momentum-scanner/1.0"})
        attempts = len(_FETCH_RETRY_DELAYS) + 1
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                with urlopen(req, timeout=timeout) as resp:
                    csv_text = resp.read().decode("utf-8")
                last_err = None
                break
            # IncompleteRead (truncated body) is an HTTPException, not an
            # OSError; a garbage body raises UnicodeDecodeError. Both used
            # to escape and abort us-eod before any .txt was written.
            except (URLError, TimeoutError, OSError,
                    http.client.HTTPException, UnicodeDecodeError) as e:
                last_err = e
                if i < len(_FETCH_RETRY_DELAYS):
                    delay = _FETCH_RETRY_DELAYS[i]
                    logger.warning(
                        f"[RS Rating] Fetch attempt {i + 1}/{attempts} failed "
                        f"({e}); retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)
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
