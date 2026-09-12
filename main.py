#!/usr/bin/env python3
"""Finviz screener to TradingView watchlist generator."""

import argparse
import logging
import os
import re
import shutil
import sys
import time
import tomllib
from datetime import date, datetime, timedelta
import io
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

os.environ.setdefault("DISABLE_TQDM", "1")

import yfinance as yf
from finviz import get_stock
from finviz.helper_functions.error_handling import NoResults
from finviz.screener import Screener
import finviz_timeout  # noqa: F401  — patches finviz sync HTTP (timeout + bounded retries)
import openpyxl

from futu_sync import (
    _opend_reachable,
    discover_hk_morning_gap_candidates,
    discover_morning_gap_candidates,
    get_market_caps_futu,
    intraday_cumulative_volume_futu,
    pre_market_gap_futu,
    sync_to_futu,
)
from tv_sync import sync_to_tv
from cleanup import cleanup_old_outputs
from hk_eod import fetch_hkex_equities, filter_hk_shorts, run_hk_eod
from notify import notify_morning_gap
from rs_rating import fetch_rs_table, filter_by_rs

logger = logging.getLogger(__name__)


def _log_section(title: str) -> None:
    """Visual separator + section header for log readability."""
    bar = "─" * 80
    logger.info("")
    logger.info(bar)
    logger.info(title)
    logger.info(bar)


def _futu_sync(config: dict, key: str, tickers: list[str], market: str) -> None:
    """Sync to Futu if [futu] is enabled in config — silent no-op otherwise.
    Empty `tickers` is also a no-op: today's screen produced nothing, so we
    leave the existing Futu group alone instead of clearing it."""
    if not tickers:
        return
    futu_cfg = config.get("futu") or {}
    if not futu_cfg.get("enabled", False):
        return
    group_name = (futu_cfg.get("groups") or {}).get(key)
    if not group_name:
        return
    append_only_groups = set(futu_cfg.get("append_only_groups") or [])
    sync_to_futu(
        tickers,
        group_name,
        market,  # type: ignore[arg-type]
        host=futu_cfg.get("host", "127.0.0.1"),
        port=futu_cfg.get("port", 11111),
        append_only=group_name in append_only_groups,
    )


def _tv_sync(config: dict, key: str, tickers: list[str], market: str) -> None:
    """Sync to TradingView if [tv_sync] is enabled — silent no-op otherwise.
    Same empty-list contract as _futu_sync (don't clobber the existing list).
    Independent from Futu sync: TV failure does not affect Futu and vice
    versa; both are soft side effects."""
    if not tickers:
        return
    tv_cfg = config.get("tv_sync") or {}
    if not tv_cfg.get("enabled", False):
        return
    list_name = (tv_cfg.get("lists") or {}).get(key)
    if not list_name:
        return
    append_only_lists = set(tv_cfg.get("append_only_lists") or [])
    sync_to_tv(
        tickers,
        list_name,
        market,  # type: ignore[arg-type]
        append_only=list_name in append_only_lists,
    )


def _morning_gap_seen_path(
    output_dir: Path, today: str, phase: str, market: str = "US"
) -> Path:
    """Per-phase seen file. `phase` is 'pre' or 'post'. Auto-resets daily.
    HK morning-gap (post-open only) uses a separate `hk_morning_gap_seen_*`
    prefix to keep US and HK state independent."""
    prefix = "hk_morning_gap_seen" if market == "HK" else "morning_gap_seen"
    return output_dir / "state" / f"{prefix}_{phase}_{today}.txt"


def _read_seen_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError as e:
        logger.warning(f"[Morning Gap] could not read {path}: {e}")
        return set()


