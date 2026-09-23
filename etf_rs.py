"""Daily 3M RS ranking of a fixed ETF list (sector / theme rotation view).

Reuses the US 3M RS algorithm from ``us_rs_3m`` (Σ 0.5·R1M + 0.3·R2M +
0.2·R3M, benchmark-relative to SPY, 0-99 percentile) — but the universe is
the ``[etf_rs].tickers`` list from config.toml, so the percentile is *within
the ETF set*, not against the ~6000-stock Fred6725 universe. Computed locally:
~50 tickers is a single yfinance batch, well under the home-IP throttle that
pushed the stock-universe compute to GitHub Actions.

Output: ``output/TV/US/<YYYY_MM_DD>_ETF_rs.txt`` — one ETF per line,
strongest at the top, ``TICKER ↑N - 中文名 | 前五大持仓`` (``↑N`` / ``↓N`` /
``=`` / ``新`` = rank change vs the latest earlier snapshot in the same
folder, see ``read_previous_ranks``; omitted when none exists; name from the
``[etf_rs.tickers]`` table — a bare list works too and yields bare symbols;
holdings from the static ``[etf_rs.holdings]`` table, hand-maintained from
issuer disclosures, omitted when absent). Tickers sharing the
same 中文名 (e.g. two leveraged gold-miner ETFs) collapse to the strongest
one — the name is the "same instrument" key. Human-readable, not a
TradingView import. It is a ranking snapshot, NOT a watchlist: same-day rerun
overwrites, no eod_seen dedup, no Webull mirror, no Futu/TV sync. The scored
table (rank / 3M relative score / percentile) goes to the log. Aged out by
the generic ``TV/US`` 5-day cleanup rule.

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
_STEM = "ETF_rs"


def _fetch_klines(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """6mo daily closes via the shared retrying yfinance fetcher.
    Monkeypatched in tests."""
    return us_rs_3m.fetch_us_klines_yf(tickers, period="6mo")


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
) -> pd.DataFrame:
    """Score ``tickers`` with the 3M algorithm relative to ``benchmark`` and
    return the table sorted strongest first (columns: raw_score,
    rs_percentile). Tickers with < 64 bars are excluded, never padded. A
    missing benchmark falls back to absolute scores (warned inside
    ``compute_us_rs_3m_table``)."""
    etf_klines = {t: klines[t] for t in tickers if t in klines}
    bench = klines.get(benchmark)
    if bench is None:
        bench = pd.DataFrame({"time_key": [], "close": []})
    table = us_rs_3m.compute_us_rs_3m_table(etf_klines, bench)
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


_FILE_RE = re.compile(rf"^(\d{{4}}_\d{{2}}_\d{{2}})_{_STEM}\.txt$")


def read_previous_ranks(output_dir: Path, today: date) -> dict[str, int] | None:
    """``{ticker: 1-based rank}`` from the most recent ``TV/US/<date>_ETF_rs.txt``
    dated strictly before ``today`` (so a same-day rerun compares against the
    same prior snapshot, not its own earlier output). The ticker is the first
    whitespace token of each line — works on both bare and already-annotated
    lines. None when no earlier snapshot exists (long weekend past the 5-day
    retention, first run) or the file cannot be read."""
    target = output_dir / "TV" / "US"
    if not target.is_dir():
        return None
    candidates: list[tuple[date, Path]] = []
    for p in target.iterdir():
        m = _FILE_RE.match(p.name)
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
        parts = line.split()
        if parts and parts[0] not in ranks:
            ranks[parts[0]] = len(ranks) + 1
    return ranks


def rank_delta_marker(prev_rank: int | None, rank: int) -> str:
    """``↑N`` moved up N places, ``↓N`` moved down, ``=`` unchanged, ``新``
    absent from the previous snapshot."""
    if prev_rank is None:
        return "新"
    if prev_rank == rank:
        return "="
    return f"↑{prev_rank - rank}" if prev_rank > rank else f"↓{rank - prev_rank}"


def _format_line(ticker: str, name: str, holdings: str, marker: str = "") -> str:
    head = f"{ticker} {marker}" if marker else ticker
    line = f"{head} - {name}" if name else head
    return f"{line} | {holdings}" if holdings else line


def write_ranking(
    table: pd.DataFrame,
    names: dict[str, str],
    holdings: dict[str, str],
    output_dir: Path,
    today: date,
    prev_ranks: dict[str, int] | None = None,
) -> Path | None:
    """Write ``TV/US/<YYYY_MM_DD>_ETF_rs.txt``: one
    ``TICKER ↑N - 中文名 | 前五大持仓`` per line, strongest at the top (name /
    holdings segments omitted when unknown). The rank-change marker
    (``rank_delta_marker`` vs ``prev_ranks``) is omitted entirely when there
    is no previous snapshot. Empty table → no file (no 0-byte artifacts);
    returns the path or None."""
    if table is None or table.empty:
        return None
    target = output_dir / "TV" / "US"
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{today.strftime('%Y_%m_%d')}_{_STEM}.txt"
    lines = []
    for i, t in enumerate(table.index, start=1):
        marker = rank_delta_marker(prev_ranks.get(t), i) if prev_ranks is not None else ""
        lines.append(_format_line(t, names.get(t, ""), holdings.get(t, ""), marker))
    out.write_text("\n".join(lines) + "\n")
    return out


def _log_table(table: pd.DataFrame, names: dict[str, str],
               prev_ranks: dict[str, int] | None = None) -> None:
    lines = [f"[{_LABEL}] rank  ticker  3M-rel   pct  Δ     name"]
    for i, (t, row) in enumerate(table.iterrows(), start=1):
        marker = rank_delta_marker(prev_ranks.get(t), i) if prev_ranks is not None else "-"
        lines.append(
            f"[{_LABEL}] {i:>4}  {t:<6}  {row['raw_score'] * 100:+6.1f}%  "
            f"{int(row['rs_percentile']):>3}  {marker:<4}  {names.get(t, '')}"
        )
    logger.info("\n".join(lines))


def run_etf_rs(cfg: dict, output_dir: Path, today: date) -> Path | None:
    """Entry point for us-eod / ``--mode etf-rs``. Returns the written path,
    or None when disabled, unconfigured, or nothing could be scored. Never
    raises on data problems (a fetch that returns nothing is a warning)."""
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

    logger.info(f"[{_LABEL}] Fetching 6mo closes for {len(etfs)} ETFs + {benchmark}...")
    klines = _fetch_klines(etfs + [benchmark])
    if not klines:
        logger.warning(f"[{_LABEL}] yfinance returned nothing; no ranking written today")
        return None
    if benchmark not in klines:
        logger.warning(f"[{_LABEL}] {benchmark} missing from fetch; ranking on absolute 3M scores")

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
    out = write_ranking(table, names, holdings, output_dir, today, prev_ranks)
    if out is None:
        logger.warning(f"[{_LABEL}] nothing scored; no file written")
        return None
    _log_table(table, names, prev_ranks)
    logger.info(f"[{_LABEL}] {len(table)} ETFs ranked -> {out}")
    return out
