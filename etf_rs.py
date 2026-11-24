"""Daily 3M RS ranking of a fixed ETF list (sector / theme rotation view).

Reuses the US 3M RS algorithm from ``us_rs_3m`` (Σ 0.5·R1M + 0.3·R2M +
0.2·R3M, benchmark-relative to SPY, 0-99 percentile) — but the universe is
the ``[etf_rs].tickers`` list from config.toml, so the percentile is *within
the ETF set*, not against the ~6000-stock Fred6725 universe. Computed locally:
~50 tickers is a single yfinance batch, well under the home-IP throttle that
pushed the stock-universe compute to GitHub Actions.

Output: ``output/TV/US/<YYYY_MM_DD>_ETF_rs_3m.txt`` — one ETF per line,
strongest at the top, ``TICKER 🟢↑N - 中文名 | 前十大持仓`` (``🟢↑N`` / ``🔴↓N`` /
``=`` / ``新`` = rank change vs the latest earlier snapshot **of the same
stem** in the same folder, see ``read_previous_ranks``; omitted when none
exists; name from the
``[etf_rs.tickers]`` table — a bare list works too and yields bare symbols;
holdings from the static ``[etf_rs.holdings]`` table, hand-maintained from
issuer disclosures, omitted when absent). Tickers sharing the
same 中文名 (e.g. two leveraged gold-miner ETFs) collapse to the strongest
one — the name is the "same instrument" key. Human-readable, not a
TradingView import. It is a ranking snapshot, NOT a watchlist: same-day rerun
overwrites, no eod_seen dedup, no Webull mirror, no Futu/TV sync. The scored
table (rank / 3M relative score / percentile) goes to the log. Aged out by
the generic ``TV/US`` 5-day cleanup rule.

Under each ETF's row the file carries one indented continuation line
``   3M强: A > B > C > D > E`` — the ETF's US-listed constituents ranked by
the same 3M ladder (``etf_holdings_rs``; constituents from stockanalysis
top-25 with a daily cache, yfinance top-10 fallback flagged ``(前十大)``).
Computed before the file is written; a failure costs only that line.
``read_previous_ranks`` skips indented lines, so the markers above are
unaffected.

The 1M companion (``<date>_ETF_rs_1m.txt``, the ``1M强:`` line, the 1M
window of the trend page) was retired on 2026-11-24 (→ decisions): the
``.txt`` is 3M only, and the trend page pairs 3M with a 12M window instead.
Leftover ``_1m`` files age out with the ``TV/US`` 5-day rule.

The same klines also feed the trend page ``Reports/ETF/etf_rs_trend.html``
(``etf_rs_chart``: per-day 3M + 12M rank / score history recomputed from closes, top
``chart_top_n`` drawn thick) — a soft step after the ranking is written.

Soft-fail by design: runs as a side-step of us-eod; any failure logs a
warning and leaves the EOD exit code untouched.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import us_rs_3m

logger = logging.getLogger("momentum_scanner")

_LABEL = "ETF RS"
_STEM = "ETF_rs_3m"       # 3M ladder (was "ETF_rs" before 2026-10-15)
# Older stems still accepted as the "previous snapshot" of a stem, so the
# rename did not blank the rank-change markers for the retention window.
_LEGACY_STEMS: dict[str, tuple[str, ...]] = {_STEM: ("ETF_rs",)}


def _fetch_klines(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """2y daily closes via the shared retrying yfinance fetcher. The ranking
    itself only needs the last 64 bars; the rest is the trend chart's
    history (``etf_rs_chart``: its 12M ladder needs 253 bars per scored day,
    so 2y gives ~250 days on which both windows are scorable). A latest row with a NaN
    Close (Yahoo lag) is filled from intraday bars rather than dropped, so
    the ranking is not silently a day stale. Monkeypatched in tests."""
    return us_rs_3m.fetch_us_klines_yf(tickers, period="2y", fill_last_close=True)


def _holdings(cfg_holdings) -> dict[str, str]:
    """``{ticker: 持仓字符串}`` from ``[etf_rs.holdings]``; blanks dropped."""
    if not isinstance(cfg_holdings, dict):
        return {}
    out: dict[str, str] = {}
    for t, h in cfg_holdings.items():
        t = str(t).strip().upper()
        h = str(h or "").strip()
        if t and h:
            out[t] = h
    return out


def _yf_symbols(cfg_map) -> dict[str, str]:
    """``{display ticker: yfinance symbol}`` from ``[etf_rs.yf_symbols]`` —
    for non-ETF instruments whose Yahoo symbol differs from the displayed one
    (crypto: ``BTCUSD`` → ``BTC-USD``). Blanks dropped."""
    if not isinstance(cfg_map, dict):
        return {}
    out: dict[str, str] = {}
    for t, sym in cfg_map.items():
        t = str(t).strip().upper()
        sym = str(sym or "").strip().upper()
        if t and sym:
            out[t] = sym
    return out


def align_to_benchmark_days(
    klines: dict[str, pd.DataFrame],
    tickers: list[str],
    benchmark: str,
) -> dict[str, pd.DataFrame]:
    """Cut ``tickers``' klines to the benchmark's trading days. The 3M score
    counts *rows* (21/42/63), so a 7-day-a-week crypto series would otherwise
    measure ~9 weeks instead of 3 months; this also drops the crypto bar of a
    day SPY hasn't closed yet. No benchmark → left as is."""
    bench = klines.get(benchmark)
    if bench is None or bench.empty:
        return klines
    days = set(_naive_days(bench["time_key"]))
    out = dict(klines)
    for t in tickers:
        df = out.get(t)
        if df is None or df.empty:
            continue
        mask = [d in days for d in _naive_days(df["time_key"])]
        out[t] = df[mask].reset_index(drop=True)
    return out