def _write_seen_set(path: Path, seen: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for t in sorted(seen):
                f.write(f"{t}\n")
    except OSError as e:
        logger.warning(f"[Morning Gap] could not write {path}: {e}")


def _morning_gap_classify(
    today: str,
    tickers: list[str],
    output_dir: Path,
    is_pre: bool,
    market: str = "US",
) -> tuple[list[str], list[str]]:
    """Categorize tickers vs phase-specific seen sets and update seen file.

    Pre-market scans use `morning_gap_seen_pre_<today>.txt`; post-open scans
    use `morning_gap_seen_post_<today>.txt`. Both auto-reset daily.

    Returns (fresh, promoted):
      - Pre-market: fresh = tickers not yet pushed in any earlier pre-market
        scan today; promoted = [].
      - Post-open: fresh = tickers brand-new today (not in pre OR post seen);
        promoted = tickers that were in pre seen but appear in post for the
        first time today (volume gate just confirmed a pre-market gapper).
        For HK there is no pre phase (Futu does not expose HK pre-auction
        fields), so promoted is always empty.

    Side effect: appends `tickers` to the matching phase's seen file.
    """
    pre_path = _morning_gap_seen_path(output_dir, today, "pre", market)
    post_path = _morning_gap_seen_path(output_dir, today, "post", market)
    pre_seen = _read_seen_set(pre_path)
    current = set(tickers)

    if is_pre:
        fresh = sorted(current - pre_seen)
        _write_seen_set(pre_path, pre_seen | current)
        return fresh, []

    post_seen = _read_seen_set(post_path)
    new_post = current - post_seen
    promoted = sorted(new_post & pre_seen)
    fresh = sorted(new_post - pre_seen)
    _write_seen_set(post_path, post_seen | current)
    return fresh, promoted


def _fetch_snapshot_rows(
    tickers: list[str], *, futu_cfg: dict
) -> dict[str, dict[str, float | str | None]]:
    """One small `get_market_snapshot` call against just `tickers`. Returns
    `{ticker: {name, gap_pct, last_price, market_cap}}`. Soft-fail to {}
    on any error — the subprocess will still run with null fields."""
    rows: dict[str, dict[str, float | str | None]] = {}
    if not tickers:
        return rows
    try:
        from futu import OpenQuoteContext, RET_OK
    except ImportError:
        return rows
    host = futu_cfg.get("host", "127.0.0.1")
    port = futu_cfg.get("port", 11111)
    if not _opend_reachable(host, port):
        return rows
    ctx = None
    try:
        ctx = OpenQuoteContext(host=host, port=port)
        codes = [f"US.{t}" for t in tickers]
        ret, snap = ctx.get_market_snapshot(codes)
        if ret != RET_OK or snap is None:
            return rows
        for _, r in snap.iterrows():
            code = r.get("code", "")
            if not code.startswith("US."):
                continue
            t = code[3:]
            try:
                last = float(r.get("last_price", 0) or 0)
                prev = float(r.get("prev_close_price", 0) or 0)
                pre_p = r.get("pre_price")
                pre_chg = r.get("pre_change_rate")

                # Prefer pre_price during pre-market window; fall back to last trade.
                try:
                    display_price = float(pre_p) if pre_p not in (None, "N/A") else (last or None)
                except (TypeError, ValueError):
                    display_price = last or None

                # Prefer pre_change_rate; fall back to derived from last_price.
                try:
                    gap = float(pre_chg) if pre_chg not in (None, "N/A") else None
                except (TypeError, ValueError):
                    gap = None
                if gap is None and prev:
                    gap = (last - prev) / prev * 100

                rows[t] = {
                    "name": r.get("stock_name") or None,
                    "gap_pct": gap,
                    "last_price": display_price,  # display, not strictly "last"
                    "market_cap": float(r.get("total_market_val", 0) or 0) or None,
                }
            except (TypeError, ValueError):
                continue
    except Exception as e:
        logger.warning(f"[Morning Gap] snapshot fetch for catalyst report failed: {e}")
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
    return rows


def _spawn_catalyst_report(
    *,
    fresh: list[str],
    snapshot_rows: dict[str, dict[str, float | str | None]],
    today_iso: str,
    offset: int,
    config: dict,
) -> None:
    """Spawn the catalyst report subprocess (detached). Soft-fails — any
    exception logged + swallowed. Caller is responsible for `fresh`-only
    invariant (we don't re-filter here)."""
    cfg = config.get("morning_gap_catalyst") or {}
    if not cfg.get("enabled", True):
        return
    if not fresh:
        return
    try:
        import json
        import subprocess
        import tempfile

        entries = []
        for t in fresh:
            row = snapshot_rows.get(t, {})
            entries.append({
                "ticker": t,
                "company_name": row.get("name"),
                "gap_pct": row.get("gap_pct"),
                "last_price": row.get("last_price"),
                "market_cap": row.get("market_cap"),
                "first_seen_offset_minutes": offset,
            })
        fd, tmpname = tempfile.mkstemp(
            prefix="morning_snap_", suffix=".json", text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False)
        logger.info(
            f"[Morning Gap] spawning catalyst report subprocess for "
            f"{len(fresh)} fresh tickers (snapshot at {tmpname})"
        )
        uv_path = shutil.which("uv") or "/Users/xue/.local/bin/uv"
        subprocess.Popen(
            [
                uv_path, "run", "python", "-m", "report.morning",
                "--snapshot", tmpname,
                "--date", today_iso,
                "--offset", str(offset),
            ],
            cwd=str(Path(__file__).resolve().parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.warning(f"[Morning Gap] catalyst report spawn failed: {e}")


def _eod_seen_path(output_dir: Path, market: str) -> Path:
    """Path to the per-market cross-day master 'seen' file.
    Append-only across runs; reset by deleting the file manually."""
    return output_dir / "state" / f"eod_seen_{market}.txt"


def _load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError as e:
        logger.warning(f"[EOD seen] could not read {path}: {e}")
        return set()


def _persist_seen(path: Path, seen: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for t in sorted(seen):
                f.write(f"{t}\n")
    except OSError as e:
        logger.warning(f"[EOD seen] could not write {path}: {e}")


def _dedup_seen(
    label: str,
    sorted_tickers: list[str],
    seen: set[str],
    seen_path: Path,
) -> list[str]:
    """Filter out tickers already in `seen`, persist the union, return the
    new (unseen) subset. Mutates `seen` in place. Logs how many were dropped.
    Sorted-input → sorted-output."""
    if not sorted_tickers:
        logger.info(f"{label} 0 candidates after upstream filters — output will be empty")
        return []
    new = [t for t in sorted_tickers if t not in seen]
    skipped = len(sorted_tickers) - len(new)
    if skipped and not new:
        logger.warning(
            f"{label} cross-day dedup: ALL {skipped} candidate(s) already in master "
            f"— output will be empty (reset by deleting {seen_path.name})"
        )
    elif skipped:
        logger.info(
            f"{label} cross-day dedup: {skipped} already in master, kept {len(new)} new"
        )
    if new:
        seen.update(new)
        _persist_seen(seen_path, seen)
    return new


def _repeat_hits(label: str, sorted_tickers: list[str], seen: set[str]) -> list[str]:
    """The subset of `sorted_tickers` already in the cross-day master `seen`.

    These are the tickers `_dedup_seen` is about to drop. Call it BEFORE
    `_dedup_seen` (which mutates `seen`). Read-only: never touches the master
    or its file — a repeat hit is already recorded there. Sorted-input →
    sorted-output."""
    hits = [t for t in sorted_tickers if t in seen]
    if hits:
        logger.info(f"{label} {len(hits)} repeat: {', '.join(hits)}")
    return hits



def _yf_download_with_retry(tickers, max_retries=3, **kwargs):
    """Download yfinance data with retries on failure."""
    for attempt in range(max_retries):
        data = yf.download(tickers, **kwargs)
        if data is not None and not data.empty:
            return data
        if attempt < max_retries - 1:
            time.sleep(3)
            logger.warning(f"  yfinance download returned empty, retrying ({attempt + 2}/{max_retries})...")
    return data


def _get_market_cap(ticker: str, max_retries: int = 3) -> float | None:
    """Get market cap with retries."""
    for attempt in range(max_retries):
        try:
            cap = yf.Ticker(ticker).fast_info.market_cap
            if cap:
                return cap
        except Exception:
            pass
        if attempt < max_retries - 1:
            time.sleep(1)
    return None


def _get_closes_volumes(data, ticker: str, single: bool):
    """Extract closes and volumes series for a ticker from downloaded data."""
    if single:
        return data["Close"].dropna(), data["Volume"].dropna()
    return data[ticker]["Close"].dropna(), data[ticker]["Volume"].dropna()


def _get_ohlc(data, ticker: str, single: bool):
    """Extract high/low/close series for a ticker from downloaded data."""
    if single:
        return data["High"].dropna(), data["Low"].dropna(), data["Close"].dropna()
    return (
        data[ticker]["High"].dropna(),
        data[ticker]["Low"].dropna(),
        data[ticker]["Close"].dropna(),
    )


def _trim_today(series, market_open: bool, today_date):
    """Remove today's incomplete data if market is open."""
    if market_open and len(series) > 0 and series.index[-1].date() == today_date:
        return series.iloc[:-1]
    return series


def _retry_sparse_in_batch(data, tickers: list[str], period: str, min_rows: int) -> bool:
    """Patch tickers that came back sparse from a batch yf.download by re-downloading
    each one individually and writing the result back into the batch DataFrame.
    yfinance batch responses occasionally drop legitimate tickers (all-NaN columns)
    due to transient Yahoo errors; the single-ticker retry usually recovers them.
    No-op for single-ticker batches (column shape is different).

    Returns True when the batch looks rate-limited (sparse > 50% of tickers,
    individual retry skipped). The caller can use this signal to back off
    longer between batches; existing callers that ignore the return value
    keep working unchanged.
    """
    if len(tickers) <= 1:
        return False

    sparse = []
    for t in tickers:
        try:
            vol = data[t]["Volume"].dropna()
        except (KeyError, TypeError):
            sparse.append(t)
            continue
        if len(vol) < min_rows:
            sparse.append(t)

    if not sparse:
        return False

    # Heuristic: when more than half of the batch comes back sparse, this
    # is almost certainly a Yahoo rate-limit signal (not the usual ~20%
    # NaN noise from individual ticker hiccups). Individual retries will
    # all fail too while throttled AND they hammer Yahoo with hundreds of
    # back-to-back single-ticker calls, making the throttle worse. Skip
    # the retry and tell the caller to back off; the caller's inter-batch
    # sleep alone is not enough — Yahoo's throttle window is typically
    # ≥ 30s. (2026-05-21: batch 4 of US RS 3M had 499/500 sparse and the
    # retry loop flushed 499 silent failures in ~1s; batches 3-10 then
    # cascaded with 5s spacing because the 30s cooldown only fired on
    # *fully* empty responses, not on "structurally there but 99% NaN".)
    if len(sparse) > len(tickers) * 0.5:
        logger.warning(
            f"  yfinance: {len(sparse)}/{len(tickers)} sparse — likely rate-limited, "
            f"skipping individual retry"
        )
        return True

    logger.info(f"  yfinance: retrying {len(sparse)} sparse ticker(s) individually: {','.join(sparse)}")
    for t in sparse:
        try:
            single_df = yf.download(t, period=period, progress=False, threads=False)
            if single_df is None or single_df.empty:
                continue
            try:
                flat = single_df.xs(t, axis=1, level=1)
            except KeyError:
                flat = single_df
            for field in ("Open", "High", "Low", "Close", "Volume"):
                if field in flat.columns:
                    data[(t, field)] = flat[field].reindex(data.index)
        except Exception as e:
            logger.warning(f"  yfinance: single retry failed for {t}: {e}")
    return False


def load_config(config_path: Path) -> dict:
    with open(config_path, "rb") as f:
        return tomllib.load(f)


# A finviz symbol: 1-6 uppercase alnum, optional .X / -X class suffix (BRK-B,
# AAC-U). Used to detect the finviz>=2.0.0 row-parse regression below.
_FINVIZ_TICKER_RE = re.compile(r"[A-Z][A-Z0-9]{0,5}([.\-][A-Z0-9]+)?$")


def _finviz_row_shift(rows: list[dict]) -> bool:
    """Detect the finviz>=2.0.0 row-parse regression.

    Finviz's current screener HTML carries one extra leading cell per data row,
    so the library zips every value one column to the right of its header: the
    real ticker lands in the column *after* 'Ticker' (which keeps only the
    symbol's first letter), the real Market Cap lands one past 'Market Cap', and
    so on. We detect it from the first row — a single-char 'Ticker' cell whose
    next cell is itself a full ticker — and correct positionally. A correctly
    parsed row (real multi-char ticker in 'Ticker', or a genuine 1-char ticker
    followed by a company *name*) fails the test, so a future library fix
    self-heals with no code change here.
    """
    if not rows or "Ticker" not in rows[0]:
        return False
    keys = list(rows[0].keys())
    vals = list(rows[0].values())
    i = keys.index("Ticker")
    return (
        len(vals) > i + 1
        and len(str(vals[i])) == 1
        and bool(_FINVIZ_TICKER_RE.fullmatch(str(vals[i + 1])))
    )


def run_screener(
    filters: list[str],
    signal: str | None = None,
    capture_caps: dict[str, float] | None = None,
) -> list[str]:
    """Run a Finviz screener and return list of tickers. Empty result is
    a valid outcome (returns []) — Finviz raises NoResults in that case.

    If ``capture_caps`` is provided, the screener uses ``table="Ownership"``
    so each row carries a "Market Cap" column; the parsed cap (USD) is
    stored in ``capture_caps[ticker]``. Used to feed the US IPO ladder
    after the long-side pipeline.
    """
    kwargs: dict = {"filters": filters}
    if signal:
        kwargs["signal"] = signal
    if capture_caps is not None:
        kwargs["table"] = "Ownership"
    try:
        stock_list = Screener(**kwargs)
    except NoResults:
        return []

    rows = stock_list.data
    shifted = _finviz_row_shift(rows)
    keys = list(rows[0].keys()) if rows else []

    def cell(stock: dict, col: str) -> str:
        """Value for logical column `col`, undoing the finviz row shift."""
        if not shifted:
            return stock[col]
        vals = list(stock.values())
        j = keys.index(col) + 1
        return vals[j] if j < len(vals) else stock[col]

    tickers: list[str] = []
    for stock in rows:
        t = cell(stock, "Ticker")
        tickers.append(t)
        if capture_caps is not None:
            try:
                capture_caps[t] = parse_number(cell(stock, "Market Cap"))
            except (KeyError, ValueError):
                pass
    return tickers


def parse_number(value: str) -> float:
    """Parse a finviz number string like '6.96M', '1.23B', '500K', or '5,366,687'."""
    value = value.strip().replace(",", "")
    suffixes = {"K": 1e3, "M": 1e6, "B": 1e9}
    if value and value[-1] in suffixes:
        return float(value[:-1]) * suffixes[value[-1]]
    return float(value)


def filter_shorts(
    filters: list[str],
    signal: str | None,
    perf_large_cap: float,
    perf_mid_cap: float,
    perf_small_cap: float,
    min_dollar_volume: float,
    min_consecutive_up_days: int,
    min_adr_percent: float = 0,
    adr_days: int = 20,
    futu_cfg: dict | None = None,
    rs_table: dict[str, int] | None = None,
    min_rs_percentile: int = 0,
    rs_table_3m: "pd.DataFrame | None" = None,
    min_rs_percentile_3m: int = 0,
) -> tuple[int, list[str]]:
    """Run shorts pipeline: finviz Ownership → single yfinance download →
    performance / dollar-volume / consecutive-up-days filters.
    Returns (total_found, filtered_tickers)."""
    kwargs_own = {"filters": filters, "table": "Ownership"}
    if signal:
        kwargs_own["signal"] = signal
    ownership = Screener(**kwargs_own)
    total = len(ownership.data)

    tickers = []
    market_caps: dict[str, float] = {}
    for stock in ownership.data:
        ticker = stock["Ticker"]
        try:
            cap = parse_number(stock["Market Cap"])
            tickers.append(ticker)
            market_caps[ticker] = cap
        except (KeyError, ValueError):
            continue

    if not tickers:
        return total, []

    if min_rs_percentile > 0:
        kept = filter_by_rs(tickers, rs_table, min_rs_percentile, "  [Shorts]")
        kept_set = set(kept)
        market_caps = {t: c for t, c in market_caps.items() if t in kept_set}
        tickers = kept
        if not tickers:
            return total, []

    if min_rs_percentile_3m > 0 and rs_table_3m is not None and tickers:
        import us_rs_3m
        kept = us_rs_3m.filter_by_rs(tickers, rs_table_3m, min_rs_percentile_3m)
        kept_set = set(kept)
        market_caps = {t: c for t, c in market_caps.items() if t in kept_set}
        dropped = len(tickers) - len(kept)
        logger.info(
            f"  [Shorts] {len(kept)} after RS_3M >= {min_rs_percentile_3m} "
            f"(dropped {dropped})"
        )
        tickers = kept
        if not tickers:
            return total, []

    # Prefer Futu snapshot's total_market_val (real-time, USD) over Finviz's
    # coarse "6.96M" / "1.23B" string. Fall back to the Finviz cap per-ticker
    # for any names Futu doesn't return.
    if futu_cfg and futu_cfg.get("enabled"):
        futu_caps = get_market_caps_futu(
            tickers, market="US",
            host=futu_cfg.get("host", "127.0.0.1"),
            port=futu_cfg.get("port", 11111),
        )
        if futu_caps is not None:
            covered = sum(1 for t in tickers if t in futu_caps)
            for t in tickers:
                if t in futu_caps:
                    market_caps[t] = futu_caps[t]
            logger.info(
                f"  market caps: {covered}/{len(tickers)} from Futu "
                f"(rest from Finviz)"
            )

    # Single yfinance download — shared by all three filters
    data = _yf_download_with_retry(
        tickers, period="2mo", progress=False, group_by="ticker", threads=False
    )

    now_et = datetime.now(ZoneInfo("America/New_York"))
    market_open = 9 <= now_et.hour < 16 and now_et.weekday() < 5
    today_et = now_et.date()
    if market_open:
        logger.info("  US market still open, excluding today's incomplete data")

    single = len(tickers) == 1

    # 1. Performance filter (cap-conditional, 2/3/4-week windows)
    perf_weeks = [2, 3, 4]
    passed: set[str] = set()
    for weeks in perf_weeks:
        trading_days = weeks * 5 + (2 if weeks == 4 else 0)  # 10, 15, 22
        week_hits = 0
        for ticker in tickers:
            if ticker in passed:
                continue
            try:
                if single:
                    closes = data["Close"].dropna()
                else:
                    closes = data[ticker]["Close"].dropna()
                closes = _trim_today(closes, market_open, today_et)

                if len(closes) < trading_days + 1:
                    continue

                perf = (closes.iloc[-1] - closes.iloc[-trading_days]) / closes.iloc[-trading_days] * 100
                cap = market_caps[ticker]

                if cap >= 10e9:
                    threshold = perf_large_cap
                elif cap >= 2e9:
                    threshold = perf_mid_cap
                else:
                    threshold = perf_small_cap

                if perf >= threshold:
                    passed.add(ticker)
                    week_hits += 1
            except (KeyError, ValueError, ZeroDivisionError):
                continue
        logger.info(f"  {weeks}-week window: {week_hits} new hits")

    perf_passed = list(passed)
    logger.info(f"  {len(perf_passed)} after performance filter (2/3/4 week combined)")

    # 2. Dollar volume filter (uses same data)
    if min_dollar_volume > 0 and perf_passed:
        dv_passed = _filter_dollar_volume_from_data(
            perf_passed, data, min_dollar_volume, market_open, today_et, single
        )
        logger.info(f"  {len(dv_passed)} after dollar volume filter (20-day avg)")
    else:
        dv_passed = perf_passed

    # 3. ADR% filter (uses same data)
    if min_adr_percent > 0 and dv_passed:
        dv_passed = _filter_adr_percent(
            dv_passed, data, min_adr_percent, adr_days, today_et,
            market_open=market_open, single=single,
        )
        logger.info(f"  {len(dv_passed)} after ADR% filter (>= {min_adr_percent}%, {adr_days}d)")

    # 4. Consecutive up days filter (uses same data)
    if min_consecutive_up_days > 0 and dv_passed:
        final = _filter_consecutive_up_days_from_data(
            dv_passed, data, min_consecutive_up_days, market_open, today_et, single
        )
        logger.info(f"  {len(final)} after consecutive up days filter (>= {min_consecutive_up_days})")
    else:
        final = dv_passed

    return total, final


def run_morning_gap(
    config: dict, futu_cfg: dict | None = None
) -> tuple[int, list[str]]:
    """Run the morning-gap scan. Negative offset = pre-market (skips
    intraday cumulative-volume filter); positive offset = post-open.
    Returns (offset, tickers), or (None, []) if outside scan window."""
    scan_offsets = config.get("scan_offsets", [-20, -10, 10, 15, 20, 25, 30])
    tolerance = config.get("offset_tolerance_minutes", 2)
    offset = _get_et_scan_offset(scan_offsets, tolerance)
    if offset is None:
        logger.info("[Morning Gap] Not in scan window, exiting")
        return None, []

    sign = "+" if offset >= 0 else ""
    logger.info(f"[Morning Gap] Running for offset {sign}{offset}min")

    # Phase 1: Futu snapshot-based discovery. Replaces Finviz screener +
    # ta_topgainers signal — that signal does not actually surface today's
    # pre-market gappers (it ranks by recent regular-session perf), so big
    # earnings gappers like TWLO 2026-05-01 +19.5% pre-market never entered
    # the candidate set. We now scan NASDAQ/NYSE/AMEX directly via Futu's
    # bulk snapshot and filter by pre_change_rate / change_rate at the source.
    futu_host = (futu_cfg or {}).get("host", "127.0.0.1")
    futu_port = (futu_cfg or {}).get("port", 11111)
    # 盘前门槛独立于盘后 (min_gap_percent_pre)，不设则沿用 min_gap_percent。
    pre_market = offset < 0
    min_gap_pct = config.get("min_gap_percent", 5.0)
    if pre_market:
        min_gap_pct = config.get("min_gap_percent_pre", min_gap_pct)
    discovery = discover_morning_gap_candidates(
        min_gap_pct=min_gap_pct,
        min_market_cap=config.get("min_market_cap", 300_000_000),
        min_price=config.get("min_price", 10.0),
        pre_market=pre_market,
        exchanges=config.get(
            "exchanges", ["US_NASDAQ", "US_NYSE", "US_AMEX"]
        ),
        host=futu_host,
        port=futu_port,
    )
    if discovery is None:
        logger.warning(
            "[Morning Gap] Futu discovery failed (OpenD unreachable or API error), "
            "skipping run"
        )
        return offset, []
    quotes = discovery
    tickers = list(quotes)
    logger.info(f"  Found {len(tickers)} tickers from Futu snapshot discovery")
    if not tickers:
        return offset, []

    # Phase 2: 1y daily data — used by dollar-volume / ADR% / SMA / avg-vol
    # filters. The 1y window is required for SMA200 (needs >=200 trading days);
    # the shorter-window filters (dollar volume, ADR%, avg volume) only need
    # the trailing 20-30 bars and ignore the rest.
    daily_data = _yf_download_with_retry(
        tickers, period="1y", interval="1d", progress=False,
        group_by="ticker", threads=False,
    )
    if daily_data is None or daily_data.empty:
        logger.warning("  Daily yfinance download failed, exiting")
        return offset, []

    now_et = datetime.now(ZoneInfo("America/New_York"))
    market_open = True  # always trim today's partial bar (pre- or post-open)
    today_et = now_et.date()

    # Phase 3: Dollar volume filter
    min_dv = config.get("min_dollar_volume", 0)
    if min_dv > 0:
        tickers = _filter_dollar_volume_from_data(
            tickers, daily_data, min_dv, market_open, today_et, len(tickers) == 1
        )
        logger.info(f"  {len(tickers)} after dollar volume filter (>= ${min_dv:,.0f})")
    if not tickers:
        return offset, []

    # Phase 3b: ADR% filter (replaces dropped Finviz beta filter). A gap above
    # `adr_bypass_gap_percent` relaxes the floor to `adr_bypass_min_percent`
    # — otherwise a low-ADR large cap's earnings gap can never alert.
    min_adr = config.get("min_adr_percent", 0)
    if min_adr > 0:
        adr_days = config.get("adr_days", 20)
        adr_bypass_pct = config.get("adr_bypass_gap_percent", 0)
        adr_bypass_min = config.get("adr_bypass_min_percent", 0)
        tickers = _filter_adr_percent(
            tickers, daily_data, min_adr, adr_days, today_et,
            gaps={t: q.gap for t, q in quotes.items()},
            bypass_gap_pct=adr_bypass_pct, bypass_min_pct=adr_bypass_min,
        )
        logger.info(
            f"  {len(tickers)} after ADR% filter (>= {min_adr}%, {adr_days}d, "
            f"floor {adr_bypass_min}% at gap>={adr_bypass_pct}%)"
        )
    if not tickers:
        return offset, []

    # Phase 3c: SMA50/SMA200 trend gate (replaces Finviz ta_sma50_pa / ta_sma200_pa).
    # Compared against the pre-market / live price rather than yesterday's close —
    # a reversal's first day is by definition the day it has not yet reclaimed its
    # averages, and that gap bar is exactly what `_trim_today` removes. A gap above
    # `sma_bypass_gap_percent` waives SMA50 only; SMA200 is never waived.
    use_live = config.get("sma_use_live_price", True)
    bypass_pct = config.get("sma_bypass_gap_percent", 0)
    tickers = _filter_sma_trend(
        tickers, daily_data, today_et,
        live_prices={t: q.price for t, q in quotes.items()} if use_live else None,
        gaps={t: q.gap for t, q in quotes.items()},
        bypass_gap_pct=bypass_pct,
    )
    logger.info(
        f"  {len(tickers)} after SMA50/SMA200 trend filter "
        f"(basis={'live' if use_live else 'prev close'}, "
        f"SMA50 bypass at gap>={bypass_pct}%)"
    )
    if not tickers:
        return offset, []

    # Phase 3d: 20-day average volume gate (replaces Finviz sh_avgvol_o500).
    min_avg_vol = config.get("min_avg_volume", 500_000)
    avg_days = config.get("avg_volume_days", 20)
    tickers = _filter_avg_volume(
        tickers, daily_data, min_avg_vol, avg_days, today_et
    )
    logger.info(
        f"  {len(tickers)} after 20d avg volume filter (>= {min_avg_vol:,.0f})"
    )
    if not tickers:
        return offset, []

    # Pre-market path: discovery already enforced pre_change_rate >= min_gap_pct
    # from the same Futu snapshot, so no re-validation is needed. Skip the
    # cumulative volume gate (no accumulated session volume yet pre-open).
    # Phase 3e (pre-market only): pre-market volume must be a meaningful
    # fraction of the 20d average — a 5% gap printed on a few hundred shares
    # (NVT / WIX 2026-09-11: pre_price above the whole day's range, opened
    # +1.7% / -0.1%) is noise, not a gap. `min_pre_volume_ratio = 0` disables.
    if offset < 0:
        pre_vol_ratio = config.get("min_pre_volume_ratio", 0.05)
        tickers = _filter_pre_market_volume(
            tickers, daily_data, quotes, pre_vol_ratio, avg_days, today_et
        )
        logger.info(
            f"  {len(tickers)} after pre-market volume filter "
            f"(pre_volume >= {pre_vol_ratio:.0%} x {avg_days}d avg)"
        )
        return offset, tickers

    # Phase 4: Compute 20-day avg daily volume per ticker
    avg_days = config.get("avg_volume_days", 20)
    single = len(tickers) == 1
    avg_daily_volumes: dict[str, float] = {}
    for ticker in tickers:
        try:
            if single:
                volumes = daily_data["Volume"].dropna()
            else:
                volumes = daily_data[ticker]["Volume"].dropna()
            volumes = _trim_today(volumes, market_open, today_et)
            if len(volumes) < avg_days:
                continue
            avg_daily_volumes[ticker] = float(volumes.iloc[-avg_days:].mean())
        except (KeyError, TypeError):
            continue

    tickers = [t for t in tickers if t in avg_daily_volumes]
    logger.info(f"  {len(tickers)} have sufficient 20-day daily volume data")
    if not tickers:
        return offset, []

    # Phase 5: Cumulative volume filter — prefer Futu snapshot's RTH `volume`
    # field (one call, real-time, no per-ticker yfinance flakiness — the
    # yfinance 1m path was dropping 4/4 candidates with "failed to process"
    # errors in past runs). Fall back to yfinance 1m bars when OpenD is
    # unreachable or the snapshot call errors.
    final = None
    if futu_cfg and futu_cfg.get("enabled"):
        final = intraday_cumulative_volume_futu(
            tickers, avg_daily_volumes,
            host=futu_cfg.get("host", "127.0.0.1"),
            port=futu_cfg.get("port", 11111),
        )
    if final is not None:
        logger.info(
            f"  {len(final)} after intraday cumulative volume filter "
            f"(Futu, offset={offset}m)"
        )
    else:
        intraday_data = _yf_download_with_retry(
            tickers, period="1d", interval="1m", progress=False,
            group_by="ticker", threads=False,
        )
        if intraday_data is None or intraday_data.empty:
            logger.warning("  Intraday yfinance download failed, exiting")
            return offset, []
        final = _filter_intraday_cumulative_volume(
            tickers, intraday_data, avg_daily_volumes, offset
        )
        logger.info(
            f"  {len(final)} after intraday cumulative volume filter "
            f"(yfinance, offset={offset}m)"
        )

    return offset, final


def _get_hkt_scan_offset(
    scan_offsets: list[int], tolerance_minutes: int
) -> int | None:
    """HK equivalent of `_get_et_scan_offset`. Offset is minutes relative to
    9:30 HKT (market open). HK has a 12:00–13:00 lunch break, but morning-gap
    only schedules positive offsets up to +30, so the lunch break is not a
    concern. Returns None if outside any window or weekend."""
    now_hk = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    if now_hk.weekday() >= 5:
        return None
    market_open_hk = now_hk.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes_since_open = (now_hk - market_open_hk).total_seconds() / 60
    if (
        minutes_since_open < min(scan_offsets) - tolerance_minutes
        or minutes_since_open > max(scan_offsets) + tolerance_minutes
    ):
        return None
    best = min(scan_offsets, key=lambda o: abs(o - minutes_since_open))
    if abs(best - minutes_since_open) <= tolerance_minutes:
        return best
    return None


def run_hk_morning_gap(
    config: dict, futu_cfg: dict | None = None
) -> tuple[int | None, list[str]]:
    """Run the HK morning-gap scan (post-open only).

    HK does not expose pre-auction fields via Futu snapshot, so there is no
    pre-market phase here. Scan offsets are positive minutes after 9:30 HKT
    market open (typically +10/+20/+30/+40/+50/+60).

    Returns (offset, tickers_in_yfinance_format), or (None, []) if outside
    any scan window. Tickers are e.g. `["0700.HK", "1810.HK"]` — the caller
    converts to TradingView `HKEX:N` before writing.
    """
    scan_offsets = config.get("scan_offsets", [10, 20, 30, 40, 50, 60])
    tolerance = config.get("offset_tolerance_minutes", 2)
    offset = _get_hkt_scan_offset(scan_offsets, tolerance)
    if offset is None:
        logger.info("[HK Morning Gap] Not in scan window, exiting")
        return None, []

    logger.info(f"[HK Morning Gap] Running for offset +{offset}min")

    # Phase 1: Futu HK snapshot-based discovery — cap / price / gap.
    # Discovery returns yfinance-format tickers (e.g. "0700.HK").
    futu_host = (futu_cfg or {}).get("host", "127.0.0.1")
    futu_port = (futu_cfg or {}).get("port", 11111)
    discovery = discover_hk_morning_gap_candidates(
        min_gap_pct=config.get("min_gap_percent", 5.0),
        min_market_cap=config.get("min_market_cap", 300_000_000),
        min_price=config.get("min_price", 20.0),
        exchanges=config.get("exchanges", ["HK_MAINBOARD"]),
        host=futu_host,
        port=futu_port,
    )
    if discovery is None:
        logger.warning(
            "[HK Morning Gap] Futu discovery failed (OpenD unreachable or "
            "API error), skipping run"
        )
        return offset, []
    quotes = discovery
    tickers = list(quotes)
    logger.info(f"  Found {len(tickers)} tickers from Futu HK snapshot discovery")
    if not tickers:
        return offset, []

    # Phase 2: yfinance daily for 1y (needed for SMA200) — also feeds the
    # dollar-volume / ADR% / avg-vol filters which look at the trailing
    # ~20-30 bars.
    daily_data = _yf_download_with_retry(
        tickers, period="1y", interval="1d", progress=False,
        group_by="ticker", threads=False,
    )
    if daily_data is None or daily_data.empty:
        logger.warning("  Daily yfinance download failed, exiting")
        return offset, []

    now_hk = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    market_open = True  # always trim today's partial bar
    today_hk = now_hk.date()

    # Phase 3: Dollar volume filter (HK$100M default — matches long-side baseline)
    min_dv = config.get("min_dollar_volume", 0)
    if min_dv > 0:
        tickers = _filter_dollar_volume_from_data(
            tickers, daily_data, min_dv, market_open, today_hk, len(tickers) == 1
        )
        logger.info(f"  {len(tickers)} after dollar volume filter (>= HK${min_dv:,.0f})")
    if not tickers:
        return offset, []

    # Phase 3b: ADR% filter — same big-gap floor relaxation as US.
    min_adr = config.get("min_adr_percent", 0)
    if min_adr > 0:
        adr_days = config.get("adr_days", 20)
        adr_bypass_pct = config.get("adr_bypass_gap_percent", 0)
        adr_bypass_min = config.get("adr_bypass_min_percent", 0)
        tickers = _filter_adr_percent(
            tickers, daily_data, min_adr, adr_days, today_hk,
            gaps={t: q.gap for t, q in quotes.items()},
            bypass_gap_pct=adr_bypass_pct, bypass_min_pct=adr_bypass_min,
        )
        logger.info(
            f"  {len(tickers)} after ADR% filter (>= {min_adr}%, {adr_days}d, "
            f"floor {adr_bypass_min}% at gap>={adr_bypass_pct}%)"
        )
    if not tickers:
        return offset, []

    # Phase 3c: SMA50/SMA200 trend gate — same live-price basis and SMA50 gap
    # bypass as US. HK is post-open only, so the basis is always `last_price`.
    use_live = config.get("sma_use_live_price", True)
    bypass_pct = config.get("sma_bypass_gap_percent", 0)
    tickers = _filter_sma_trend(
        tickers, daily_data, today_hk,
        live_prices={t: q.price for t, q in quotes.items()} if use_live else None,
        gaps={t: q.gap for t, q in quotes.items()},
        bypass_gap_pct=bypass_pct,
    )
    logger.info(
        f"  {len(tickers)} after SMA50/SMA200 trend filter "
        f"(basis={'live' if use_live else 'prev close'}, "
        f"SMA50 bypass at gap>={bypass_pct}%)"
    )
    if not tickers:
        return offset, []

    # Phase 3d: 20-day average volume gate (500K shares/day default)
    min_avg_vol = config.get("min_avg_volume", 500_000)
    avg_days = config.get("avg_volume_days", 20)
    tickers = _filter_avg_volume(
        tickers, daily_data, min_avg_vol, avg_days, today_hk
    )
    logger.info(
        f"  {len(tickers)} after 20d avg volume filter (>= {min_avg_vol:,.0f} shares)"
    )
    if not tickers:
        return offset, []

    # Phase 4: Compute 20-day avg daily volume per ticker for the cumulative gate
    single = len(tickers) == 1
    avg_daily_volumes: dict[str, float] = {}
    for ticker in tickers:
        try:
            if single:
                volumes = daily_data["Volume"].dropna()
            else:
                volumes = daily_data[ticker]["Volume"].dropna()
            volumes = _trim_today(volumes, market_open, today_hk)
            if len(volumes) < avg_days:
                continue
            avg_daily_volumes[ticker] = float(volumes.iloc[-avg_days:].mean())
        except (KeyError, TypeError):
            continue

    tickers = [t for t in tickers if t in avg_daily_volumes]
    logger.info(f"  {len(tickers)} have sufficient 20-day daily volume data")
    if not tickers:
        return offset, []

    # Phase 5: Cumulative-volume gate via Futu HK snapshot. No yfinance 1m
    # fallback — HK 1m yfinance data is unreliable, and discovery already
    # requires OpenD anyway, so a single source of truth keeps it simple.
    final = intraday_cumulative_volume_futu(
        tickers, avg_daily_volumes,
        host=futu_host, port=futu_port, market="HK",
    )
    if final is None:
        logger.warning(
            "  Futu cumulative-volume gate failed — emitting pre-gate list"
        )
        return offset, tickers
    logger.info(
        f"  {len(final)} after intraday cumulative volume filter "
        f"(Futu HK, offset={offset}m)"
    )
    return offset, final


def filter_consecutive_up_days(tickers: list[str], min_days: int) -> list[str]:
    """Filter tickers to those with >= min_days consecutive up days.
    Uses yfinance to fetch recent daily close prices."""
    if not tickers:
        return []

    data = yf.download(tickers, period="1mo", progress=False, group_by="ticker")
    result = []

    # If US market is still open, today's data is incomplete — exclude it
    now_et = datetime.now(ZoneInfo("America/New_York"))
    market_open = 9 <= now_et.hour < 16 and now_et.weekday() < 5
    if market_open:
        logger.info("  US market still open, excluding today's incomplete data")

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                closes = data["Close"].dropna()
            else:
                closes = data[ticker]["Close"].dropna()

            if market_open and len(closes) > 0 and closes.index[-1].date() == now_et.date():
                closes = closes.iloc[:-1]

            if len(closes) < 2:
                logger.warning(f"  yfinance: no data for {ticker}, keeping it")
                result.append(ticker)
                continue

            consecutive = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes.iloc[i] > closes.iloc[i - 1]:
                    consecutive += 1
                else:
                    break

            if consecutive >= min_days:
                result.append(ticker)
        except (KeyError, TypeError):
            logger.warning(f"  yfinance: failed to process {ticker}, keeping it")
            result.append(ticker)

    return result


def _filter_consecutive_up_days_from_data(
    tickers: list[str],
    data,
    min_days: int,
    market_open: bool,
    today_date,
    single: bool,
) -> list[str]:
    """Filter tickers to those with >= min_days consecutive up days,
    using a pre-downloaded yfinance DataFrame.
    Strict: tickers with no data are dropped."""
    if not tickers:
        return []

    result = []
    for ticker in tickers:
        try:
            if single:
                closes = data["Close"].dropna()
            else:
                closes = data[ticker]["Close"].dropna()
            closes = _trim_today(closes, market_open, today_date)

            if len(closes) < 2:
                logger.warning(f"  yfinance: no data for {ticker}, dropping")
                continue

            consecutive = 0
            for i in range(len(closes) - 1, 0, -1):
                if closes.iloc[i] > closes.iloc[i - 1]:
                    consecutive += 1
                else:
                    break

            if consecutive >= min_days:
                result.append(ticker)
        except (KeyError, TypeError):
            logger.warning(f"  yfinance: failed to process {ticker}, dropping")

    return result


def filter_dollar_volume_and_adr_yf(
    tickers: list[str],
    min_dollar_volume: float,
    min_adr_percent: float,
    adr_days: int = 20,
    dv_days: int = 20,
    ipo_drops: set[str] | None = None,
) -> list[str]:
    """Apply dollar-volume and ADR% filters via a single yfinance download.
    Either filter is skipped when its threshold is 0. Strict: tickers with
    insufficient data are dropped. If `ipo_drops` is provided, tickers
    dropped due to missing/insufficient yfinance history are recorded there
    (likely IPOs)."""
    if not tickers:
        return []

    data = yf.download(tickers, period="2mo", progress=False, group_by="ticker", threads=False)
    _retry_sparse_in_batch(data, tickers, period="2mo", min_rows=max(adr_days, dv_days))
    now_et = datetime.now(ZoneInfo("America/New_York"))
    market_open = 9 <= now_et.hour < 16 and now_et.weekday() < 5
    today_et = now_et.date()
    single = len(tickers) == 1

    if min_dollar_volume > 0:
        tickers = _filter_dollar_volume_from_data(
            tickers, data, min_dollar_volume, market_open, today_et, single,
            days=dv_days, ipo_drops=ipo_drops,
        )
    if not tickers:
        return []

    if min_adr_percent > 0:
        tickers = _filter_adr_percent(
            tickers, data, min_adr_percent, adr_days, today_et,
            market_open=market_open, single=single, ipo_drops=ipo_drops,
        )

    return tickers


def _filter_dollar_volume_from_data(
    tickers: list[str],
    data,
    min_dollar_volume: float,
    market_open: bool,
    today_date,
    single: bool,
    days: int = 20,
    ipo_drops: set[str] | None = None,
) -> list[str]:
    """Filter tickers by dollar volume using a pre-downloaded yfinance DataFrame.
    Dollar volume = latest close price * N-day average volume.
    Strict: tickers with insufficient data are dropped. When `ipo_drops` is
    given, tickers dropped for missing/insufficient yfinance history are
    recorded there."""
    if not tickers:
        return []

    result = []
    for ticker in tickers:
        try:
            closes, volumes = _get_closes_volumes(data, ticker, single)
            closes = _trim_today(closes, market_open, today_date)
            volumes = _trim_today(volumes, market_open, today_date)

            if len(volumes) < days or len(closes) < 1:
                logger.warning(f"  yfinance: insufficient data for {ticker}, dropping")
                if ipo_drops is not None:
                    ipo_drops.add(ticker)
                continue

            price = closes.iloc[-1]
            avg_vol = volumes.iloc[-days:].mean()

            if price * avg_vol >= min_dollar_volume:
                result.append(ticker)
        except (KeyError, TypeError):
            logger.warning(f"  yfinance: failed to process {ticker}, dropping")
            if ipo_drops is not None:
                ipo_drops.add(ticker)

    return result


# Retained one release as a fallback after morning-gap discovery moved to
# the Futu snapshot path. Safe to remove once Futu-discovery has proven
# stable in production (re-evaluate after 2026-06-01).
def _filter_pre_market_gap(
    tickers: list[str],
    daily_data,
    min_gap_pct: float,
    today_date,
) -> list[str]:
    """Revalidate pre-market gap via yfinance. Pulls 1m bars with prepost=True,
    compares the latest pre-market close to yesterday's daily close, and keeps
    only tickers whose actual pre-market gap >= min_gap_pct. Strict: tickers
    with no pre-market trades or no prev-close data are dropped. If the
    yfinance fetch fails entirely, the filter is skipped (returns input)."""
    if not tickers:
        return []

    pm_data = _yf_download_with_retry(
        tickers, period="1d", interval="1m", prepost=True,
        progress=False, group_by="ticker", threads=False,
    )
    if pm_data is None or pm_data.empty:
        logger.warning("  pre-market yfinance download failed, skipping gap revalidation")
        return tickers

    # Both daily_data and pm_data come from yf.download(..., group_by="ticker"),
    # which always yields MultiIndex columns (ticker, field) regardless of count.
    result = []
    for ticker in tickers:
        try:
            closes_daily = daily_data[ticker]["Close"].dropna()
            closes_daily = _trim_today(closes_daily, True, today_date)
            if len(closes_daily) < 1:
                logger.warning(f"  {ticker}: no prev-close data, dropping")
                continue
            prev_close = float(closes_daily.iloc[-1])

            pm_closes = pm_data[ticker]["Close"].dropna()
            if len(pm_closes) < 1:
                logger.info(f"  {ticker}: no pre-market trades yet, dropping")
                continue
            pm_price = float(pm_closes.iloc[-1])

            gap_pct = (pm_price - prev_close) / prev_close * 100
            if gap_pct >= min_gap_pct:
                result.append(ticker)
            else:
                logger.info(
                    f"  {ticker}: pre-market gap {gap_pct:+.2f}% < +{min_gap_pct}%, dropping"
                )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            logger.warning(f"  {ticker}: pre-market gap check failed ({e}), dropping")

    return result


def _filter_adr_percent(
    tickers: list[str],
    daily_data,
    min_pct: float,
    days: int,
    today_date,
    market_open: bool = True,
    single: bool | None = None,
    ipo_drops: set[str] | None = None,
    gaps: dict[str, float] | None = None,
    bypass_gap_pct: float | None = None,
    bypass_min_pct: float | None = None,
) -> list[str]:
    """Keep tickers whose ADR% over the last `days` completed daily bars
    is >= min_pct. ADR% = mean((High - Low) / Close) * 100. When
    `market_open` is True, today's partial bar is excluded via _trim_today;
    pass False for EOD runs where today's bar is already complete. Strict:
    tickers with insufficient data are dropped. When `ipo_drops` is given,
    tickers dropped for missing/insufficient yfinance history are recorded
    there (real ADR% < min_pct rejections are NOT recorded — that's a
    legitimate filter, not a data gap).

    Morning-gap only: a ticker whose live gap (from `gaps`) is
    >= `bypass_gap_pct` is judged against the relaxed `bypass_min_pct` floor
    instead of `min_pct` — ADR% is a static 20d property that a
    low-volatility large cap's earnings gap can never move, so the gap itself
    stands in as the volatility evidence. EOD call sites pass none of the
    three and are unaffected."""
    if not tickers:
        return []
    if single is None:
        single = len(tickers) == 1

    result = []
    for ticker in tickers:
        try:
            if single:
                highs = daily_data["High"].dropna()
                lows = daily_data["Low"].dropna()
                closes = daily_data["Close"].dropna()
            else:
                highs = daily_data[ticker]["High"].dropna()
                lows = daily_data[ticker]["Low"].dropna()
                closes = daily_data[ticker]["Close"].dropna()
            highs = _trim_today(highs, market_open, today_date)
            lows = _trim_today(lows, market_open, today_date)
            closes = _trim_today(closes, market_open, today_date)

            n = min(len(highs), len(lows), len(closes), days)
            if n < days:
                logger.warning(f"  {ticker}: insufficient daily bars for ADR% ({n}<{days}), dropping")
                if ipo_drops is not None:
                    ipo_drops.add(ticker)
                continue

            highs = highs.iloc[-days:]
            lows = lows.iloc[-days:]
            closes = closes.iloc[-days:]
            ranges = (highs.values - lows.values) / closes.values
            adr_pct = float(ranges.mean()) * 100

            gap = gaps.get(ticker) if gaps else None
            bypassed = (
                bypass_gap_pct is not None
                and bypass_gap_pct > 0
                and bypass_min_pct is not None
                and gap is not None
                and gap >= bypass_gap_pct
            )
            floor = bypass_min_pct if bypassed else min_pct
            if adr_pct >= floor:
                if bypassed and adr_pct < min_pct:
                    logger.info(
                        f"  {ticker}: ADR% {adr_pct:.2f}% < {min_pct}% but gap "
                        f"{gap:.1f}% >= {bypass_gap_pct}%, floor relaxed to "
                        f"{bypass_min_pct}%, keeping"
                    )
                result.append(ticker)
            else:
                logger.info(f"  {ticker}: ADR% {adr_pct:.2f}% < {floor}%, dropping")
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            logger.warning(f"  {ticker}: ADR% check failed ({e}), dropping")
            if ipo_drops is not None:
                ipo_drops.add(ticker)

    return result


def _filter_sma_trend(
    tickers: list[str],
    daily_data,
    today_date,
    sma_short: int = 50,
    sma_long: int = 200,
    market_open: bool = True,
    single: bool | None = None,
    live_prices: dict[str, float] | None = None,
    gaps: dict[str, float] | None = None,
    bypass_gap_pct: float | None = None,
) -> list[str]:
    """Keep tickers trading above both SMA50 and SMA200. Replaces Finviz
    `ta_sma50_pa`/`ta_sma200_pa` for the Futu-discovery path. Strict: tickers
    with insufficient history (< sma_long bars after trimming today's partial
    bar) are dropped.

    The moving averages are always computed from completed bars — a half-formed
    daily bar must never enter a 50/200-day mean. Only the *comparison basis*
    is configurable: pass `live_prices` to compare the pre-market / intraday
    price instead of the last completed close, which is what the trader is
    actually looking at during a morning-gap scan. A ticker absent from
    `live_prices`, or carrying a non-positive value there, falls back to the
    close.

    `bypass_gap_pct` exempts a big gapper from the SMA50 requirement only —
    the SMA200 floor is never waived. This targets the long-term-uptrend /
    mid-term-pullback / event-gap shape without admitting bottom-bounces in
    genuine downtrends. Tickers missing from `gaps` take the strict path.

    With all three new parameters omitted, behavior is identical to the
    pre-2026-08-13 gate.
    """
    if not tickers:
        return []
    if single is None:
        single = len(tickers) == 1

    result = []
    for ticker in tickers:
        try:
            if single:
                closes = daily_data["Close"].dropna()
            else:
                closes = daily_data[ticker]["Close"].dropna()
            closes = _trim_today(closes, market_open, today_date)
            if len(closes) < sma_long:
                logger.warning(
                    f"  {ticker}: insufficient daily bars for SMA{sma_long} "
                    f"({len(closes)}<{sma_long}), dropping"
                )
                continue
            last_close = float(closes.iloc[-1])
            sma_s = float(closes.iloc[-sma_short:].mean())
            sma_l = float(closes.iloc[-sma_long:].mean())

            ref, basis = last_close, "close"
            live = (live_prices or {}).get(ticker)
            if live is not None:
                try:
                    live = float(live)
                except (TypeError, ValueError):
                    live = None
                if live is not None and live > 0:
                    ref, basis = live, "live"

            gap = (gaps or {}).get(ticker)
            bypass = (
                bypass_gap_pct is not None
                and bypass_gap_pct > 0
                and gap is not None
                and gap >= bypass_gap_pct
            )

            if ref < sma_l:
                logger.info(
                    f"  {ticker}: {basis} {ref:.2f} below SMA{sma_long}={sma_l:.2f}"
                    f"{' (gap bypass does not waive SMA200)' if bypass else ''}"
                    ", dropping"
                )
                continue
            if ref < sma_s and not bypass:
                logger.info(
                    f"  {ticker}: {basis} {ref:.2f} below SMA{sma_short}={sma_s:.2f}"
                    ", dropping"
                )
                continue
            if ref < sma_s and bypass:
                logger.info(
                    f"  {ticker}: {basis} {ref:.2f} below SMA{sma_short}={sma_s:.2f} "
                    f"but gap {gap:.1f}% >= {bypass_gap_pct}%, SMA{sma_short} waived"
                )
            result.append(ticker)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"  {ticker}: SMA trend check failed ({e}), dropping")

    return result


def _filter_avg_volume(
    tickers: list[str],
    daily_data,
    min_avg_vol: float,
    days: int,
    today_date,
    market_open: bool = True,
    single: bool | None = None,
) -> list[str]:
    """Keep tickers whose N-day average daily volume is >= min_avg_vol.
    Replaces Finviz `sh_avgvol_o500` for the Futu-discovery path. Strict:
    tickers with fewer than `days` completed bars are dropped."""
    if not tickers:
        return []
    if single is None:
        single = len(tickers) == 1

    result = []
    for ticker in tickers:
        try:
            if single:
                volumes = daily_data["Volume"].dropna()
            else:
                volumes = daily_data[ticker]["Volume"].dropna()
            volumes = _trim_today(volumes, market_open, today_date)
            if len(volumes) < days:
                logger.warning(
                    f"  {ticker}: insufficient daily bars for avg vol "
                    f"({len(volumes)}<{days}), dropping"
                )
                continue
            avg = float(volumes.iloc[-days:].mean())
            if avg >= min_avg_vol:
                result.append(ticker)
            else:
                logger.info(
                    f"  {ticker}: 20d avg vol {avg:,.0f} < {min_avg_vol:,.0f}, "
                    f"dropping"
                )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"  {ticker}: avg vol check failed ({e}), dropping")

    return result


def _filter_pre_market_volume(
    tickers: list[str],
    daily_data,
    quotes: dict,
    min_ratio: float,
    days: int,
    today_date,
    single: bool | None = None,
) -> list[str]:
    """Keep tickers whose Futu pre-market volume is >= min_ratio x N-day
    average daily volume. Pre-market only (negative offsets).

    A pre_change_rate >= 5% printed on a few hundred shares is a thin-tape
    artifact, not a gap. ``quotes`` is the discovery result
    (``{ticker: GapQuote}``); a quote without ``pre_volume`` is KEPT (nothing
    to judge). ``min_ratio <= 0`` disables the gate. Missing / short daily
    history drops, matching ``_filter_avg_volume`` strictness.
    """
    if not tickers:
        return []
    if not min_ratio or min_ratio <= 0:
        return list(tickers)
    if single is None:
        single = len(tickers) == 1

    result = []
    for ticker in tickers:
        pre_vol = getattr(quotes.get(ticker), "pre_volume", None)
        if pre_vol is None:
            result.append(ticker)
            continue
        try:
            if single:
                volumes = daily_data["Volume"].dropna()
            else:
                volumes = daily_data[ticker]["Volume"].dropna()
            volumes = _trim_today(volumes, True, today_date)
            if len(volumes) < days:
                logger.warning(
                    f"  {ticker}: insufficient daily bars for pre-market vol "
                    f"ratio ({len(volumes)}<{days}), dropping"
                )
                continue
            avg = float(volumes.iloc[-days:].mean())
            need = min_ratio * avg
            if float(pre_vol) >= need:
                result.append(ticker)
            else:
                logger.info(
                    f"  {ticker}: pre-market vol {float(pre_vol):,.0f} < "
                    f"{min_ratio:.0%} x {days}d avg {avg:,.0f} "
                    f"(need {need:,.0f}), dropping"
                )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(
                f"  {ticker}: pre-market vol check failed ({e}), dropping"
            )

    return result


def _filter_intraday_cumulative_volume(
    tickers: list[str],
    intraday_data,
    avg_daily_volumes: dict[str, float],
    offset_minutes: int,
) -> list[str]:
    """For each ticker, sum the 1-minute volume bars from 9:30 ET up to
    9:30 + offset_minutes ET (exclusive of the +offset bar) and keep
    tickers whose cumulative volume >= their 20-day average daily volume.
    Strict: tickers with missing data are dropped."""
    if not tickers:
        return []

    et = ZoneInfo("America/New_York")
    today_et = datetime.now(et).date()
    open_ts = datetime.combine(today_et, datetime.min.time(), tzinfo=et).replace(hour=9, minute=30)
    end_ts = open_ts + timedelta(minutes=offset_minutes)

    single = len(tickers) == 1
    result = []
    for ticker in tickers:
        try:
            if single:
                volumes = intraday_data["Volume"].dropna()
            else:
                volumes = intraday_data[ticker]["Volume"].dropna()

            if len(volumes) == 0:
                logger.warning(f"  yfinance 1m: no data for {ticker}, dropping")
                continue

            # yfinance 1m index is tz-aware in ET (or UTC depending on version).
            # Normalize to ET for comparison.
            idx = volumes.index
            if idx.tz is None:
                idx = idx.tz_localize("America/New_York")
            else:
                idx = idx.tz_convert("America/New_York")
            mask = (idx >= open_ts) & (idx < end_ts)
            cumulative = volumes[mask.values].sum()

            avg_daily = avg_daily_volumes.get(ticker)
            if avg_daily is None or avg_daily <= 0:
                continue

            if cumulative >= avg_daily:
                result.append(ticker)
        except (KeyError, TypeError, AttributeError):
            logger.warning(f"  yfinance 1m: failed to process {ticker}, dropping")

    return result


def filter_relative_volume(
    tickers: list[str],
    min_rvol: float,
    days: int = 20,
    ipo_drops: set[str] | None = None,
) -> list[str]:
    """Filter tickers by relative volume: latest day's volume / N-day average volume >= min_rvol.
    Uses yfinance to fetch daily volume data.
    Strict: tickers with missing/insufficient data are dropped. When
    `ipo_drops` is given, tickers dropped for missing/insufficient yfinance
    history are recorded there."""
    if not tickers:
        return []

    data = yf.download(tickers, period="2mo", progress=False, group_by="ticker", threads=False)
    _retry_sparse_in_batch(data, tickers, period="2mo", min_rows=days + 1)
    result = []

    now_et = datetime.now(ZoneInfo("America/New_York"))
    market_open = 9 <= now_et.hour < 16 and now_et.weekday() < 5

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                volumes = data["Volume"].dropna()
            else:
                volumes = data[ticker]["Volume"].dropna()

            if market_open and len(volumes) > 0 and volumes.index[-1].date() == now_et.date():
                volumes = volumes.iloc[:-1]

            if len(volumes) < days + 1:
                logger.warning(f"  yfinance: insufficient volume data for {ticker}, dropping")
                if ipo_drops is not None:
                    ipo_drops.add(ticker)
                continue

            current_vol = volumes.iloc[-1]
            avg_vol = volumes.iloc[-(days + 1):-1].mean()

            if avg_vol > 0:
                rvol = current_vol / avg_vol
                if rvol >= min_rvol:
                    result.append(ticker)
        except (KeyError, TypeError):
            logger.warning(f"  yfinance: failed to process {ticker}, dropping")
            if ipo_drops is not None:
                ipo_drops.add(ticker)

    return result


def check_market_down(threshold: float = -1.0) -> bool:
    """Check if both SPY and QQQ are down at least |threshold|%."""
    spy = get_stock("SPY")
    qqq = get_stock("QQQ")
    spy_change = float(spy["Change"].strip("%"))
    qqq_change = float(qqq["Change"].strip("%"))
    logger.info(f"  SPY: {spy_change:+.2f}%  QQQ: {qqq_change:+.2f}%")
    return spy_change <= threshold and qqq_change <= threshold


def _get_et_scan_offset(
    scan_offsets: list[int], tolerance_minutes: int
) -> int | None:
    """Determine which scan offset (in minutes relative to 9:30 ET, negative
    = pre-market) the current ET time matches, within tolerance. Returns
    None if outside any window or weekend."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return None
    market_open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes_since_open = (now_et - market_open_et).total_seconds() / 60
    if (
        minutes_since_open < min(scan_offsets) - tolerance_minutes
        or minutes_since_open > max(scan_offsets) + tolerance_minutes
    ):
        return None
    best = min(scan_offsets, key=lambda o: abs(o - minutes_since_open))
    if abs(best - minutes_since_open) <= tolerance_minutes:
        return best
    return None


def _gap_scan_stem(offset_min: int, *, hk: bool = False) -> str:
    """Per-scan morning-gap file stem: each offset gets its own dated file
    (`MorningGapPre20`, `MorningGap5`, `HKMorningGap10`) so every scan's
    snapshot is preserved — a 0-ticker scan simply leaves no file for that
    offset instead of overwriting the day's single file."""
    if hk:
        return f"HKMorningGap{offset_min}"
    if offset_min < 0:
        return f"MorningGapPre{-offset_min}"
    return f"MorningGap{offset_min}"


def _write_webull(tickers: list[str], dated_path: Path, output_dir: Path) -> None:
    """Mirror tickers as newline-separated .txt under output/Webull/{market}/.
    Webull's "Upload as File" only recognizes one ticker per line — comma-
    separated lists silently truncate after the first 1-2 entries."""
    if not tickers:  # mirror write_watchlist: no 0-byte artifacts
        return
    market = dated_path.parent.name  # "US" or "HK"
    webull_dir = output_dir / "Webull" / market
    webull_dir.mkdir(parents=True, exist_ok=True)
    (webull_dir / dated_path.name).write_text("\n".join(tickers) + "\n")


def write_watchlist(tickers: list[str], output_path: Path, fmt: str = "comma") -> None:
    """Write tickers to file. Empty list writes nothing — no 0-byte artifact —
    and deliberately does NOT delete an existing file: a manual EOD rerun
    dedups today's names to 0 against the master, and deleting would destroy
    the earlier run's legitimate output. The log is the record of a 0-result
    scan."""
    if not tickers:
        return
    if fmt == "comma":
        content = ",".join(tickers)
    else:
        content = "\n".join(tickers)
    output_path.write_text(content + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["eod", "us-eod", "hk-eod", "morning-gap", "hk-morning-gap", "report", "rs-line-audit"],
        default="eod",
        help="eod: full end-of-day run (US + HK). "
             "us-eod: US only (Longs/Leaders/Shorts/RS/IPO) — for the morning HKT slot when HK market is mid-session. "
             "hk-eod: HK only (Shorts + Longs/Leaders/RS) — for the evening HKT slot after HK market closes. "
             "morning-gap: US intraday gap-up scanner. "
             "hk-morning-gap: HK intraday gap-up scanner (post-open only — Futu does not expose HK pre-auction fields). "
             "report: generate CANSLIM Markdown+HTML report from today's dated .txt files (requires --market).",
    )
    parser.add_argument(
        "--market", choices=["us", "hk", "both"],
        help="Required when --mode=report. Selects which market's .txt files to analyze.",
    )
    parser.add_argument(
        "--date",
        help="Optional YYYY-MM-DD override for --mode=report (default: today HKT).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only meaningful with --mode=rs-line-audit: write the audit report "
             "and sidecars, but do NOT prune state/eod_seen_*.txt (skips the "
             "confirmation prompt too).",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Only meaningful with --mode=rs-line-audit: skip the interactive "
             "y/N confirmation and prune state/eod_seen_*.txt immediately.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    now_hkt = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    mode_label = {
        "eod": "End-of-Day (full)",
        "us-eod": "End-of-Day (US only)",
        "hk-eod": "End-of-Day (HK only)",
        "morning-gap": "Morning-Gap",
        "hk-morning-gap": "Morning-Gap (HK)",
    }.get(args.mode, args.mode)
    banner = f"  RUN {now_hkt.strftime('%Y-%m-%d %A %H:%M %Z')}  |  mode={mode_label}  "
    bar = "═" * (len(banner) + 2)
    logger.info(bar)
    logger.info(banner)
    logger.info(bar)

    project_root = Path(__file__).parent
    config_path = project_root / "config.toml"
    if not config_path.exists():
        logger.error(f"Config not found: {config_path}")
        return 1

    config = load_config(config_path)
    settings = config.get("settings", {})
    output_dir = project_root / settings.get("output_dir", "output")
    output_dir.mkdir(exist_ok=True)
    delay = settings.get("delay_between_requests", 3.0)
    fmt = settings.get("output_format", "comma")

    # Global dollar volume threshold ($100M) — shared by Longs and RS
    min_dollar_volume = settings.get("min_dollar_volume", 0)
    # Global ADR% threshold — applied to Longs / Leaders / RS / Shorts / HK Shorts /
    # Morning Gap. Replaces the dropped Finviz beta filter for capturing
    # catalyst-driven volatility regardless of multi-year market correlation.
    min_adr_percent = settings.get("min_adr_percent", 0)
    adr_days = settings.get("adr_days", 20)
    # IBD RS Rating gate, applied right after run_screener (before yfinance
    # dollar-volume) to cut downstream work by ~10x. Leaders use
    # min_rs_percentile (default 80 = top 20%); Longs (every split) use
    # min_rs_percentile_longs (default 90 = top 10%); the conditional RS
    # group and US Shorts use min_rs_percentile_rs / min_rs_percentile_shorts,
    # each falling back to min_rs_percentile_longs when unset.
    # min_rs_percentile_3m adds a 3M layer on Leaders / RS group / Shorts
    # (double gate). Any knob = 0 disables that tier.
    # 12M source: Fred6725/rs-log (refreshed weekday ~01:30 UTC).
    min_rs_percentile = settings.get("min_rs_percentile", 0)
    min_rs_percentile_longs = settings.get("min_rs_percentile_longs", 0)
    min_rs_percentile_rs = settings.get("min_rs_percentile_rs", min_rs_percentile_longs)
    min_rs_percentile_shorts = settings.get(
        "min_rs_percentile_shorts", min_rs_percentile_longs
    )
    min_rs_percentile_3m = settings.get("min_rs_percentile_3m", 0)

    today = date.today().strftime("%Y_%m_%d")
    today_date = date.today()

    us_output_dir = output_dir / "TV" / "US"
    us_output_dir.mkdir(parents=True, exist_ok=True)
    hk_output_dir = output_dir / "TV" / "HK"
    hk_output_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "report":
        if args.market not in ("us", "hk"):
            logger.error("--mode=report requires --market {us,hk} (not 'both')")
            return 1
        from report.__main__ import run as run_report
        return run_report(args.market, args.date)

    if args.mode == "rs-line-audit":
        from rs_line_audit import run_audit
        return run_audit(
            config, output_dir, args.market or "both",
            dry_run=args.dry_run, assume_yes=args.yes,
        )

    # Cold-wake guard: launchd fires the moment the machine wakes, but Wi-Fi /
    # DNS resolver may need 10-30s to come up. Without this gate every fetcher
    # NXDOMAINs and the pipeline writes 0-byte .txt files. EOD modes return 1
    # on timeout (loud failure → launchd retries next slot); morning-gap modes
    # exit 0 silently to honor the "no hard error path" rule in CLAUDE.md.
    from net_ready import wait_for_network
    eod_modes = {"eod", "us-eod", "hk-eod"}
    morning_modes = {"morning-gap", "hk-morning-gap"}
    if args.mode in eod_modes or args.mode in morning_modes:
        # finviz is the heaviest US dep; raw.githubusercontent.com hosts both
        # US/HK RS cloud CSVs; query1.finance.yahoo.com is the yfinance endpoint
        # — probing it too keeps the pipeline from starting while Yahoo is still
        # unreachable on a cold-wake link (observed: the machine took ~12 min
        # after a pmset wake to reach finviz, and yfinance stayed flaky a while
        # longer, so batch downloads came back empty). settle_seconds rides out
        # the first-minute flakiness after the handshake succeeds.
        hosts = ["finviz.com", "raw.githubusercontent.com", "query1.finance.yahoo.com"]
        if not wait_for_network(hosts, settle_seconds=20.0):
            if args.mode in morning_modes:
                logger.warning(
                    f"[net-ready] Skipping {args.mode} run — network never came up"
                )
                # Best-effort alert so the lost scan window is visible
                # (2026-08-27: Clash DNS flap silently killed all three
                # pre-market scans). Swallows failure — clean-exit stands.
                from notify import notify_scan_skipped
                notify_scan_skipped(
                    args.mode, "network never came up within 300s", config
                )
                return 0
            return 1

    if args.mode == "hk-eod":
        # HK-only EOD path: skip the entire US pipeline (Finviz screeners, IBD
        # RS fetch, IPO collection) and run only run_hk_eod. Intended for the
        # evening HKT launchd slot, after HK market has closed and k-line data
        # is finalized.
        try:
            run_hk_eod(
                config=config,
                output_dir=output_dir,
                today_iso=str(today),
                write_watchlist=write_watchlist,
                write_webull=_write_webull,
                futu_sync=_futu_sync,
                tv_sync=_tv_sync,
                load_seen=_load_seen,
                persist_seen=_persist_seen,
                eod_seen_path=_eod_seen_path,
                dedup_seen=_dedup_seen,
            )
        except Exception as e:
            logger.warning(f"[HK EOD] Pipeline failed: {e}")
        try:
            cleanup_old_outputs(output_dir, today_date)
        except Exception as e:
            logger.warning(f"[cleanup] Failed: {e}")
        logger.info("Done.")
        return 0

    if args.mode in ("eod", "us-eod"):
        # --- Cross-day master 'seen' files (per market) ---
        # Each EOD group's daily output is filtered against this master so a
        # ticker only ever appears once across all days/groups. Master grows
        # monotonically; reset by deleting state/eod_seen_{US,HK}.txt manually.
        # MorningGap is intentionally excluded (has its own per-day seen flow).
        us_seen_path = _eod_seen_path(output_dir, "US")
        hk_seen_path = _eod_seen_path(output_dir, "HK")

        # --- SMA50 prune (US master, before load) ---
        # Tickers whose close sat below SMA50 for N consecutive completed days
        # are removed from the master (backed up first) so they can re-qualify
        # later. Soft-fail: any error skips the prune, EOD proceeds.
        try:
            from sma50_prune import prune_us_master
            prune_us_master(us_seen_path, config.get("sma50_prune", {}))
        except Exception as e:
            logger.warning(f"[sma50-prune] failed, master untouched: {e}")

        us_seen = _load_seen(us_seen_path)
        hk_seen = _load_seen(hk_seen_path)
        logger.info(
            f"[EOD seen] US: {len(us_seen)} | HK: {len(hk_seen)} loaded "
            f"(reset by deleting {us_seen_path.parent}/eod_seen_*.txt)"
        )

        # --- IBD RS Rating table (US only; applied to Longs + Leaders) ---
        # Fetched once per run, cached to state/rs_rating_<date>.csv. None on
        # any failure; downstream filter calls degrade to no-ops with warnings.
        rs_table = (
            fetch_rs_table(output_dir, today)
            if max(
                min_rs_percentile,
                min_rs_percentile_longs,
                min_rs_percentile_rs,
                min_rs_percentile_shorts,
            ) > 0
            else None
        )

        # --- IBD-style 3M RS table (local, vs SPY) ---
        # Built once per run. Triggered only when the 3M knob is non-zero.
        # Universe = Fred6725 12M table's tickers (~6100). Cached to
        # state/rs_rating_3m_<date>.csv with raw_score column for IPO
        # ladder out-of-universe percentile lookup.
        import us_rs_3m  # local import: us_rs_3m imports main._yf_download_with_retry
        rs_table_3m = (
            us_rs_3m.build_3m_table(
                output_dir,
                datetime.strptime(today, "%Y_%m_%d").date(),
                rs_table_12m=rs_table,
            )
            if min_rs_percentile_3m > 0
            else None
        )

        # --- IPO collector ---
        # Tickers that pass Finviz's long-side screener but get dropped by
        # yfinance for missing/insufficient daily history accumulate here.
        # Written to its own dated file + Futu group after the long-side
        # pipeline finishes. Separate cross-day master so a ticker that ages
        # in still lands in its proper long-side group on first qualifying day.
        ipo_drops: set[str] = set()
        ipo_finviz_caps: dict[str, float] = {}
        ipo_seen_path = _eod_seen_path(output_dir, "IPO")
        ipo_seen = _load_seen(ipo_seen_path)

        # --- Longs (collect per-strategy — write deferred until after cross-group dedup) ---
        # Config list order = priority order (earlier wins). After collection,
        # tickers are assigned exclusively to the highest-priority strategy.
        longs_cfgs = config.get("longs", [])
        longs_per_strategy: list[tuple[str, str, set[str]]] = []  # (key, name, tickers)
        for i, screener_cfg in enumerate(longs_cfgs):
            name = screener_cfg["name"]
            key = screener_cfg["key"]
            _log_section(f"[Longs/{key}] Running: {name}")
            try:
                tickers = run_screener(
                    screener_cfg["filters"],
                    screener_cfg.get("signal"),
                    capture_caps=ipo_finviz_caps,
                )
                logger.info(f"  Found {len(tickers)} tickers")
                if min_rs_percentile_longs > 0 and tickers:
                    tickers = filter_by_rs(
                        tickers, rs_table, min_rs_percentile_longs, f"  [Longs/{key}]"
                    )
                if (min_dollar_volume > 0 or min_adr_percent > 0) and tickers:
                    tickers = filter_dollar_volume_and_adr_yf(
                        tickers, min_dollar_volume, min_adr_percent, adr_days,
                        ipo_drops=ipo_drops,
                    )
                    logger.info(
                        f"  {len(tickers)} after dollar volume "
                        f"(>= ${min_dollar_volume:,.0f}) + ADR% (>= {min_adr_percent}%) filter"
                    )
                min_rvol = screener_cfg.get("min_relative_volume")
                if min_rvol and tickers:
                    rvol_days = screener_cfg.get("relative_volume_days", 20)
                    tickers = filter_relative_volume(
                        tickers, min_rvol, rvol_days, ipo_drops=ipo_drops,
                    )
                    logger.info(f"  {len(tickers)} after relative volume filter (>= {min_rvol}x {rvol_days}-day avg)")
                longs_per_strategy.append((key, name, set(tickers)))
            except Exception as e:
                logger.warning(f"  Failed: {e}")
                longs_per_strategy.append((key, name, set()))
            if i < len(longs_cfgs) - 1:
                time.sleep(delay)

        # Internal priority dedup: earlier strategy wins.
        seen: set[str] = set()
        longs_dedup: list[tuple[str, str, set[str]]] = []
        for key, name, tickers in longs_per_strategy:
            before = len(tickers)
            exclusive = tickers - seen
            seen.update(exclusive)
            removed = before - len(exclusive)
            if removed:
                logger.info(f"[Longs/{key}] Dedup: removed {removed} (kept by higher-priority strategy)")
            longs_dedup.append((key, name, exclusive))
        longs_tickers: set[str] = seen  # union, used for Leaders/RS dedup

        time.sleep(delay)

        # --- Leaders (collect only) ---
        leaders_tickers: set[str] = set()
        for i, screener_cfg in enumerate(config.get("leaders", [])):
            name = screener_cfg["name"]
            _log_section(f"[Leaders] Running: {name}")
            try:
                tickers = run_screener(
                    screener_cfg["filters"],
                    screener_cfg.get("signal"),
                    capture_caps=ipo_finviz_caps,
                )
                logger.info(f"  Found {len(tickers)} tickers")
                if min_rs_percentile > 0 and tickers:
                    tickers = filter_by_rs(
                        tickers, rs_table, min_rs_percentile, f"  [Leaders/{name}]"
                    )
                if min_rs_percentile_3m > 0 and rs_table_3m is not None and tickers:
                    before = len(tickers)
                    tickers = us_rs_3m.filter_by_rs(
                        tickers, rs_table_3m, min_rs_percentile_3m
                    )
                    logger.info(
                        f"  [Leaders/{name}] {len(tickers)} after RS_3M >= "
                        f"{min_rs_percentile_3m} (dropped {before - len(tickers)})"
                    )
                if (min_dollar_volume > 0 or min_adr_percent > 0) and tickers:
                    tickers = filter_dollar_volume_and_adr_yf(
                        tickers, min_dollar_volume, min_adr_percent, adr_days,
                        ipo_drops=ipo_drops,
                    )
                    logger.info(
                        f"  {len(tickers)} after dollar volume "
                        f"(>= ${min_dollar_volume:,.0f}) + ADR% (>= {min_adr_percent}%) filter"
                    )
                leaders_tickers.update(tickers)
            except Exception as e:
                logger.warning(f"  Failed: {e}")
            if i < len(config.get("leaders", [])) - 1:
                time.sleep(delay)

        if config.get("leaders"):
            time.sleep(delay)

        # --- Shorts (independent — write directly) ---
        shorts_cfg = config.get("shorts")
        if shorts_cfg:
            _log_section(f"[Shorts] Running: {shorts_cfg['name']}")
            try:
                total, shorts_tickers = filter_shorts(
                    shorts_cfg["filters"],
                    shorts_cfg.get("signal"),
                    perf_large_cap=shorts_cfg.get("perf_large_cap", 50),
                    perf_mid_cap=shorts_cfg.get("perf_mid_cap", 200),
                    perf_small_cap=shorts_cfg.get("perf_small_cap", 300),
                    min_dollar_volume=shorts_cfg.get("min_dollar_volume", 100_000_000),
                    min_consecutive_up_days=shorts_cfg.get("min_consecutive_up_days", 3),
                    min_adr_percent=shorts_cfg.get("min_adr_percent", min_adr_percent),
                    adr_days=shorts_cfg.get("adr_days", adr_days),
                    futu_cfg=config.get("futu") or {},
                    rs_table=rs_table,
                    min_rs_percentile=min_rs_percentile_shorts,
                    rs_table_3m=rs_table_3m,
                    min_rs_percentile_3m=min_rs_percentile_3m,
                )
                logger.info(f"  Found {total} tickers from finviz Ownership screener")

                sorted_shorts = sorted(set(shorts_tickers))
                dated = us_output_dir / f"{today}_Shorts.txt"
                write_watchlist(sorted_shorts, dated, fmt)
                logger.info(f"[Shorts] Final: {len(sorted_shorts)} tickers -> {dated}")
                _write_webull(sorted_shorts, dated, output_dir)
                _futu_sync(config, "shorts", sorted_shorts, "US")
                _tv_sync(config, "shorts", sorted_shorts, "US")
            except Exception as e:
                logger.warning(f"[Shorts] Failed: {e}")

        time.sleep(delay)

        # --- RS (conditional, collect only) ---
        rs_tickers: set[str] = set()
        rs_ran = False
        rs_cfg = config.get("rs")
        if rs_cfg:
            _log_section("[RS] Checking market condition...")
            try:
                if check_market_down():
                    logger.info("[RS] Condition met, running screener...")
                    time.sleep(delay)
                    found = run_screener(
                        rs_cfg["filters"],
                        rs_cfg.get("signal"),
                        capture_caps=ipo_finviz_caps,
                    )
                    logger.info(f"  Found {len(found)} tickers")
                    if min_rs_percentile_rs > 0 and found:
                        found = filter_by_rs(
                            found, rs_table, min_rs_percentile_rs, "  [RS]"
                        )
                    if min_rs_percentile_3m > 0 and rs_table_3m is not None and found:
                        before = len(found)
                        found = us_rs_3m.filter_by_rs(
                            found, rs_table_3m, min_rs_percentile_3m
                        )
                        logger.info(
                            f"  [RS] {len(found)} after RS_3M >= "
                            f"{min_rs_percentile_3m} (dropped {before - len(found)})"
                        )
                    if (min_dollar_volume > 0 or min_adr_percent > 0) and found:
                        found = filter_dollar_volume_and_adr_yf(
                            found, min_dollar_volume, min_adr_percent, adr_days,
                            ipo_drops=ipo_drops,
                        )
                        logger.info(
                            f"  {len(found)} after dollar volume "
                            f"(>= ${min_dollar_volume:,.0f}) + ADR% (>= {min_adr_percent}%) filter"
                        )
                    rs_tickers.update(found)
                    rs_ran = True
                else:
                    logger.info("[RS] Condition not met (SPY/QQQ not both down >=1.0%), skipping")
            except Exception as e:
                logger.warning(f"[RS] Failed: {e}")

        # --- Cross-group dedup: priority Longs > Leaders ---
        # A ticker firing in both Longs and Leaders is kept only in Longs.
        # RS is intentionally NOT subtracted here — it's the conditional
        # weak-market scan, and re-surfacing a Longs/Leaders ticker that
        # still holds up on a market-down day is the entire signal.
        before_le = len(leaders_tickers)
        leaders_tickers -= longs_tickers
        removed_le = before_le - len(leaders_tickers)
        if removed_le:
            logger.info(
                f"[Dedup] Priority Longs > Leaders: "
                f"removed {removed_le} from Leaders"
            )

        # --- Write Longs (one file per strategy; Leaders/RS dedup already applied to union) ---
        # Filename matches Futu group name (PascalCase).
        futu_groups_cfg = (config.get("futu") or {}).get("groups") or {}
        repeat_cfg = config.get("eod_repeat") or {}
        repeat_keys = set(repeat_cfg.get("keys") or []) if repeat_cfg.get("enabled") else set()
        repeat_tickers: set[str] = set()
        written_longs: dict[str, list[str]] = {}
        for key, name, tickers in longs_dedup:
            futu_key = f"longs_{key}"
            file_stem = futu_groups_cfg.get(futu_key) or key  # fallback to key if unmapped
            sorted_t = sorted(tickers)
            # Collect repeats BEFORE _dedup_seen — it mutates us_seen.
            if key in repeat_keys:
                repeat_tickers.update(_repeat_hits(f"[Repeat/{key}]", sorted_t, us_seen))
            sorted_t = _dedup_seen(f"[Longs/{key}]", sorted_t, us_seen, us_seen_path)
            dated = us_output_dir / f"{today}_{file_stem}.txt"
            write_watchlist(sorted_t, dated, fmt)
            logger.info(f"[Longs/{key}] {len(sorted_t)} tickers -> {dated}")
            _write_webull(sorted_t, dated, output_dir)
            _futu_sync(config, futu_key, sorted_t, "US")
            _tv_sync(config, futu_key, sorted_t, "US")
            written_longs[key] = sorted_t

        # --- Write Leaders ---
        if config.get("leaders"):
            sorted_leaders = sorted(leaders_tickers)
            sorted_leaders = _dedup_seen("[Leaders]", sorted_leaders, us_seen, us_seen_path)
            dated = us_output_dir / f"{today}_Leaders.txt"
            write_watchlist(sorted_leaders, dated, fmt)
            logger.info(f"[Leaders] Total unique: {len(sorted_leaders)} -> {dated}")
            _write_webull(sorted_leaders, dated, output_dir)
            _futu_sync(config, "leaders", sorted_leaders, "US")
            _tv_sync(config, "leaders", sorted_leaders, "US")

        # --- Write Repeat (old names that re-fired an event group today) ---
        # These are already in the cross-day master, so they were dropped from
        # their own group's .txt above. Read-only w.r.t. us_seen — writing them
        # here neither consults nor mutates the master again.
        if repeat_keys:
            sorted_repeat = sorted(repeat_tickers)
            repeat_stem = repeat_cfg.get("file_stem") or "Repeat"
            dated = us_output_dir / f"{today}_{repeat_stem}.txt"
            write_watchlist(sorted_repeat, dated, fmt)
            logger.info(f"[Repeat] Total unique: {len(sorted_repeat)} -> {dated}")
            _write_webull(sorted_repeat, dated, output_dir)
            _futu_sync(config, "eod_repeat", sorted_repeat, "US")
            _tv_sync(config, "eod_repeat", sorted_repeat, "US")

        # --- Write RS (only if it actually ran) ---
        # RS bypasses cross-day master dedup (eod_seen_US.txt) — see the
        # within-day comment above. _dedup_seen is intentionally NOT called
        # here so an RS hit neither consults nor mutates us_seen.
        if rs_ran:
            sorted_rs = sorted(rs_tickers)
            dated = us_output_dir / f"{today}_RS.txt"
            write_watchlist(sorted_rs, dated, fmt)
            logger.info(f"[RS] Found {len(sorted_rs)} tickers -> {dated}")
            _write_webull(sorted_rs, dated, output_dir)
            _futu_sync(config, "rs", sorted_rs, "US")
            _tv_sync(config, "rs", sorted_rs, "US")

        # --- RS-line trend annotation (log only, no output change) ---
        # rs_table_3m is the cloud-published 3M CSV frame, which carries the
        # rs_below_ma/rs_days_below_ma/rs_frac_below_ma columns (added cloud-side);
        # we READ them here, never compute locally. Silent until the cloud seeds
        # a CSV carrying those columns (summarize_rs_line returns None meanwhile).
        import rs_line
        for _key, _tickers in written_longs.items():
            _s = rs_line.summarize_rs_line(_tickers, rs_table_3m)
            if _s:
                logger.info(f"[Longs/{_key}] {_s}")
        if config.get("leaders"):
            _s = rs_line.summarize_rs_line(sorted_leaders, rs_table_3m)
            if _s:
                logger.info(f"[Leaders] {_s}")
        if rs_ran:
            _s = rs_line.summarize_rs_line(sorted_rs, rs_table_3m)
            if _s:
                logger.info(f"[RS] {_s}")

        # --- Write IPO list ---
        # Tickers dropped by yfinance across the long-side pipeline are
        # passed through a depth-conditional ladder (mirror of HK's
        # filter_hk_ipo_candidates): 20-day floor, cap/price/vol/dv/ADR,
        # SMA50/200, 3M RS ≥ 90. Guard: tickers in the 12M RS table cannot
        # be fresh IPOs (need ≥12mo history) — those drops are transient
        # yfinance gaps and excluded before the ladder.
        if rs_table and ipo_drops:
            non_ipo = {t for t in ipo_drops if t.upper() in rs_table}
            if non_ipo:
                logger.info(
                    f"[IPO] Dropping {len(non_ipo)} tickers with RS history "
                    f"(transient yfinance gaps, not IPOs): {sorted(non_ipo)}"
                )
                ipo_drops -= non_ipo

        ipo_kept: list[str] = []
        if ipo_drops:
            import us_ipo
            ipo_tickers = sorted(ipo_drops)
            logger.info(f"[IPO] Fetching k-lines for {len(ipo_tickers)} candidates...")
            ipo_klines = us_rs_3m.fetch_us_klines_yf(
                ipo_tickers, period="1y", batch_size=500, include_ohlcv=True,
            )
            spy_kline = None
            if rs_table_3m is not None and min_rs_percentile_3m > 0:
                # Reuse SPY data (single ticker, yfinance caches it briefly).
                spy_kline = us_rs_3m._fetch_spy_kline(period="6mo")
            ipo_kept, ipo_drop_counts = us_ipo.filter_us_ipo_candidates(
                klines=ipo_klines,
                finviz_caps=ipo_finviz_caps,
                rs_table_3m_full=rs_table_3m,
                spy_kline=spy_kline,
                settings=settings,
            )
            logger.info(
                f"[IPO] {len(ipo_kept)}/{len(ipo_tickers)} kept; drops={ipo_drop_counts}"
            )

        sorted_ipo = sorted(ipo_kept)
        sorted_ipo = _dedup_seen("[IPO]", sorted_ipo, ipo_seen, ipo_seen_path)
        dated = us_output_dir / f"{today}_IPO.txt"
        write_watchlist(sorted_ipo, dated, fmt)
        logger.info(f"[IPO] {len(sorted_ipo)} tickers -> {dated}")
        _write_webull(sorted_ipo, dated, output_dir)
        _futu_sync(config, "ipo", sorted_ipo, "US")
        _tv_sync(config, "ipo", sorted_ipo, "US")

        # --- HK EOD pipeline (Shorts + Longs/Leaders/RS) ---
        # In us-eod mode the HK pipeline is skipped — that's the dedicated
        # 8 PM HKT hk-eod slot's job, where HK k-line data is finalized
        # (HK market closed at 16:00 HKT). Running HK here at 10 AM HKT
        # would feed the cross-day master with incomplete intraday bars.
        if args.mode == "eod":
            try:
                run_hk_eod(
                    config=config,
                    output_dir=output_dir,
                    today_iso=str(today),
                    write_watchlist=write_watchlist,
                    write_webull=_write_webull,
                    futu_sync=_futu_sync,
                    tv_sync=_tv_sync,
                    load_seen=_load_seen,
                    persist_seen=_persist_seen,
                    eod_seen_path=_eod_seen_path,
                    dedup_seen=_dedup_seen,
                )
            except Exception as e:
                logger.warning(f"[HK EOD] Pipeline failed: {e}")

        try:
            cleanup_old_outputs(output_dir, today_date)
        except Exception as e:
            logger.warning(f"[cleanup] Failed: {e}")
        logger.info("Done.")
        return 0

    if args.mode == "morning-gap":
        morning_cfg = config.get("morning_gap")
        if not morning_cfg:
            logger.error("[Morning Gap] No [morning_gap] config section found")
            return 1
        morning_cfg.setdefault("min_adr_percent", min_adr_percent)
        morning_cfg.setdefault("adr_days", adr_days)

        _log_section(f"[Morning Gap] Running: {morning_cfg['name']}")
        try:
            offset, tickers = run_morning_gap(
                morning_cfg, futu_cfg=config.get("futu") or {}
            )
        except Exception as e:
            logger.warning(f"[Morning Gap] Failed: {e}")
            return 1

        if offset is None:
            return 0  # Outside scan window — not an error

        # Pre-market and post-open scans write to separate files / Futu groups
        # so each gets its own drop-guard baseline (filter strictness differs).
        is_pre = offset < 0
        stem = _gap_scan_stem(offset)
        futu_key = "morning_gap_pre" if is_pre else "morning_gap"
        sign = "" if is_pre else "+"

        sorted_tickers = sorted(set(tickers))
        dated = us_output_dir / f"{today}_{stem}.txt"
        write_watchlist(sorted_tickers, dated, fmt)
        logger.info(
            f"[Morning Gap] {sign}{offset}min: {len(sorted_tickers)} tickers -> {dated}"
        )
        _write_webull(sorted_tickers, dated, output_dir)
        _futu_sync(config, futu_key, sorted_tickers, "US")
        _tv_sync(config, futu_key, sorted_tickers, "US")
        fresh, promoted = _morning_gap_classify(
            today, sorted_tickers, output_dir, is_pre
        )
        if fresh or promoted:
            notify_morning_gap(
                fresh, offset, len(sorted_tickers), config, promoted=promoted,
                all_tickers=sorted_tickers,
            )

        if is_pre and fresh:
            snapshot_rows = _fetch_snapshot_rows(
                fresh, futu_cfg=config.get("futu") or {}
            )
            today_iso = today_date.isoformat()
            _spawn_catalyst_report(
                fresh=fresh,
                snapshot_rows=snapshot_rows,
                today_iso=today_iso,
                offset=offset,
                config=config,
            )

        try:
            cleanup_old_outputs(output_dir, today_date)
        except Exception as e:
            logger.warning(f"[cleanup] Failed: {e}")
        logger.info("Done.")
        return 0

    if args.mode == "hk-morning-gap":
        morning_cfg = config.get("hk_morning_gap")
        if not morning_cfg:
            logger.error("[HK Morning Gap] No [hk_morning_gap] config section found")
            return 1
        # Inherit ADR settings from [hk_settings] if not overridden — HK
        # long-side baseline is min_adr_percent=3.5, lower than US's 4.0.
        hk_settings = config.get("hk_settings") or {}
        morning_cfg.setdefault("min_adr_percent", hk_settings.get("min_adr_percent", 3.5))
        morning_cfg.setdefault("adr_days", hk_settings.get("adr_days", 20))

        _log_section(f"[HK Morning Gap] Running: {morning_cfg['name']}")
        try:
            offset, yf_tickers = run_hk_morning_gap(
                morning_cfg, futu_cfg=config.get("futu") or {}
            )
        except Exception as e:
            logger.warning(f"[HK Morning Gap] Failed: {e}")
            return 1

        if offset is None:
            return 0  # Outside scan window — not an error

        # Convert yfinance format "0700.HK" → TradingView "HKEX:700"
        # (leading zeros stripped — TradingView rejects HKEX:0148 imports).
        def _yf_to_tv(yf: str) -> str:
            stripped = yf.replace(".HK", "").lstrip("0")
            return f"HKEX:{stripped or '0'}"

        tv_tickers = sorted({_yf_to_tv(t) for t in yf_tickers})
        dated = hk_output_dir / f"{today}_{_gap_scan_stem(offset, hk=True)}.txt"
        write_watchlist(tv_tickers, dated, fmt)
        logger.info(
            f"[HK Morning Gap] +{offset}min: {len(tv_tickers)} tickers -> {dated}"
        )
        _write_webull(tv_tickers, dated, output_dir)
        _futu_sync(config, "hk_morning_gap", tv_tickers, "HK")
        _tv_sync(config, "hk_morning_gap", tv_tickers, "HK")
        fresh, _ = _morning_gap_classify(
            today, tv_tickers, output_dir, is_pre=False, market="HK"
        )
        if fresh:
            notify_morning_gap(
                fresh, offset, len(tv_tickers), config,
                promoted=[], market="HK", all_tickers=tv_tickers,
            )

        try:
            cleanup_old_outputs(output_dir, today_date)
        except Exception as e:
            logger.warning(f"[cleanup] Failed: {e}")
        logger.info("Done.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
