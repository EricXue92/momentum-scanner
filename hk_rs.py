"""Local IBD-style Relative Strength percentile for the HK universe.

Mirrors rs_rating.py's contract but computes percentiles in-process from
Futu k-line data rather than reading the Fred6725 CSV (which only covers
US tickers). HSI is the benchmark.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

logger = logging.getLogger(__name__)


# Weight tuples: (months, weight). months 决定回看偏移 (months * 21 个交易日)；
# weight 按 IBD 风格压在最短窗口上，重叠窗口产生隐式时间衰减。
WEIGHTS_12M: list[tuple[int, float]] = [(3, 0.4), (6, 0.2), (9, 0.2), (12, 0.2)]
WEIGHTS_3M:  list[tuple[int, float]] = [(1, 0.4), (2, 0.3), (3, 0.3)]

# Cloud-published combined CSV (written by .github/workflows/update_hk_rs.yml).
# Local pipeline reads from raw.githubusercontent.com instead of computing
# locally — the home-IP yfinance fetch of the ~2,400 HK universe gets
# throttled (only ~50% coverage on 2026-05-25), warping the percentile
# distribution. Schema: index ``code`` (Futu format), columns
# ``rs_percentile_12m`` + ``rs_percentile_3m``.
_CLOUD_CSV_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/EricXue92/momentum-scanner/main/"
    "data/hk_rs/{date}.csv"
)

# Walk back up to N days if today's CSV isn't published yet. Mirrors
# us_rs_3m._FALLBACK_MAX_AGE_DAYS — HK EOD runs Mon-Fri, so a Mon-failure
# + Tue-failure gap is at worst 2 days; 3 gives margin.
_FALLBACK_MAX_AGE_DAYS = 3


def _score_from_kline(
    df: pd.DataFrame,
    weights: list[tuple[int, float]] = WEIGHTS_12M,
) -> tuple[float | None, str]:
    """Compute Σ wᵢ·Rᵢ from a k-line DataFrame sorted ascending by ``time_key``.
    Returns ``(score, reason)`` — score is None on rejection. Reason is one of:
      ``ok``, ``no_data``, ``short_history`` (insufficient rows for max lookback),
      ``zero_last``, ``zero_past``.

    Minimum rows = ``max(months for months, _ in weights) * 21 + 1``.
    """
    if df is None or df.empty:
        return None, "no_data"
    max_months = max(m for m, _ in weights)
    min_rows = max_months * 21 + 1
    if len(df) < min_rows:
        return None, "short_history"
    closes = df["close"].astype(float).values
    last = closes[-1]
    if last <= 0:
        return None, "zero_last"

    score = 0.0
    for months, w in weights:
        idx = -1 - months * 21
        if abs(idx) > len(closes):
            return None, "short_history"
        past = closes[idx]
        if past <= 0:
            return None, "zero_past"
        score += w * ((last / past) - 1.0)
    return score, "ok"


def compute_rs_table(
    klines: dict[str, pd.DataFrame],
    hsi_kline: pd.DataFrame,
    weights: list[tuple[int, float]] = WEIGHTS_12M,
    label: str = "12M",
) -> pd.DataFrame:
    """Return DataFrame indexed by Futu code with column ``rs_percentile``
    (0-99). Tickers without enough history are excluded.

    ``weights`` selects the weight tuple (WEIGHTS_12M or WEIGHTS_3M). ``label``
    is purely cosmetic — it's spliced into the rejection-breakdown log line so
    a single run computing both 12M and 3M tables produces distinguishable
    output.

    Logs a per-reason rejection breakdown so the operator can tell whether
    a small RS table is due to (a) Futu/yfinance not returning enough k-line
    history for less liquid HK names (``short_history``) versus (b) data
    hygiene issues (``zero_last`` / ``zero_past``).
    """
    hsi_score, hsi_reason = _score_from_kline(hsi_kline, weights=weights)
    if hsi_score is None:
        logger.warning(
            f"[HK RS {label}] HSI score rejected ({hsi_reason}) — falling back to "
            f"absolute scores (effectively un-relativised)."
        )
        hsi_score = 0.0

    scores: dict[str, float] = {}
    reasons: dict[str, int] = {}
    for code, df in klines.items():
        s, reason = _score_from_kline(df, weights=weights)
        reasons[reason] = reasons.get(reason, 0) + 1
        if s is None:
            continue
        scores[code] = s - hsi_score

    logger.info(
        f"[HK RS {label}] computed: {len(scores)}/{len(klines)} klines scored. "
        f"Reason breakdown: {dict(sorted(reasons.items(), key=lambda x: -x[1]))}"
    )

    if not scores:
        return pd.DataFrame(columns=["rs_percentile"])

    series = pd.Series(scores, name="relative_score")
    pct = series.rank(method="average", pct=True) * 99
    return pd.DataFrame({"rs_percentile": pct.round().astype(int)})


def filter_by_rs(
    tickers: list[str],
    table: pd.DataFrame | None,
    threshold: int,
) -> list[str]:
    """Keep tickers with rs_percentile >= threshold. Tickers missing from
    ``table`` are KEPT (passthrough — same policy as rs_rating.py for IPOs
    and recent listings without 12mo history). If ``table`` is None or
    empty, all tickers are returned unchanged (failure passthrough)."""
    if table is None or table.empty or threshold <= 0:
        return list(tickers)
    out = []
    for t in tickers:
        if t not in table.index:
            out.append(t)
            continue
        if int(table.loc[t, "rs_percentile"]) >= threshold:
            out.append(t)
    return out


def cache_path(today: date, output_dir: Path, suffix: str = "") -> Path:
    """Default suffix '' → hk_rs_rating_<date>.csv (12M, legacy path).
    suffix='3m' → hk_rs_rating_3m_<date>.csv."""
    prefix = f"{suffix}_" if suffix else ""
    return output_dir / "state" / f"hk_rs_rating_{prefix}{today.isoformat()}.csv"


def save_cache(
    table: pd.DataFrame, today: date, output_dir: Path, suffix: str = ""
) -> None:
    p = cache_path(today, output_dir, suffix=suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(p, index_label="code")


def load_cache(
    today: date, output_dir: Path, suffix: str = ""
) -> pd.DataFrame | None:
    p = cache_path(today, output_dir, suffix=suffix)
    if not p.exists():
        return None
    try:
        return pd.read_csv(p, index_col="code")
    except Exception:
        return None


def _fetch_cloud_csv(url: str, timeout: int = 30) -> pd.DataFrame | None:
    """Fetch the combined HK RS CSV from the cloud-published artifact.

    Returns the DataFrame indexed by ``code`` on success, or None on HTTP
    404 (today's file not yet published), network error, or parse failure.
    Never raises — callers walk back day-by-day on None.
    """
    try:
        req = Request(url, headers={"User-Agent": "momentum-scanner/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return pd.read_csv(io.BytesIO(body), index_col="code")
    except HTTPError as e:
        if e.code == 404:
            return None  # today's file may not be published yet; caller falls back
        logger.warning(f"[HK RS] HTTP {e.code} fetching {url}")
        return None
    except (URLError, TimeoutError) as e:
        logger.warning(f"[HK RS] Network error fetching {url}: {e}")
        return None
    except Exception as e:
        # pd.read_csv can raise ParserError, EmptyDataError, etc. — all
        # treated as "this URL didn't yield a usable table".
        logger.warning(f"[HK RS] Failed to parse {url}: {type(e).__name__}: {e}")
        return None


def _split_combined(combined: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the combined cloud CSV into the two single-column ``rs_percentile``
    frames the gate / IPO ladder expect. NaN rows are dropped per column, so a
    ticker scored for 3M but lacking 12-month history appears in the 3M frame
    only — matching the in-process compute, where each table independently
    excludes short-history names."""
    def _one(col: str) -> pd.DataFrame:
        if col not in combined.columns:
            return pd.DataFrame(columns=["rs_percentile"])
        s = combined[col].dropna()
        return pd.DataFrame({"rs_percentile": s.astype(int)})

    return _one("rs_percentile_12m"), _one("rs_percentile_3m")


_RS_LINE_COLS = ["rs_below_ma", "rs_days_below_ma", "rs_frac_below_ma"]


def _extract_rs_line(combined: pd.DataFrame) -> pd.DataFrame:
    """Pull the RS-line columns out of the combined cloud CSV (if present).
    Rows where rs_below_ma is NaN (short history / pre-feature CSVs) are
    dropped, mirroring the per-column NaN handling in _split_combined."""
    present = [c for c in _RS_LINE_COLS if c in combined.columns]
    if "rs_below_ma" not in present:
        return pd.DataFrame(columns=_RS_LINE_COLS)
    out = combined[present].dropna(subset=["rs_below_ma"]).copy()
    out["rs_below_ma"] = out["rs_below_ma"].astype(int)
    if "rs_days_below_ma" in out.columns:
        out["rs_days_below_ma"] = out["rs_days_below_ma"].astype(int)
    return out


def build_hk_rs_tables(
    output_dir: Path,
    today: date,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
    """Load today's HK RS tables (12M + 3M + RS-line) from the cloud-published CSV.

    Returns ``(table_12m, table_3m, rs_line_tbl)``; ``table_12m`` and
    ``table_3m`` are each indexed by Futu code with a single
    ``rs_percentile`` column (the shape ``filter_by_rs`` and the HK IPO
    ladder expect). ``rs_line_tbl`` carries the RS-line trend columns
    (``rs_below_ma``, ``rs_days_below_ma``, ``rs_frac_below_ma``) indexed by
    Futu code — None when the data are unavailable (cache-hit, stale
    fallback that pre-dates the feature, or all fetches failed).

    All three are None only when no usable CSV exists within
    ``_FALLBACK_MAX_AGE_DAYS`` — callers then degrade via filter_by_rs's
    missing→passthrough policy.

    Strategy mirrors ``us_rs_3m.build_3m_table``:
      1. Local cache hit (both 12M + 3M present for today) → return immediately
         (rs_line_tbl is None because local caches don't carry those columns).
      2. HTTP fetch today's combined cloud CSV; walk back up to
         _FALLBACK_MAX_AGE_DAYS if not yet published.
      3. All fail → (None, None, None).

    Compute moved off-host (.github/workflows/update_hk_rs.yml) because the
    home-IP yfinance fetch of the ~2,400 HK universe gets throttled, leaving
    the percentile distribution computed over only ~half the universe.
    """
    cached_12m = load_cache(today, output_dir)
    cached_3m = load_cache(today, output_dir, suffix="3m")
    if cached_12m is not None and cached_3m is not None:
        logger.info(
            f"[HK RS] Using cached tables: 12M {len(cached_12m)}, "
            f"3M {len(cached_3m)} tickers"
        )
        return cached_12m, cached_3m, None

    for delta in range(_FALLBACK_MAX_AGE_DAYS + 1):
        target_date = today - timedelta(days=delta)
        url = _CLOUD_CSV_URL_TEMPLATE.format(date=target_date.isoformat())
        combined = _fetch_cloud_csv(url)
        if combined is None or combined.empty:
            continue
        table_12m, table_3m = _split_combined(combined)
        if delta == 0:
            logger.info(
                f"[HK RS] Fetched cloud CSV: 12M {len(table_12m)}, "
                f"3M {len(table_3m)} tickers"
            )
            # Mirror today's pull to the local caches so a same-day rerun
            # short-circuits. Stale fallbacks are NOT cached (would mask
            # staleness on the next rerun).
            save_cache(table_12m, today, output_dir)
            save_cache(table_3m, today, output_dir, suffix="3m")
        else:
            logger.warning(
                f"[HK RS] Cloud CSV for {today.isoformat()} not available; "
                f"using stale fallback from {target_date.isoformat()} "
                f"({delta} day(s) old)"
            )
        return table_12m, table_3m, _extract_rs_line(combined)

    logger.warning(
        f"[HK RS] No cloud CSV within {_FALLBACK_MAX_AGE_DAYS} days; RS gate "
        "will passthrough "
        "(check https://github.com/EricXue92/momentum-scanner/actions)"
    )
    return None, None, None