def _naive_days(col) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(col))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def _normalise(tickers) -> dict[str, str]:
    """``{ticker: 中文名}`` from either the ``[etf_rs.tickers]`` table or a
    plain list (names empty). Upper-case, strip, drop blanks and duplicates
    (first occurrence wins) — config order kept."""
    items = tickers.items() if isinstance(tickers, dict) else ((t, "") for t in tickers)
    out: dict[str, str] = {}
    for t, name in items:
        t = str(t).strip().upper()
        if t and t not in out:
            out[t] = str(name or "").strip()
    return out


def rank_etfs(
    klines: dict[str, pd.DataFrame],
    tickers: list[str],
    benchmark: str,
    weights_bars: list[tuple[int, float]] | None = None,
) -> pd.DataFrame:
    """Score ``tickers`` relative to ``benchmark`` and return the table sorted
    strongest first (columns: raw_score, rs_percentile). ``weights_bars``
    None = the 3M ladder (other ``(bars, weight)`` pairs = another window).
    Tickers with too few bars (64 for 3M) are excluded, never padded. A
    missing benchmark falls back to absolute scores (warned inside
    ``compute_us_rs_3m_table``)."""
    etf_klines = {t: klines[t] for t in tickers if t in klines}
    bench = klines.get(benchmark)
    if bench is None:
        bench = pd.DataFrame({"time_key": [], "close": []})
    table = us_rs_3m.compute_us_rs_3m_table(etf_klines, bench, weights_bars=weights_bars)
    if table.empty:
        return table
    return table.sort_values("raw_score", ascending=False)


