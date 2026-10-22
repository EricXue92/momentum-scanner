"""Local IBD-style Relative Strength percentile for the US universe (3-month).

Complements `rs_rating.py` (Fred6725-CSV-based 12-month RS) with a locally
computed short-window layer benchmarked against SPY. Universe is the same
~6100 tickers as `rs_rating.py` (Fred6725 CSV); SPY k-line comes from yfinance.

The 3M table is consumed twice:
  1. Long-side 3M gate on Leaders / conditional RS / Shorts
  2. US IPO ladder's 3M RS filter (via raw_score column for out-of-universe
     percentile lookup)
"""

from __future__ import annotations

import io
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

logger = logging.getLogger(__name__)

WEIGHTS_3M: list[tuple[int, float]] = [(1, 0.5), (2, 0.3), (3, 0.2)]
BARS_PER_MONTH = 21

# 1M weekly ladder, in bars: cumulative 1/2/3/4-week returns (5/10/15/20
# trading days back from today, overlapping like the IBD 3/6/9/12M ladder)
# weighted 0.4/0.2/0.2/0.2. Needs 21 rows. Used by the ETF 1M ranking only.
WEIGHTS_1M_BARS: list[tuple[int, float]] = [(5, 0.4), (10, 0.2), (15, 0.2), (20, 0.2)]

# Cloud-published CSV (written by .github/workflows/update_us_rs_3m.yml).
# Local pipeline reads from raw.githubusercontent.com instead of computing
# locally — the home-IP yfinance compute hits Yahoo's IP-cumulative rate
# limit after ~2000 tickers (see spec 2026-05-21-us-rs-3m-cloud-pipeline).
_CLOUD_CSV_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/EricXue92/momentum-scanner/main/"
    "data/us_rs_3m/{date}.csv"
)

# Walk back up to N days if today's CSV isn't published yet. Mirrors
# rs_rating._FALLBACK_MAX_AGE_DAYS — US EOD runs Mon-Fri so a Mon-failure
# + Tue-failure gap is at worst 2 days; 3 gives margin.
_FALLBACK_MAX_AGE_DAYS = 3


def _months_to_bars(weights: list[tuple[int, float]]) -> list[tuple[int, float]]:
    return [(m * BARS_PER_MONTH, w) for m, w in weights]


def _score_from_kline_bars(
    df: pd.DataFrame,
    weights_bars: list[tuple[int, float]],
) -> tuple[float | None, str]:
    """Compute Σ wᵢ·(close[-1] / close[-1-barsᵢ] − 1) from a k-line DataFrame
    sorted ascending by ``time_key``; ``weights_bars`` = (lookback in bars,
    weight). Lookbacks count rows, not calendar days.

    Returns (score, reason); score is None on rejection. Reason ∈
    {"ok", "no_data", "short_history", "zero_last", "zero_past"}.

    Minimum rows = max(bars) + 1 (64 for the 3M ladder, 21 for 1M).
    """
    if df is None or df.empty:
        return None, "no_data"
    min_rows = max(b for b, _ in weights_bars) + 1
    if len(df) < min_rows:
        return None, "short_history"
    closes = df["close"].astype(float).values
    last = float(closes[-1])
    if last <= 0:
        return None, "zero_last"

    score = 0.0
    for bars, w in weights_bars:
        past = float(closes[-1 - bars])
        if past <= 0:
            return None, "zero_past"
        score += w * ((last / past) - 1.0)
    return score, "ok"


def _score_from_kline(
    df: pd.DataFrame,
    weights: list[tuple[int, float]] = WEIGHTS_3M,
) -> tuple[float | None, str]:
    """Month-ladder front of ``_score_from_kline_bars``: ``weights`` =
    (months, weight), one month = ``BARS_PER_MONTH`` rows (64 rows minimum
    for WEIGHTS_3M)."""
    return _score_from_kline_bars(df, _months_to_bars(weights))


