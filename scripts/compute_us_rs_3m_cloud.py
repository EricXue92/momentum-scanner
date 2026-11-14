#!/usr/bin/env python3
"""Cloud-side US RS compute (3M + 12M) — runs in GitHub Actions weekday cron.

One ~18-month yfinance download of the nasdaqtrader universe feeds both
tables:
  data/us_rs_3m/<today>.csv   3M (+1M, RS-line) — scored on the last 6 months
  data/us_rs_12m/<today>.csv  12M IBD-style (us_rs_12m.compute_us_rs_12m_table)

Exits 1, publishing NOTHING, on:
  - no universe (nasdaqtrader down and no previously published table)
  - SPY missing, or its last bar still behind the latest completed session
    after the retries (Yahoo sometimes lags a session → decisions 2026-11-14)
  - too many tickers missing that session's bar after one re-fetch (a
    half-updated universe, as upstream published on 2026-09-18)
  - coverage < 50% of the universe (Yahoo throttle on the runner — better
    to fail loudly than commit a warped distribution)
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# Make repo root importable so we can use `us_rs_3m` and `rs_rating` as
# top-level modules (mirrors how main.py imports them).
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

import tomllib  # noqa: E402

import pandas as pd  # noqa: E402

from rs_line import (  # noqa: E402
    compute_rs_line_features,
    is_enabled,
    params_from_config,
)
from us_rs_12m import (  # noqa: E402
    COL_TICKER,
    compute_us_rs_12m_table,
    expected_session,
    fetch_universe,
    kline_last_date,
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
_DATA_DIR_12M = _REPO_ROOT / "data" / "us_rs_12m"
_RETENTION_DAYS = 14
_COVERAGE_THRESHOLD = 0.5

# 252 sessions for the 12M ladder + slack; a relative period, so the request
# has no `end` (yf.download's `end` is exclusive: end=today drops today's bar).
_PERIOD = "18mo"
# The 3M / 1M / RS-line columns keep their pre-12M input: the last 6 months.
_MONTHS_3M = 6

# SPY behind the expected session: Yahoo has not posted the bar yet.
_SPY_STALE_RETRIES = 3
_SPY_STALE_WAIT_SEC = 300.0
# After one re-fetch, more than this share of the universe still without the
# session's bar = a half-updated snapshot. A handful is normal (halts).
_MAX_STALE_FRACTION = 0.10


def _today() -> date:
    """Indirection for tests to monkeypatch today's date."""
    return date.today()


def _prune_old_files(today: date, data_dir: Path | None = None) -> int:
    """Delete <YYYY-MM-DD>.csv files in ``data_dir`` (default _DATA_DIR)
    older than _RETENTION_DAYS.

    Returns the count of pruned files. Non-date filenames (README.md,
    .gitkeep) are left untouched.
    """
    data_dir = data_dir or _DATA_DIR
    if not data_dir.exists():
        return 0
    cutoff = today - timedelta(days=_RETENTION_DAYS)
    pruned = 0
    for p in data_dir.glob("*.csv"):
        try:
            file_date = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue  # non-date filename, skip
        if file_date < cutoff:
            p.unlink()
            pruned += 1
    return pruned


def _last_published_universe() -> list[str]:
    """Tickers of the newest table already in the repo checkout (12M first,
    then 3M) — the universe when nasdaqtrader is unreachable."""
    for data_dir, col in ((_DATA_DIR_12M, COL_TICKER), (_DATA_DIR, "ticker")):
        if not data_dir.exists():
            continue
        dated = sorted(p for p in data_dir.glob("*.csv") if p.stem[:4].isdigit())
        for p in reversed(dated):
            try:
                tickers = pd.read_csv(p, usecols=[col])[col].dropna().astype(str)
            except Exception as exc:
                logger.warning(f"[Cloud RS] {p.name} unreadable as a universe ({exc})")
                continue
            if len(tickers):
                logger.warning(f"[Cloud RS] Universe from previously published {p}")
                return sorted(set(tickers))
    return []


