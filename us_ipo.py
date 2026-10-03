"""US IPO candidate filter — mirrors HK's filter_hk_ipo_candidates.

IPO candidates = tickers that passed a Finviz long-side screener but were
dropped by yfinance for missing/insufficient daily history. They go through
a conditional ladder based on data depth so a 30-day-old IPO can still surface
while a 200-day-old one is held to nearly the full long-side baseline.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _build_ipo_metrics(
    klines: dict[str, pd.DataFrame],
    finviz_caps: dict[str, float],
) -> pd.DataFrame:
    """Compute IPO-ladder metrics frame from k-lines.

    Columns: market_cap, last_price, avg_vol_20d, avg_dollar_vol_20d,
    adr_pct, sma50, sma200, above_sma50, above_sma200, perf_4w, n_rows.

    Tickers shorter than the relevant window get NaN for that window's
    metrics (matching hk_eod.build_metrics_frame semantics). Caller checks
    NaN via pd.notna in the ladder.
    """
    rows: list[dict] = []
    for t, df in klines.items():
        if df is None or df.empty or len(df) < 2:
            continue
        closes = df["close"].astype(float).values
        highs = df["high"].astype(float).values if "high" in df.columns else closes
        lows = df["low"].astype(float).values if "low" in df.columns else closes
        volumes = (
            df["volume"].astype(float).values if "volume" in df.columns
            else np.full(len(df), float("nan"))
        )
        n = len(closes)
        last = float(closes[-1])

        avg_vol_20 = float(volumes[-20:].mean()) if n >= 20 else float("nan")
        avg_dv_20 = last * avg_vol_20 if n >= 20 else float("nan")
        if n >= 20:
            adr = float(((highs[-20:] - lows[-20:]) / closes[-20:]).mean()) * 100
        else:
            adr = float("nan")
        sma50 = float(closes[-50:].mean()) if n >= 50 else float("nan")
        sma200 = float(closes[-200:].mean()) if n >= 200 else float("nan")
        above_sma50 = bool(n >= 50 and last > sma50)
        above_sma200 = bool(n >= 200 and last > sma200)
        # 20 sessions back, same window as hk_eod.build_metrics_frame's _perf(20)
        past = float(closes[-21]) if n > 20 else float("nan")
        perf_4w = (last - past) / past * 100.0 if past > 0 else float("nan")

        rows.append({
            "ticker": t,
            "market_cap": finviz_caps.get(t, float("nan")),
            "last_price": last,
            "avg_vol_20d": avg_vol_20,
            "avg_dollar_vol_20d": avg_dv_20,
            "adr_pct": adr,
            "sma50": sma50,
            "sma200": sma200,
            "above_sma50": above_sma50,
            "above_sma200": above_sma200,
            "perf_4w": perf_4w,
            "n_rows": n,
        })

    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).set_index("ticker")
    # Use object dtype to preserve Python bool identity (is False / is True tests).
    for col in ("above_sma50", "above_sma200"):
        if col in result.columns:
            result[col] = result[col].map(bool).astype(object)
    return result


def filter_us_ipo_candidates(
    klines: dict[str, pd.DataFrame],
    finviz_caps: dict[str, float],
    rs_table_3m_full: pd.DataFrame | None,
    spy_kline: pd.DataFrame | None,
    settings: dict,
) -> tuple[list[str], dict[str, int]]:
    """Filter US IPO candidates through the depth-conditional ladder.

    Drop buckets (first hit wins, mirrors HK filter_hk_ipo_candidates):
      - len(df) < 20                                   → drops['min_history']
      - cap < min_market_cap                           → drops['cap']
      - price < min_price                              → drops['price']
      - if n >= 20:
          avg_vol_20d < min_avg_volume                 → drops['avg_vol']
          avg_dollar_vol_20d < min_dollar_volume       → drops['dvol']
          adr_pct < min_adr_percent                    → drops['adr']
      - if n >= 50: not above SMA50                    → drops['sma50']
      - if n >= 200: not above SMA200                  → drops['sma200']
      - if n < 64 and ipo_min_perf_4w is set:          (direction gate)
          not perf_4w > ipo_min_perf_4w                → drops['perf_4w']
          (NaN drops too: 20 rows can't yield a 4-week return)
      - if n >= 64 and rs_table_3m_full and threshold>0:
          compute IPO 3M score vs SPY; rank against
          rs_table_3m_full['raw_score'] distribution;
          pct < min_rs_percentile_3m                   → drops['rs_3m']
    """
    min_cap = float(settings.get("min_market_cap", 300_000_000))
    min_price = float(settings.get("min_price", 10.0))
    min_avg_vol = float(settings.get("min_avg_volume", 1_000_000))
    min_dvol = float(settings.get("min_dollar_volume", 100_000_000))
    min_adr = float(settings.get("min_adr_percent", 4.0))
    rs_threshold = int(settings.get("min_rs_percentile_3m", 0))
    min_perf_4w = settings.get("ipo_min_perf_4w")  # unset = direction gate off

    drops: dict[str, int] = {
        "min_history": 0,
        "cap": 0, "price": 0,
        "avg_vol": 0, "dvol": 0, "adr": 0,
        "sma50": 0, "sma200": 0,
        "perf_4w": 0,
        "rs_3m": 0,
    }

    # Hoist the SPY score + sorted distribution above the loop — both are
    # invariant across tickers, and re-running _score_from_kline / sort_values
    # per-IPO is wasted work.
    rs_gate_ready = (
        rs_threshold > 0
        and rs_table_3m_full is not None
        and not rs_table_3m_full.empty
        and spy_kline is not None
    )
    if rs_gate_ready:
        from us_rs_3m import _score_from_kline
        _spy_score, _ = _score_from_kline(spy_kline)
        _spy_score = _spy_score if _spy_score is not None else 0.0
        _rs_distribution = (
            rs_table_3m_full["raw_score"].astype(float).sort_values().values
        )

    metrics = _build_ipo_metrics(klines, finviz_caps)
    if metrics.empty:
        # All k-lines were empty/None or len<2 — count as min_history drops
        drops["min_history"] = len(klines)
        return [], drops

    kept: list[str] = []
    for t, df in klines.items():
        if df is None or df.empty or len(df) < 20:
            drops["min_history"] += 1
            continue
        if t not in metrics.index:
            drops["min_history"] += 1
            continue
        row = metrics.loc[t]
        cap = row["market_cap"]
        if not (pd.notna(cap) and cap >= min_cap):
            drops["cap"] += 1
            continue
        price = row["last_price"]
        if not (pd.notna(price) and price >= min_price):
            drops["price"] += 1
            continue
        # >= 20 day metrics — guaranteed non-NaN since len(df) >= 20
        if pd.notna(row["avg_vol_20d"]) and row["avg_vol_20d"] < min_avg_vol:
            drops["avg_vol"] += 1
            continue
        if pd.notna(row["avg_dollar_vol_20d"]) and row["avg_dollar_vol_20d"] < min_dvol:
            drops["dvol"] += 1
            continue
        if pd.notna(row["adr_pct"]) and row["adr_pct"] < min_adr:
            drops["adr"] += 1
            continue
        if pd.notna(row["sma50"]) and not bool(row["above_sma50"]):
            drops["sma50"] += 1
            continue
        if pd.notna(row["sma200"]) and not bool(row["above_sma200"]):
            drops["sma200"] += 1
            continue
        # Direction gate — below 64 rows there is no 3M RS (and mostly no
        # SMA50 either), so nothing else in the ladder looks at strength.
        # NaN (exactly 20 rows) compares False → drop; the ticker isn't in
        # the master yet, so it can still surface on a later scan.
        if (
            len(df) < 64
            and min_perf_4w is not None
            and not row["perf_4w"] > min_perf_4w
        ):
            drops["perf_4w"] += 1
            continue
        # 3M RS gate — only fires for n >= 64 with a usable table.
        if rs_gate_ready and len(df) >= 64:
            ipo_score, _ = _score_from_kline(df)
            if ipo_score is None:
                # len(df) >= 64 but _score_from_kline returned None (zero_last
                # or zero_past data-hygiene reject on the IPO k-line). Drop
                # into the same rs_3m bucket as a low-percentile score.
                drops["rs_3m"] += 1
                continue
            relative_score = ipo_score - _spy_score
            # searchsorted("right") gives the count of distribution <= score.
            rank = np.searchsorted(_rs_distribution, relative_score, side="right")
            pct = int(round(rank / max(len(_rs_distribution), 1) * 99))
            if pct < rs_threshold:
                drops["rs_3m"] += 1
                continue
        kept.append(t)

    return kept, drops