def collapse_same_name(
    table: pd.DataFrame,
    names: dict[str, str],
) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Keep only the strongest ticker per 中文名 (table must already be sorted
    strongest first). Unnamed tickers are never collapsed. Returns
    ``(kept_table, [(dropped, kept_in_its_place), ...])``."""
    first_by_name: dict[str, str] = {}
    keep: list[str] = []
    dropped: list[tuple[str, str]] = []
    for t in table.index:
        name = names.get(t, "")
        if not name:
            keep.append(t)
            continue
        winner = first_by_name.get(name)
        if winner is None:
            first_by_name[name] = t
            keep.append(t)
        else:
            dropped.append((t, winner))
    return table.loc[keep], dropped


def _file_re(stem: str) -> re.Pattern[str]:
    return re.compile(rf"^(\d{{4}}_\d{{2}}_\d{{2}})_{re.escape(stem)}\.txt$")


_RANK_PREFIX_RE = re.compile(r"^\d+\.$")  # leading "12." rank number on each output line


def read_previous_ranks(output_dir: Path, today: date,
                        stem: str = _STEM) -> dict[str, int] | None:
    """``{ticker: 1-based rank}`` from the most recent ``TV/US/<date>_<stem>.txt``
    (stems are matched exactly, plus ``_LEGACY_STEMS``) dated strictly before ``today`` (so a same-day rerun compares against the
    same prior snapshot, not its own earlier output). The ticker is the first
    whitespace token of each line after an optional ``N.`` rank prefix — works
    on numbered, bare and already-annotated lines; indented continuation
    lines (the per-ETF ``3M强:`` holdings line) are skipped. None when no
    earlier snapshot exists (long weekend past the 5-day retention, first
    run) or the file cannot be read."""
    target = output_dir / "TV" / "US"
    if not target.is_dir():
        return None
    candidates: list[tuple[date, Path]] = []
    file_res = [_file_re(s) for s in (stem, *_LEGACY_STEMS.get(stem, ()))]
    for p in target.iterdir():
        m = next((m for r in file_res if (m := r.match(p.name))), None)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y_%m_%d").date()
        except ValueError:
            continue
        if d < today:
            candidates.append((d, p))
    if not candidates:
        return None
    _, prev = max(candidates)
    try:
        lines = prev.read_text().splitlines()
    except OSError as e:
        logger.warning(f"[{_LABEL}] cannot read previous snapshot {prev}: {e}")
        return None
    ranks: dict[str, int] = {}
    for line in lines:
        if line[:1].isspace():
            continue  # indented "   3M强: …" continuation line (etf_holdings_rs), not an ETF row
        parts = line.split()
        if parts and _RANK_PREFIX_RE.match(parts[0]):
            parts = parts[1:]  # "12." rank prefix (current format); legacy lines have none
        if parts and parts[0] not in ranks:
            ranks[parts[0]] = len(ranks) + 1
    return ranks


def rank_delta_marker(prev_rank: int | None, rank: int) -> str:
    """``🟢↑N`` moved up N places, ``🔴↓N`` moved down, ``=`` unchanged, ``新``
    absent from the previous snapshot. The colored dot is the only way to
    tint a plain-text arrow (green = up, red = down)."""
    if prev_rank is None:
        return "新"
    if prev_rank == rank:
        return "="
    return f"🟢↑{prev_rank - rank}" if prev_rank > rank else f"🔴↓{rank - prev_rank}"


def _format_line(ticker: str, name: str, holdings: str, marker: str = "",
                 rank: int | None = None) -> str:
    head = f"{ticker} {marker}" if marker else ticker
    if rank is not None:
        head = f"{rank}. {head}"
    line = f"{head} - {name}" if name else head
    return f"{line} | {holdings}" if holdings else line


def write_ranking(
    table: pd.DataFrame,
    names: dict[str, str],
    holdings: dict[str, str],
    output_dir: Path,
    today: date,
    prev_ranks: dict[str, int] | None = None,
    stem: str = _STEM,
    extras: dict[str, str] | None = None,
) -> Path | None:
    """Write ``TV/US/<YYYY_MM_DD>_<stem>.txt``: one
    ``N. TICKER 🟢↑N - 中文名 | 前十大持仓`` per line (``N.`` = 1-based rank),
    strongest at the top (name /
    holdings segments omitted when unknown). The rank-change marker
    (``rank_delta_marker`` vs ``prev_ranks``) is omitted entirely when there
    is no previous snapshot. ``extras`` (``etf_holdings_rs.compute_extras``)
    adds one 3-space-indented continuation line under an ETF's row, verbatim
    (``   3M强: A > B > …``); absent / empty → no extra line. Empty table →
    no file (no 0-byte artifacts); returns the path or None."""
    if table is None or table.empty:
        return None
    target = output_dir / "TV" / "US"
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{today.strftime('%Y_%m_%d')}_{stem}.txt"
    lines = []
    for i, t in enumerate(table.index, start=1):
        marker = rank_delta_marker(prev_ranks.get(t), i) if prev_ranks is not None else ""
        lines.append(_format_line(t, names.get(t, ""), holdings.get(t, ""), marker, rank=i))
        extra = (extras or {}).get(t)
        if extra:
            lines.append(f"   {extra}")
    out.write_text("\n".join(lines) + "\n")
    return out


def _log_table(table: pd.DataFrame, names: dict[str, str],
               prev_ranks: dict[str, int] | None = None, window: str = "3M") -> None:
    lines = [f"[{_LABEL}] rank  ticker  {window}-rel   pct  Δ     name"]
    for i, (t, row) in enumerate(table.iterrows(), start=1):
        marker = rank_delta_marker(prev_ranks.get(t), i) if prev_ranks is not None else "-"
        lines.append(
            f"[{_LABEL}] {i:>4}  {t:<6}  {row['raw_score'] * 100:+6.1f}%  "
            f"{int(row['rs_percentile']):>3}  {marker:<4}  {names.get(t, '')}"
        )
    logger.info("\n".join(lines))


def run_etf_rs(cfg: dict, output_dir: Path, today: date,
               holdings_rs: bool = True) -> Path | None:
    """Entry point for us-eod / ``--mode etf-rs``. Returns the written path,
    or None when disabled, unconfigured, or nothing could be scored. Never
    raises on data problems (a fetch that returns nothing is a warning).
    ``holdings_rs=False`` skips the per-ETF strongest-holdings line
    (``etf_holdings_rs``) — the cloud Pages build passes it, so GitHub
    Actions never scrapes stockanalysis or downloads ~1000 constituents."""
    if not cfg.get("enabled", True):
        logger.info(f"[{_LABEL}] disabled in config; skipping")
        return None
    names = _normalise(cfg.get("tickers", []))
    holdings = _holdings(cfg.get("holdings", {}))
    benchmark = str(cfg.get("benchmark", "SPY")).strip().upper()
    if not names:
        logger.info(f"[{_LABEL}] no tickers configured; skipping")
        return None
    etfs = [t for t in names if t != benchmark]
    yf_symbols = {t: s for t, s in _yf_symbols(cfg.get("yf_symbols", {})).items() if t in etfs}
    to_display = {s: t for t, s in yf_symbols.items()}

    logger.info(f"[{_LABEL}] Fetching 2y closes for {len(etfs)} ETFs + {benchmark}...")
    klines = _fetch_klines([yf_symbols.get(t, t) for t in etfs] + [benchmark])
    klines = {to_display.get(k, k): v for k, v in (klines or {}).items()}
    if yf_symbols:
        klines = align_to_benchmark_days(klines, list(yf_symbols), benchmark)
    if not klines:
        logger.warning(f"[{_LABEL}] yfinance returned nothing; no ranking written today")
        return None
    if benchmark not in klines:
        logger.warning(f"[{_LABEL}] {benchmark} missing from fetch; ranking on absolute 3M scores")

    # Per-ETF "3M强: A > B > …" continuation line (etf_holdings_rs): computed
    # before the file is written. Soft — any failure here means the ranking
    # goes out without the extra lines, exactly as before this step existed.
    extras: dict[str, str] = {}
    if holdings_rs:
        try:
            import etf_holdings_rs
            extras = etf_holdings_rs.compute_extras(
                cfg.get("holdings_rs", {}), etfs, klines.get(benchmark), today, output_dir)
        except Exception as e:
            logger.warning(f"[{_LABEL}] holdings 3M RS failed, ranking written without it: {e}")
            extras = {}

    table = rank_etfs(klines, etfs, benchmark)
    missing = [t for t in etfs if t not in table.index]
    if missing:
        logger.warning(f"[{_LABEL}] {len(missing)} ETF(s) unscored (no data / < 64 bars): {missing}")
    table, collapsed = collapse_same_name(table, names)
    if collapsed:
        logger.info(
            f"[{_LABEL}] same-name collapse, hidden: "
            + ", ".join(f"{d} (→ {w})" for d, w in collapsed)
        )
    no_holdings = [t for t in table.index if t not in holdings]
    if no_holdings:
        logger.info(f"[{_LABEL}] no holdings configured for: {no_holdings}")
    prev_ranks = read_previous_ranks(output_dir, today)
    if prev_ranks is None:
        logger.info(f"[{_LABEL}] no earlier snapshot before {today}; rank-change markers omitted")
    out = write_ranking(table, names, holdings, output_dir, today, prev_ranks, extras=extras)
    if out is None:
        logger.warning(f"[{_LABEL}] nothing scored; no file written")
        return None
    _log_table(table, names, prev_ranks)
    logger.info(f"[{_LABEL}] {len(table)} ETFs ranked -> {out}")
    # Cumulative watchlist of the top ETFs' strongest constituents
    # (etf_rs_leaders): reads the ranking file just written. Soft.
    try:
        import etf_rs_leaders
        etf_rs_leaders.run_leaders(cfg.get("leaders", {}), output_dir, today, out)
    except Exception as e:
        logger.warning(f"[{_LABEL}] leaders watchlist failed, ranking unaffected: {e}")
    # Trend page (Reports/ETF/etf_rs_trend.html): same klines, same order as
    # the .txt. Soft — a render failure must not cost the ranking.
    try:
        import etf_rs_chart
        etf_rs_chart.write_trend_chart(
            klines, list(table.index), benchmark, names, holdings, output_dir, today,
            top_n=int(cfg.get("chart_top_n", 20)), extras=extras,
        )
    except Exception as e:
        logger.warning(f"[{_LABEL}] trend chart failed, ranking unaffected: {e}")
    return out