def compute_us_rs_3m_table(
    klines: dict[str, pd.DataFrame],
    spy_kline: pd.DataFrame,
    weights_bars: list[tuple[int, float]] | None = None,
) -> pd.DataFrame:
    """Return DataFrame indexed by ticker with columns ``raw_score`` and
    ``rs_percentile`` (0-99).

    ``weights_bars`` defaults to the 3M month ladder (``WEIGHTS_3M``); pass
    e.g. ``WEIGHTS_1M_BARS`` for another window — same benchmark-relative
    score and percentile formula, only the lookbacks change.
    Tickers without enough history (< max lookback + 1 rows; 64 for 3M) are
    excluded. ``raw_score`` is retained alongside the percentile so the US
    IPO ladder can score out-of-universe candidates against the same
    Fred6725 distribution.
    """
    if weights_bars is None:
        weights_bars = _months_to_bars(WEIGHTS_3M)
    spy_score, spy_reason = _score_from_kline_bars(spy_kline, weights_bars)
    if spy_score is None:
        logger.warning(
            f"[US RS 3M] SPY score rejected ({spy_reason}) — falling back to "
            f"absolute scores (effectively un-relativised)."
        )
        spy_score = 0.0

    scores: dict[str, float] = {}
    reasons: dict[str, int] = {}
    for ticker, df in klines.items():
        s, reason = _score_from_kline_bars(df, weights_bars)
        reasons[reason] = reasons.get(reason, 0) + 1
        if s is None:
            continue
        scores[ticker] = s - spy_score

    logger.info(
        f"[US RS 3M] computed: {len(scores)}/{len(klines)} klines scored. "
        f"Reason breakdown: {dict(sorted(reasons.items(), key=lambda x: -x[1]))}"
    )

    if not scores:
        return pd.DataFrame(columns=["raw_score", "rs_percentile"])

    series = pd.Series(scores)  # column name comes from the dict key below
    pct = series.rank(method="average", pct=True) * 99
    return pd.DataFrame({
        "raw_score": series,
        "rs_percentile": pct.round().astype(int),
    })


def filter_by_rs(
    tickers: list[str],
    table: pd.DataFrame | None,
    threshold: int,
) -> list[str]:
    """Keep tickers with rs_percentile >= threshold.

    Missing-from-table → KEPT (US passthrough policy, mirrors rs_rating.py
    and hk_rs.filter_by_rs). Threshold ≤ 0 → passthrough.
    """
    if table is None or table.empty or threshold <= 0:
        return list(tickers)
    out: list[str] = []
    for t in tickers:
        if t not in table.index:
            out.append(t)
            continue
        if int(table.loc[t, "rs_percentile"]) >= threshold:
            out.append(t)
    return out