def _trim_after(kline: pd.DataFrame, session: date) -> pd.DataFrame:
    """Drop bars after ``session`` (the partial bar of a US session still in
    progress, on a manual dispatch during US hours)."""
    keep = pd.to_datetime(kline["time_key"]).dt.tz_localize(None).dt.normalize() <= pd.Timestamp(session)
    return kline if keep.all() else kline[keep].reset_index(drop=True)


def _tail_months(kline: pd.DataFrame, session: date, months: int) -> pd.DataFrame:
    cutoff = pd.Timestamp(session) - pd.DateOffset(months=months)
    keep = pd.to_datetime(kline["time_key"]).dt.tz_localize(None) >= cutoff
    return kline[keep].reset_index(drop=True)


def _fetch_current_spy(session: date) -> pd.DataFrame | None:
    """SPY k-line whose last bar is ``session``, trimmed to it; None when the
    fetch fails or Yahoo is still behind after the retries."""
    for attempt in range(_SPY_STALE_RETRIES + 1):
        spy = _fetch_spy_kline(period=_PERIOD)
        if spy is None or spy.empty:
            logger.error("[Cloud RS] SPY fetch failed")
            return None
        spy = _trim_after(spy, session)
        last = kline_last_date(spy)
        if last == session:
            return spy
        if attempt < _SPY_STALE_RETRIES:
            logger.warning(
                f"[Cloud RS] SPY last bar {last} is behind session {session}; "
                f"retry {attempt + 1}/{_SPY_STALE_RETRIES} in {_SPY_STALE_WAIT_SEC / 60:.0f} min"
            )
            time.sleep(_SPY_STALE_WAIT_SEC)
    logger.error(f"[Cloud RS] SPY still behind session {session} (last bar {last})")
    return None


def _refresh_stale(klines: dict[str, pd.DataFrame], session: date) -> list[str]:
    """Re-fetch once the tickers whose last bar is before ``session`` and
    return the ones still behind. ``klines`` is updated in place."""
    stale = [t for t, k in klines.items() if kline_last_date(k) != session]
    if not stale:
        return []
    logger.warning(
        f"[Cloud RS] {len(stale)}/{len(klines)} tickers lack the {session} bar — one re-fetch"
    )
    again = fetch_us_klines_yf(stale, period=_PERIOD, recover=False)
    still: list[str] = []
    for t in stale:
        new = again.get(t)
        if new is not None and not new.empty:
            new = _trim_after(new, session)
            if kline_last_date(new) == session:
                klines[t] = new
                continue
        still.append(t)
    logger.info(f"[Cloud RS] re-fetch: {len(stale) - len(still)}/{len(stale)} now current")
    return still


