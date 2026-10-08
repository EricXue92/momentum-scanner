"""Daily list of US master tickers whose close FIRST fell below EMA20.

Runs in us-eod right before the SMA50 prune, over the same universe
(``eod_seen_US.txt``): a ticker qualifies when its latest completed close is
below EMA20 AND the prior close was at/above it (today is the cross-down day,
not day 2+ under the line). List only — the master is never modified.

Output: ``output/TV/US/<date>_EMA20Break.txt`` (comma-sep, TradingView-
importable). Nothing found -> nothing written (an existing file is left as
is). A same-day rerun overwrites. Only the newest ``keep_files`` (3) such
files are kept; older ones are deleted here, after each write.

Soft-fail by design: a total yfinance failure skips the step with a warning;
tickers with missing data or too little history are skipped. US only.
"""

import logging
import re

from pathlib import Path

import pandas as pd

from sma50_prune import _fetch_daily_closes

logger = logging.getLogger("momentum_scanner")

_LABEL = "ema20-break"
_FILE_RE = re.compile(r"^\d{4}_\d{2}_\d{2}_EMA20Break\.txt$")


def find_ema20_first_breaks(
    closes_by_ticker: dict[str, pd.Series], ema_period: int = 20
) -> list[str]:
    """Tickers whose latest close is below EMA20 while the prior close was
    at/above the prior day's EMA20. The EMA (``adjust=False``) includes each
    day's own close. Tickers with fewer than ``ema_period + 1`` bars are
    skipped."""
    hits = []
    for ticker, closes in closes_by_ticker.items():
        if len(closes) < ema_period + 1:
            continue
        ema = closes.ewm(span=ema_period, adjust=False).mean()
        if closes.iloc[-1] < ema.iloc[-1] and closes.iloc[-2] >= ema.iloc[-2]:
            hits.append(ticker)
    return sorted(hits)


def _prune_old_lists(out_dir: Path, keep_files: int) -> None:
    """Keep only the newest ``keep_files`` dated EMA20Break lists (by the date
    in the name — the zero-padded YYYY_MM_DD prefix sorts chronologically)."""
    files = sorted(p for p in out_dir.iterdir() if _FILE_RE.match(p.name))
    for old in files[:-keep_files] if keep_files > 0 else []:
        try:
            old.unlink()
        except OSError as e:
            logger.warning(f"[{_LABEL}] could not delete {old}: {e}")


def write_ema20_break_list(seen_path: Path, cfg: dict, out_path: Path) -> list[str]:
    """Write today's first-break-below-EMA20 tickers to ``out_path``. Returns
    them. No-op when disabled, when the master is missing/empty, or when the
    batch download fails (warned)."""
    if not cfg.get("enabled", False):
        return []
    if not seen_path.exists():
        logger.info(f"[{_LABEL}] master missing, nothing to check: {seen_path}")
        return []
    tickers = [ln.strip() for ln in seen_path.read_text().splitlines() if ln.strip()]
    if not tickers:
        logger.info(f"[{_LABEL}] master empty, nothing to check")
        return []

    ema_period = int(cfg.get("ema_period", 20))
    closes = _fetch_daily_closes(tickers)
    if closes is None:
        logger.warning(f"[{_LABEL}] yfinance batch download failed — skipping")
        return []

    hits = find_ema20_first_breaks(closes, ema_period=ema_period)
    logger.info(
        f"[{_LABEL}] checked {len(closes)}/{len(tickers)} tickers | close first "
        f"below EMA{ema_period}: {len(hits)}"
        + (f" -> {','.join(hits)}" if hits else "")
    )
    if hits:
        try:
            out_path.write_text(",".join(hits) + "\n")
            logger.info(f"[{_LABEL}] -> {out_path}")
        except OSError as e:
            logger.warning(f"[{_LABEL}] could not write {out_path}: {e}")
    _prune_old_lists(out_path.parent, int(cfg.get("keep_files", 3)))
    return hits