def _fetch_cloud_csv(url: str, timeout: int = 30) -> pd.DataFrame | None:
    """Fetch a 3M CSV from the cloud-published artifact.

    Returns the DataFrame indexed by ticker on success, or None on HTTP
    404 (today's file not yet published), network error, or parse failure.
    Never raises — callers walk back day-by-day on None.
    """
    try:
        req = Request(url, headers={"User-Agent": "momentum-scanner/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return pd.read_csv(io.BytesIO(body), index_col="ticker")
    except HTTPError as e:
        if e.code == 404:
            return None  # today's file may not be published yet; caller falls back
        logger.warning(f"[US RS 3M] HTTP {e.code} fetching {url}")
        return None
    except (URLError, TimeoutError) as e:
        logger.warning(f"[US RS 3M] Network error fetching {url}: {e}")
        return None
    except Exception as e:
        # pd.read_csv can raise ParserError, EmptyDataError, etc. — all
        # treated as "this URL didn't yield a usable table".
        logger.warning(f"[US RS 3M] Failed to parse {url}: {type(e).__name__}: {e}")
        return None


def cache_path(today: date, output_dir: Path) -> Path:
    return output_dir / "state" / f"rs_rating_3m_{today.isoformat()}.csv"


def save_cache(table: pd.DataFrame, today: date, output_dir: Path) -> None:
    p = cache_path(today, output_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(p, index_label="ticker")


def load_cache(today: date, output_dir: Path) -> pd.DataFrame | None:
    p = cache_path(today, output_dir)
    if not p.exists():
        return None
    try:
        return pd.read_csv(p, index_col="ticker")
    except Exception:
        return None


def _yf_download_with_retry(tickers, **kwargs):
    """Indirection layer so tests can monkeypatch this attribute.

    At runtime, lazily import the helper from main.py (avoids circular import:
    main.py imports from us_rs_3m).
    """
    from main import _yf_download_with_retry as _impl
    return _impl(tickers, **kwargs)


def _retry_sparse_in_batch(data, tickers: list[str], period: str, min_rows: int) -> bool:
    """Indirection layer so tests can monkeypatch this attribute.

    At runtime, lazily import the helper from main.py (avoids circular import:
    main.py imports from us_rs_3m). Returns True when the batch looked
    rate-limited (caller should back off longer).
    """
    from main import _retry_sparse_in_batch as _impl
    return _impl(data, tickers, period=period, min_rows=min_rows)


def _fill_missing_last_close(closes: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """Indirection layer so tests can monkeypatch this attribute.

    Lazily delegates to ``sma50_prune.fill_missing_last_close`` (it imports
    main, which imports us). Soft: a failed intraday fetch returns the input
    unchanged, so the trailing NaN row is dropped as before (one day stale).
    """
    from sma50_prune import fill_missing_last_close
    try:
        return fill_missing_last_close(closes, label="US RS 3M")
    except Exception as e:
        logger.warning(f"[US RS 3M] intraday Close fill failed, latest day left stale: {e}")
        return closes


def fetch_us_klines_yf(
    tickers: list[str],
    period: str = "6mo",
    batch_size: int = 100,
    include_ohlcv: bool = False,
    recover: bool = True,
    fill_last_close: bool = False,
) -> dict[str, pd.DataFrame]:
    """Batch-download daily closes (or full OHLCV) for US tickers via yfinance.

    Returns ``{ticker: DataFrame[time_key, close]}`` when ``include_ohlcv``
    is False (default), or ``{ticker: DataFrame[time_key, open, high, low,
    close, volume]}`` when True.

    Tickers that fail the batch retry or come back as all-NaN are silently
    dropped (callers treat them as "not in 3M table" via the kept-as-missing
    policy).

    100-ticker batches with threads=True and a 5s inter-batch pause.
    Batch size matches Fred6725/relative-strength's reliability sweet spot
    (their README: "100 is a sweet spot — speed vs reliability"); we
    previously used 500 like hk_eod, but at US scale (~6000 tickers across
    60 batches with 100, vs 12 with 500) the smaller batches reduce the
    blast radius when Yahoo throttles a single response and let curl_cffi's
    browser-impersonation breathe between calls.

    Defensive layers (kept as belt-and-braces even with curl_cffi):
    - When a batch returns fully empty after retries → 30s throttle cooldown
    - When a batch returns ≥50% sparse → also 30s cooldown (signal from
      _retry_sparse_in_batch); 5s spacing is not enough for Yahoo's window
    - ``recover`` (default on): ONE more pass, after a 30s cooldown, over the
      tickers a failed / throttled batch left with no data at all. A name
      with a short but real history (fresh IPO) is not retried. Without it a
      one-batch call (SPY benchmark, IPO candidates, the rs-line audit) had
      no second chance — 2026-10-17 the audit scored 13/91 and called the
      other 78 "insufficient history".

    ``fill_last_close`` (default off): a latest daily row that Yahoo published
    with a NaN Close is filled from that day's 1h bars
    (``sma50_prune.fill_missing_last_close``) instead of being dropped. The
    ETF RS ranking turns it on — 2026-10-22 its 10:00 run (and a rerun at
    11:42) ranked on 10-20 closes because every 10-21 Close was NaN, so all
    48 rank markers read ``=``.
    """
    if not tickers:
        return {}

    result: dict[str, pd.DataFrame] = {}
    t0 = time.monotonic()
    lost = _fetch_us_batches(tickers, period, batch_size, include_ohlcv, result, fill_last_close)
    elapsed = time.monotonic() - t0
    if recover and lost and elapsed > _RECOVERY_MAX_ELAPSED_SEC:
        logger.warning(
            f"[US RS 3M] {len(lost)} ticker(s) without data, but the first pass took "
            f"{elapsed / 60:.0f} min — skipping the recovery pass (Yahoo down, not a blip)"
        )
    elif recover and lost:
        logger.warning(
            f"[US RS 3M] {len(lost)} ticker(s) left without data by failed/throttled "
            f"batches — throttle cooldown {_RECOVERY_COOLDOWN_SEC:.0f}s, one recovery pass"
        )
        time.sleep(_RECOVERY_COOLDOWN_SEC)
        still = _fetch_us_batches(lost, period, batch_size, include_ohlcv, result, fill_last_close)
        logger.info(
            f"[US RS 3M] recovery pass: {len(lost) - len(still)}/{len(lost)} recovered"
            + (f"; still missing: {','.join(still[:20])}" if still else "")
        )
    return result


# Yahoo's throttle window is >= 30s (see main._retry_sparse_in_batch).
_RECOVERY_COOLDOWN_SEC = 30.0
# A first pass this slow means Yahoo is down, not blipping: a second pass
# would push the cloud job (timeout-minutes: 60, ~12 min normally) into the
# GH kill instead of its `if not klines: return 1`.
_RECOVERY_MAX_ELAPSED_SEC = 25 * 60.0


def _fetch_us_batches(
    tickers: list[str],
    period: str,
    batch_size: int,
    include_ohlcv: bool,
    result: dict[str, pd.DataFrame],
    fill_last_close: bool = False,
) -> list[str]:
    """One pass of ``fetch_us_klines_yf``: fill ``result`` in place and return
    the tickers that a failed or throttled batch left without any data (the
    recovery pool). Misses from a healthy batch are real (delisted / bad
    symbol) and are not returned."""
    lost: list[str] = []
    n_batches = (len(tickers) - 1) // batch_size + 1
    for bidx, start in enumerate(range(0, len(tickers), batch_size), start=1):
        batch = tickers[start:start + batch_size]
        logger.info(f"[US RS 3M] yfinance batch {bidx}/{n_batches} ({len(batch)} tickers)...")
        batch_data = _yf_download_with_retry(
            batch, period=period, progress=False, group_by="ticker", threads=True,
        )
        if batch_data is None or batch_data.empty:
            logger.warning(f"[US RS 3M]   batch failed; skipping {len(batch)} tickers")
            lost.extend(batch)
            if bidx < n_batches:
                logger.info("[US RS 3M]   throttle cooldown 30s before next batch...")
                time.sleep(30)
            continue
        # min_rows=20 mirrors hk_eod.fetch_hk_klines_yf — this is a "batch
        # quality floor" (retry tickers whose batch result is essentially
        # empty, < 20 of ~126 trading days for period="6mo"), NOT the 3M
        # algorithm's 64-row scoring minimum. min_rows=64 here was a 2026-
        # 05-21 mis-tune that flagged ~90% of tickers (including AAPL/MSFT)
        # as sparse on routine yfinance NaN noise, sending the retry phase
        # into a 5+ hour spiral. _score_from_kline enforces the real 64-row
        # cut at scoring time.
        throttled = _retry_sparse_in_batch(batch_data, batch, period=period, min_rows=20)
        # A one-ticker batch (the SPY benchmark fetch, or the tail of an
        # N*batch_size+1 universe) comes back with flat field columns —
        # _yf_download_with_retry flattens it — so there is no batch_data[t].
        flat = not isinstance(batch_data.columns, pd.MultiIndex)
        frames: dict[str, pd.DataFrame] = {}
        raw: dict[str, pd.Series] = {}
        for t in batch:
            try:
                frame = batch_data if flat and len(batch) == 1 else batch_data[t]
                series = frame["Close"]
                if not series.notna().any():
                    continue
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            frames[t], raw[t] = frame, series
        if fill_last_close and raw:
            raw = _fill_missing_last_close(raw)
        for t, series in raw.items():
            frame = frames[t]
            closes = series.dropna()
            if closes.empty:
                continue
            if include_ohlcv:
                try:
                    row = {
                        "time_key": closes.index,
                        "open": frame["Open"].reindex(closes.index).values,
                        "high": frame["High"].reindex(closes.index).values,
                        "low": frame["Low"].reindex(closes.index).values,
                        "close": closes.values,
                        "volume": frame["Volume"].reindex(closes.index).values,
                    }
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                result[t] = pd.DataFrame(row)
            else:
                result[t] = pd.DataFrame({
                    "time_key": closes.index,
                    "close": closes.values,
                })
        if throttled:
            lost.extend(t for t in batch if t not in result)
        # Cooldown selection: if the batch came back ≥50% sparse (rate-limit
        # signal from _retry_sparse_in_batch), back off the full 30s instead
        # of the normal 5s — Yahoo's throttle window is ≥30s and 5s spacing
        # just cascades sparse-batches one after another (2026-05-21 20:55:
        # batches 3-10 all 371-499 sparse with only 5s between).
        if bidx < n_batches:
            if throttled:
                logger.info("[US RS 3M]   throttle cooldown 30s (high-sparse batch)...")
                time.sleep(30)
            else:
                time.sleep(5)
    return lost


def fetch_universe_from_rs_csv(rs_table_12m: dict[str, int] | None) -> list[str]:
    """Return the ticker list from the Fred6725 12M table.

    We piggyback on the existing 12M fetch (already done at top of EOD)
    instead of re-downloading the CSV. ``rs_table_12m`` is the dict
    returned by ``rs_rating.fetch_rs_table``.
    """
    if not rs_table_12m:
        return []
    return sorted(rs_table_12m.keys())


def _fetch_spy_kline(period: str = "6mo") -> pd.DataFrame | None:
    """Fetch SPY closes via the same retrying yfinance helper as the universe.
    Returns a DataFrame with time_key + close, or None on failure.
    """
    klines = fetch_us_klines_yf(["SPY"], period=period, batch_size=1)
    return klines.get("SPY")


def build_3m_table(
    output_dir: Path,
    today: date,
    rs_table_12m: dict[str, int] | None = None,
) -> pd.DataFrame | None:
    """Load today's US 3M RS table from the cloud-published CSV.

    Strategy:
      1. Local cache hit (same-day rerun) → return immediately.
      2. HTTP fetch today's cloud CSV; walk back up to _FALLBACK_MAX_AGE_DAYS
         if not yet published.
      3. All fail → return None (callers degrade via filter_by_rs's
         missing→passthrough policy).

    The local compute path was removed in 2026-05-21 because home-IP
    runs were getting throttled mid-loop; compute now happens in
    .github/workflows/update_us_rs_3m.yml on ephemeral GH runners.

    ``rs_table_12m`` is retained for signature compatibility with the
    pre-cloud call sites but is no longer consulted.
    """
    del rs_table_12m  # unused; signature kept for back-compat

    cached = load_cache(today, output_dir)
    if cached is not None and not cached.empty:
        logger.info(f"[US RS 3M] Using cached table: {len(cached)} tickers")
        return cached

    for delta in range(_FALLBACK_MAX_AGE_DAYS + 1):
        target_date = today - timedelta(days=delta)
        url = _CLOUD_CSV_URL_TEMPLATE.format(date=target_date.isoformat())
        table = _fetch_cloud_csv(url)
        if table is None or table.empty:
            continue
        if delta == 0:
            logger.info(f"[US RS 3M] Fetched cloud CSV: {len(table)} tickers")
            save_cache(table, today, output_dir)
        elif delta == 1 and today.weekday() == 5:
            # Saturday local run (US-EOD launchd fires Tue-Sat) always walks
            # back 1 day because the cloud cron is Mon-Fri (no Sat run).
            # Expected, not stale — keep it at INFO so the Sat log doesn't
            # look like a degraded mode.
            logger.info(
                f"[US RS 3M] Fetched cloud CSV from {target_date.isoformat()}: "
                f"{len(table)} tickers (Saturday: cloud cron runs Mon-Fri)"
            )
        else:
            logger.warning(
                f"[US RS 3M] Cloud CSV for {today.isoformat()} not available; "
                f"using stale fallback from {target_date.isoformat()} ({delta} day(s) old)"
            )
        return table

    logger.warning(
        f"[US RS 3M] No cloud CSV within {_FALLBACK_MAX_AGE_DAYS} days; "
        "3M layer will passthrough "
        "(check https://github.com/EricXue92/momentum-scanner/actions)"
    )
    return None
