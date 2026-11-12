#!/usr/bin/env python3
"""Cloud-side US 3M RS table compute — runs in GitHub Actions weekday cron.

Pulls Fred6725's 12M CSV for the universe, fetches 6mo of yfinance closes
for each ticker (curl_cffi auto-active), computes IBD-style 3M RS via
us_rs_3m.compute_us_rs_3m_table, writes data/us_rs_3m/<today>.csv, and
prunes files older than 14 days.

Exits 1 on:
  - Fred6725 CSV unavailable (no universe to score)
  - Coverage < 50% of universe (Yahoo throttle on the runner — better
    to fail loudly than commit a warped distribution)
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make repo root importable so we can use `us_rs_3m` and `rs_rating` as
# top-level modules (mirrors how main.py imports them).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

import tomllib  # noqa: E402

from rs_rating import fetch_rs_table  # noqa: E402
from rs_line import (  # noqa: E402
    compute_rs_line_features,
    is_enabled,
    params_from_config,
)
from us_rs_3m import (  # noqa: E402
    WEIGHTS_1M_BARS,
    _fetch_spy_kline,
    compute_us_rs_3m_table,
    fetch_us_klines_yf,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compute_us_rs_3m_cloud")

_DATA_DIR = _REPO_ROOT / "data" / "us_rs_3m"
_RETENTION_DAYS = 14
_COVERAGE_THRESHOLD = 0.5


def _today() -> date:
    """Indirection for tests to monkeypatch today's date."""
    return date.today()


def _prune_old_files(today: date) -> int:
    """Delete <YYYY-MM-DD>.csv files in _DATA_DIR older than _RETENTION_DAYS.

    Returns the count of pruned files. Non-date filenames (README.md,
    .gitkeep) are left untouched.
    """
    if not _DATA_DIR.exists():
        return 0
    cutoff = today - timedelta(days=_RETENTION_DAYS)
    pruned = 0
    for p in _DATA_DIR.glob("*.csv"):
        try:
            file_date = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue  # non-date filename, skip
        if file_date < cutoff:
            p.unlink()
            pruned += 1
    return pruned


def main() -> int:
    today = _today()
    logger.info(f"[Cloud RS 3M] Starting compute for {today.isoformat()}")

    # 1. Pull Fred6725 12M CSV for the universe. Only the ticker list is
    # used, so a table one session behind is fine: do NOT poll for a fresh
    # one (that 60-min wait is this job's whole GH timeout).
    rs_table_12m = fetch_rs_table(
        Path("/tmp"), today.strftime("%Y_%m_%d"), wait_for_fresh=False
    )
    if not rs_table_12m:
        logger.error("[Cloud RS 3M] Fred6725 12M CSV unavailable — no universe to score")
        return 1
    universe = sorted(rs_table_12m.keys())
    logger.info(f"[Cloud RS 3M] Universe: {len(universe)} tickers from Fred6725")

    # 2. Fetch SPY first (mirror local fix B — SPY-first ordering).
    spy_kline = _fetch_spy_kline(period="6mo")
    if spy_kline is None or spy_kline.empty:
        # Publishing absolute scores would be wrong for the local IPO gate:
        # us_ipo subtracts ITS SPY score from a candidate and ranks it in this
        # table's raw_score distribution, so the two must share a basis. No
        # table today -> the local run walks back to yesterday's coherent one.
        logger.error("[Cloud RS 3M] SPY fetch failed — not publishing an un-relativised table")
        return 1

    # 3. Fetch the full universe (curl_cffi auto-active in CI environment).
    klines = fetch_us_klines_yf(universe, period="6mo")
    if not klines:
        logger.error("[Cloud RS 3M] yfinance returned no klines")
        return 1

    # 4. Compute the table.
    import pandas as pd  # local import to keep top-of-file imports minimal
    spy_for_compute = spy_kline
    table = compute_us_rs_3m_table(klines, spy_for_compute)

    # Market-wide 1M percentile for the ETF leaders entry gate, scored on the
    # same klines. Best-effort: a failure must NOT cost the 3M table.
    try:
        table_1m = compute_us_rs_3m_table(klines, spy_for_compute, weights_bars=WEIGHTS_1M_BARS)
        if not table_1m.empty:
            table = table.join(table_1m["rs_percentile"].rename("rs_percentile_1m"), how="left")
    except Exception as exc:
        logger.warning(f"[Cloud RS 3M] 1M percentile failed ({exc}); column omitted")

    # RS-line-vs-MA features (TraderLion-style) merged as extra columns —
    # best-effort/annotation-only: a config read failure must NOT fail the run.
    _cfg = {}
    try:
        with open(_REPO_ROOT / "config.toml", "rb") as f:
            _cfg = tomllib.load(f)
    except Exception as exc:
        logger.warning(f"[Cloud RS 3M] config.toml unreadable ({exc}); skipping RS-line features")
    if _cfg and is_enabled(_cfg) and spy_kline is not None and not spy_kline.empty:
        feats = compute_rs_line_features(klines, spy_kline, **params_from_config(_cfg))
        table = table.join(feats, how="left")
        logger.info(f"[Cloud RS 3M] RS-line features merged for {len(feats)} tickers")

    # 5. Coverage guard.
    coverage = len(table) / len(universe) if universe else 0
    if coverage < _COVERAGE_THRESHOLD:
        logger.error(
            f"[Cloud RS 3M] Coverage {len(table)}/{len(universe)} "
            f"({coverage:.1%}) below {_COVERAGE_THRESHOLD:.0%} threshold — failing"
        )
        return 1

    # 6. Write today's CSV.
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _DATA_DIR / f"{today.isoformat()}.csv"
    table.to_csv(out_path, index_label="ticker")
    logger.info(f"[Cloud RS 3M] Wrote {len(table)} tickers ({coverage:.1%} coverage) → {out_path}")

    # 7. Prune old files.
    pruned = _prune_old_files(today)
    logger.info(f"[Cloud RS 3M] Pruned {pruned} files older than {_RETENTION_DAYS} days")

    return 0


if __name__ == "__main__":
    sys.exit(main())