def main() -> int:
    today = _today()
    logger.info(f"[Cloud RS] Starting compute for {today.isoformat()}")

    # 1. Universe: the nasdaqtrader list, filtered to common equity.
    universe = fetch_universe() or _last_published_universe()
    if not universe:
        logger.error("[Cloud RS] No universe (nasdaqtrader down, no published table)")
        return 1
    logger.info(f"[Cloud RS] Universe: {len(universe)} tickers")

    # 2. SPY first, and it must carry the latest completed session's bar.
    # Without SPY the 3M raw_score would be absolute while the local IPO gate
    # subtracts ITS SPY score before ranking against it; with a lagging SPY
    # both tables would be a session old under today's file name. No table
    # -> the local run walks back to the previous coherent one.
    session = expected_session()
    spy_kline = _fetch_current_spy(session)
    if spy_kline is None:
        logger.error("[Cloud RS] No current SPY — publishing nothing")
        return 1

    # 3. Fetch the full universe (curl_cffi auto-active in CI environment).
    klines = fetch_us_klines_yf(universe, period=_PERIOD)
    if not klines:
        logger.error("[Cloud RS] yfinance returned no klines")
        return 1
    klines = {t: _trim_after(k, session) for t, k in klines.items()}
    klines = {t: k for t, k in klines.items() if not k.empty}

    # 4. Latest-bar validation across the universe.
    still_stale = _refresh_stale(klines, session)
    stale_fraction = len(still_stale) / len(klines) if klines else 1.0
    if stale_fraction > _MAX_STALE_FRACTION:
        logger.error(
            f"[Cloud RS] {len(still_stale)}/{len(klines)} tickers ({stale_fraction:.1%}) "
            f"still lack the {session} bar — a half-updated snapshot, publishing nothing"
        )
        return 1
    if still_stale:
        logger.info(
            f"[Cloud RS] {len(still_stale)} ticker(s) without the {session} bar "
            f"(halted / not traded): {','.join(still_stale[:20])}"
        )

    # 5. 3M table, on the last 6 months as before.
    klines_3m = {t: _tail_months(k, session, _MONTHS_3M) for t, k in klines.items()}
    spy_for_compute = _tail_months(spy_kline, session, _MONTHS_3M)
    table = compute_us_rs_3m_table(klines_3m, spy_for_compute)

    # Market-wide 1M percentile for the ETF leaders entry gate, scored on the
    # same klines. Best-effort: a failure must NOT cost the 3M table.
    try:
        table_1m = compute_us_rs_3m_table(klines_3m, spy_for_compute, weights_bars=WEIGHTS_1M_BARS)
        if not table_1m.empty:
            table = table.join(table_1m["rs_percentile"].rename("rs_percentile_1m"), how="left")
    except Exception as exc:
        logger.warning(f"[Cloud RS] 1M percentile failed ({exc}); column omitted")

    # RS-line-vs-MA features (TraderLion-style) merged as extra columns —
    # best-effort/annotation-only: a config read failure must NOT fail the run.
    _cfg = {}
    try:
        with open(_REPO_ROOT / "config.toml", "rb") as f:
            _cfg = tomllib.load(f)
    except Exception as exc:
        logger.warning(f"[Cloud RS] config.toml unreadable ({exc}); skipping RS-line features")
    if _cfg and is_enabled(_cfg):
        feats = compute_rs_line_features(klines_3m, spy_for_compute, **params_from_config(_cfg))
        table = table.join(feats, how="left")
        logger.info(f"[Cloud RS] RS-line features merged for {len(feats)} tickers")

    # 6. Coverage guard (on the 3M table: it needs 64 rows, the 12M needs 120,
    # so recent IPOs make the 12M table legitimately smaller).
    coverage = len(table) / len(universe) if universe else 0
    if coverage < _COVERAGE_THRESHOLD:
        logger.error(
            f"[Cloud RS] Coverage {len(table)}/{len(universe)} "
            f"({coverage:.1%}) below {_COVERAGE_THRESHOLD:.0%} threshold — failing"
        )
        return 1

    # 7. 12M table. Computed before anything is written: both files or none.
    table_12m = compute_us_rs_12m_table(klines, spy_kline)
    if table_12m.empty:
        logger.error("[Cloud RS] 12M table is empty — publishing nothing")
        return 1

    # 8. Write today's CSVs.
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _DATA_DIR / f"{today.isoformat()}.csv"
    table.to_csv(out_path, index_label="ticker")
    logger.info(f"[Cloud RS] Wrote 3M: {len(table)} tickers ({coverage:.1%} coverage) → {out_path}")

    _DATA_DIR_12M.mkdir(parents=True, exist_ok=True)
    out_12m = _DATA_DIR_12M / f"{today.isoformat()}.csv"
    table_12m.to_csv(out_12m, index_label=COL_TICKER)
    logger.info(f"[Cloud RS] Wrote 12M: {len(table_12m)} tickers, session {session} → {out_12m}")

    # 9. Prune old files.
    pruned = _prune_old_files(today) + _prune_old_files(today, _DATA_DIR_12M)
    logger.info(f"[Cloud RS] Pruned {pruned} files older than {_RETENTION_DAYS} days")

    return 0


if __name__ == "__main__":
    sys.exit(main())
