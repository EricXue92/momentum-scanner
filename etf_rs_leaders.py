"""Cumulative TradingView watchlist of the strongest constituents of the
strongest ETFs (``[etf_rs.leaders]``).

Every run, after ``etf_rs`` has written ``<date>_ETF_rs_1m.txt``, the top
``top_etfs`` (10) rows of THAT file are read back and the symbols of their
``   1M强: A > B > …`` continuation lines (``etf_holdings_rs``) are merged,
in rank order, into one candidate list (≤ 10 × 5 names; an ETF without a
line — IBIT, a 404, a fetch failure — contributes nothing and is not
back-filled from rank 11). The file, not the in-memory tables, is the
input on purpose: the list is "the names under the first ten rows of the
file you read every day", and a rerun / a hand-fixed file gives the same
answer.

Two outputs under ``output/TV/US/``, both comma-separated so they import
into TradingView as they are:

* ``ETF_rs_leaders.txt`` — the **master**: undated, append-only, in order of
  first appearance; never pruned, never aged out (no ``_RETENTION_RULES``
  entry matches an undated name). Delete it to reset. Hand edits are
  tolerated (newline- or comma-separated, duplicates dropped on read).
* ``<date>_ETF_rs_new.txt`` — the day's additions (names the master did not
  have), absent when nothing is new. Like every EOD list it is written
  once per day: a same-day rerun that finds further new names grows the
  master but keeps the first run's file (log says so). Aged out by the
  generic ``TV/US`` 5-day rule.

* ``<date>_ETF_rs_pruned.txt`` — names the **SMA50 first-break prune** removed
  today (comma-separated; absent when none; a same-day rerun merges, since
  the rerun no longer sees the names the first run removed). Same 5-day rule.

**Floors** (``min_price`` 20 / ``min_dollar_volume`` 100M /
``min_adr_percent`` 3.5, 0 = off): latest close, the 20-session average of
close × volume, and ADR% = 20-session mean of (High − Low) / Close × 100 (same
formula as ``main._filter_adr_percent``, but < 20 usable bars → not judged,
kept, unlike that strict filter). Applied at **entry** (a candidate under a
floor is not added today — it comes in the day it qualifies; no bars for it →
added, like every other gate's pass-through) and to the **existing master** in
the same prune pass below. One 1y batch for master ∪ today's candidates serves
both (``run_leaders``).

**Entry-only gates** (``entry_above_sma`` [50, 200] / ``entry_min_rs_percentile_1m``
90; ``[]`` / 0 = off): a NEW name also needs its latest close strictly above
both SMAs (a period longer than its history is not judged) and a market-wide 1M
RS percentile ≥ 90 (``rs_percentile_1m`` in the cloud US RS table; no table, no
column, or a ticker absent from it → not judged). They are NOT retention rules:
once on the list, a name leaves only through the floors or the prune below, so
the first-break rule keeps its meaning.

**Prune rule** (``find_break_drops``, after today's names were added, over
the whole master): the trailing streak of completed sessions whose close is
below SMA50 must be ≥ 2 days AND the latest close must be below the LOW of
the streak's FIRST day (the day it first closed under the line) — not
yesterday's low, not yesterday's close (that is ``sma50_prune``'s rule for
the EOD master). A streak of one, a close back above the line (streak
restarts), or a close holding at/above that first-day low keeps the name.
Soft-fail like ``sma50_prune``: a failed batch skips the prune; names with
no data / < 50 bars / a stale series (halted, delisted) / a NaN first-day
low are kept. A pruned name comes back the next day it is in a top ETF's
top 5 again. No ``.bak``: the pruned file is the record.

**EOD master dedup** (``state/eod_seen_US.txt``, read-only): a candidate the
EOD master holds is not added, and a name already on this list leaves the day
the EOD master holds it (``run_leaders``, before the download; logged, not in
the pruned file). It can come back once the EOD master has dropped it. No EOD
master file (the cloud Pages build) → no dedup.

Soft step: ``run_etf_rs`` wraps it; a failure costs neither ranking file nor
the trend page. No ntfy, no Webull mirror, no Futu/TV sync.
"""

from __future__ import annotations

import os
import logging
import re
from datetime import date
from pathlib import Path
from typing import NamedTuple

import pandas as pd

import us_rs_3m

logger = logging.getLogger("momentum_scanner")

_LABEL = "ETF RS 龙头"
MASTER_NAME = "ETF_rs_leaders.txt"
NEW_STEM = "ETF_rs_new"
PRUNED_STEM = "ETF_rs_pruned"
DEFAULT_TOP_ETFS = 10
DEFAULT_SMA_PERIOD = 50
DEFAULT_MIN_PRICE = 20.0
DEFAULT_MIN_DOLLAR_VOLUME = 100_000_000.0
DOLLAR_VOLUME_WINDOW = 20
DEFAULT_MIN_ADR_PERCENT = 3.5
ADR_WINDOW = 20
DEFAULT_ENTRY_ABOVE_SMA = (50, 200)
DEFAULT_ENTRY_MIN_RS_1M = 90
RS_1M_COLUMN = "rs_percentile_1m"
_UNSET = object()


class Bars(NamedTuple):
    """Per-ticker daily series from one yfinance batch (see ``_fetch_daily_bars``)."""
    closes: dict[str, pd.Series]
    lows: dict[str, pd.Series]
    volumes: dict[str, pd.Series]
    highs: dict[str, pd.Series]

_EXTRA_PREFIX = "1M强:"
_EXTRA_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")       # " (前十大)"
_ROW_RE = re.compile(r"^(\d+)\.\s+(\S+)")                  # "12. XLV 🟢↑2 - …"
_SPLIT_RE = re.compile(r"[,\s]+")


def parse_extra(line: str) -> list[str]:
    """Inverse of ``etf_holdings_rs.format_extra``: ``"   1M强: A > B (前十大)"``
    → ``["A", "B"]``. Anything that is not such a line → ``[]``."""
    text = line.strip()
    if not text.startswith(_EXTRA_PREFIX):
        return []
    text = _EXTRA_SUFFIX_RE.sub("", text[len(_EXTRA_PREFIX):])
    return [s.strip() for s in text.split(">") if s.strip()]


def parse_ranking(text: str) -> list[tuple[str, list[str]]]:
    """``[(etf, strongest_constituents)]`` in file order, one entry per
    ``N. TICKER …`` row; the constituents come from the indented ``1M强:``
    line right under the row (``[]`` when the row has none)."""
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if m:
            entries.append((m.group(2), []))
        elif entries and line[:1].isspace():
            syms = parse_extra(line)
            if syms:
                entries[-1] = (entries[-1][0], syms)
    return entries


def candidates(entries: list[tuple[str, list[str]]], top_etfs: int = DEFAULT_TOP_ETFS) -> list[str]:
    """Ordered, de-duplicated union of the constituents of the first
    ``top_etfs`` entries (ETF rank first, then strength within the ETF)."""
    out: list[str] = []
    seen: set[str] = set()
    for _, syms in entries[:top_etfs]:
        for s in syms:
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def load_master(path: Path) -> list[str]:
    """The master as an ordered list; missing file → ``[]``. Accepts commas
    and/or whitespace as separators (hand edits), drops duplicates."""
    if not path.is_file():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tok in _SPLIT_RE.split(path.read_text(encoding="utf-8")):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _write_csv(path: Path, symbols: list[str]) -> None:
    # Temp file + os.replace: the master is undated and never regenerated, so
    # a kill mid-write must leave the old file, not a truncated one.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(",".join(symbols) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _canon(symbol: str) -> str:
    """Comparison key across the two masters: Finviz writes ``BRK-B`` into the
    EOD master, the ETF 1M line displays ``BRK.B``."""
    return symbol.replace("-", ".")


def _eod_seen(output_dir: Path) -> set[str]:
    """The US EOD master as comparison keys; no file (the cloud Pages build) → empty."""
    from main import _eod_seen_path, _load_seen  # local import: main imports etf_rs

    return {_canon(t) for t in _load_seen(_eod_seen_path(output_dir, "US"))}


def _drop_eod_seen_from_master(output_dir: Path, master: list[str]) -> list[str]:
    """Remove the names the US EOD master holds from the leaders master, now
    that they sit on the EOD list; returns what is left. Logged only: a
    duplicate is not a weak name, so it stays out of the pruned file."""
    seen = _eod_seen(output_dir)
    dupes = [t for t in master if _canon(t) in seen]
    if not dupes:
        return master
    kept = [t for t in master if _canon(t) not in seen]
    _write_csv(output_dir / "TV" / "US" / MASTER_NAME, kept)
    logger.info(f"[{_LABEL}] removed {len(dupes)} already in eod_seen_US -> master {len(kept)}: {dupes}")
    return kept


def _todays_new(cfg: dict, output_dir: Path, ranking_path: Path | None) -> tuple[list[str], list[str]]:
    """``(master, new)``: the master as stored and today's candidates that
    neither it nor the US EOD master holds, in rank order. ``([], [])`` when
    disabled or the ranking file is missing."""
    if not cfg.get("enabled", True):
        logger.info(f"[{_LABEL}] disabled in config; skipping")
        return [], []
    if ranking_path is None or not Path(ranking_path).is_file():
        logger.warning(f"[{_LABEL}] no 1M ranking file to read; skipping")
        return [], []
    top_etfs = int(cfg.get("top_etfs", DEFAULT_TOP_ETFS))
    entries = parse_ranking(Path(ranking_path).read_text(encoding="utf-8"))
    cands = candidates(entries, top_etfs)
    master = load_master(output_dir / "TV" / "US" / MASTER_NAME)
    known = set(master)
    seen = _eod_seen(output_dir)
    in_eod = [s for s in cands if s not in known and _canon(s) in seen]
    new = [s for s in cands if s not in known and _canon(s) not in seen]
    logger.info(f"[{_LABEL}] top {min(top_etfs, len(entries))} ETFs -> {len(cands)} candidates, "
                f"master {len(master)}, already in eod_seen_US {in_eod}, new {len(new)}: {new}")
    return master, new


def _floors(cfg: dict) -> tuple[float, float, float]:
    return (float(cfg.get("min_price", DEFAULT_MIN_PRICE)),
            float(cfg.get("min_dollar_volume", DEFAULT_MIN_DOLLAR_VOLUME)),
            float(cfg.get("min_adr_percent", DEFAULT_MIN_ADR_PERCENT)))


def _floors_label(cfg: dict) -> str:
    min_price, min_dv, min_adr = _floors(cfg)
    return f"price ≥ {min_price:g}, $vol ≥ {min_dv:,.0f}, ADR% ≥ {min_adr:g}"


def _below_floors(cfg: dict, closes: dict[str, pd.Series], bars: Bars) -> list[str]:
    """Names in ``closes`` under any of the three floors (sorted)."""
    min_price, min_dv, min_adr = _floors(cfg)
    return sorted(set(find_floor_drops(closes, bars.volumes, min_price, min_dv))
                  | set(find_adr_drops(closes, bars.highs, bars.lows, min_adr)))


def _entry_sma_periods(cfg: dict) -> list[int]:
    return [int(p) for p in cfg.get("entry_above_sma", DEFAULT_ENTRY_ABOVE_SMA)]


def _entry_rs_threshold(cfg: dict) -> float:
    return float(cfg.get("entry_min_rs_percentile_1m", DEFAULT_ENTRY_MIN_RS_1M))


def update_leaders(cfg: dict, output_dir: Path, today: date, ranking_path: Path | None,
                   bars: Bars | None = None,
                   plan: tuple[list[str], list[str]] | None = None,
                   rs_table: pd.DataFrame | None = None) -> list[str]:
    """Grow the master from today's 1M ranking file; returns the names added
    today (``[]`` when disabled, no file, or nothing new). With ``bars``,
    candidates under the price / dollar-volume / ADR% floors or not above the
    entry SMAs are not added (a candidate with no bars passes through); with
    ``rs_table``, neither are those under the market-wide 1M RS threshold.
    ``plan`` is ``_todays_new``'s result when the caller already computed it.
    Writes ``<date>_ETF_rs_new.txt`` only when there is something new AND the
    day has no such file yet."""
    master, new = plan if plan is not None else _todays_new(cfg, output_dir, ranking_path)
    if not new:
        return []
    gates: list[tuple[str, list[str]]] = []
    if bars is not None:
        new_closes = {t: bars.closes[t] for t in new if t in bars.closes}
        gates.append((_floors_label(cfg), _below_floors(cfg, bars.closes, bars)))
        gates.append((f"close > SMA{_entry_sma_periods(cfg)}",
                      find_below_sma_drops(new_closes, _entry_sma_periods(cfg))))
    threshold = _entry_rs_threshold(cfg)
    gates.append((f"1M RS ≥ {threshold:g}", find_rs_1m_drops(new, rs_table, threshold)))
    for label, rejected in gates:
        hit = sorted(r for r in rejected if r in new)
        if hit:
            logger.info(f"[{_LABEL}] entry gate ({label}) rejected: {hit}")
            new = [s for s in new if s not in hit]
    if not new:
        return []
    tv_dir = output_dir / "TV" / "US"
    master_path = tv_dir / MASTER_NAME
    tv_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(master_path, master + new)
    new_path = tv_dir / f"{today:%Y_%m_%d}_{NEW_STEM}.txt"
    if new_path.exists():
        logger.info(f"[{_LABEL}] {new_path.name} already written today; keeping the earlier "
                    f"run's file (master grown by {len(new)})")
    else:
        _write_csv(new_path, new)
        logger.info(f"[{_LABEL}] {len(new)} new -> {new_path}")
    return new


# ---------------------------------------------------------------------------
# SMA50 first-break prune
# ---------------------------------------------------------------------------


def _fetch_daily_bars(tickers: list[str]) -> Bars | None:
    """One 1y yfinance batch → ``Bars`` (close / low / volume / high per ticker),
    the closes with ``sma50_prune``'s stale-series exclusion and trailing-NaN
    Close fill applied (the shared EOD conventions); None when the batch
    failed entirely. Monkeypatched in tests."""
    from etf_holdings_rs import to_yf_symbol
    from main import _yf_download_with_retry  # local import: main imports etf_rs
    from sma50_prune import drop_stale_series, fill_missing_last_close

    # The master / 1M line carry the display form (`BRK.B`); Yahoo only knows
    # `BRK-B`. Unconverted, a class share came back with no bars and so
    # passed every gate and was never pruned ("no bars → kept").
    yf_of = {t: to_yf_symbol(t) for t in tickers}
    data = _yf_download_with_retry(
        list(dict.fromkeys(yf_of.values())),
        period="1y", progress=False, group_by="ticker", threads=False,
    )
    if data is None or data.empty:
        return None
    single = not isinstance(data.columns, pd.MultiIndex)  # one ticker → flat frame
    closes: dict[str, pd.Series] = {}
    lows: dict[str, pd.Series] = {}
    volumes: dict[str, pd.Series] = {}
    highs: dict[str, pd.Series] = {}
    for t in tickers:
        try:
            frame = data if single else data[yf_of[t]]
            close, low, vol, high = frame["Close"], frame["Low"], frame["Volume"], frame["High"]
        except (KeyError, IndexError):
            continue
        if close.notna().any():
            closes[t] = close
            lows[t] = low
            volumes[t] = vol
            highs[t] = high
    closes = drop_stale_series(closes)
    closes = fill_missing_last_close(closes, label=_LABEL)
    closes = {t: c.dropna() for t, c in closes.items() if c.notna().any()}
    return Bars(closes, {t: lows[t] for t in closes if t in lows},
                {t: volumes[t] for t in closes if t in volumes},
                {t: highs[t] for t in closes if t in highs})


def _fetch_rs_1m_table(output_dir: Path, today: date) -> pd.DataFrame | None:
    """The cloud-published US RS table, when it carries the market-wide 1M
    percentile. None (gate waived, WARNING) when it is unreachable or was
    published before the column existed. Monkeypatched in tests."""
    try:
        table = us_rs_3m.build_3m_table(output_dir, today)
    except Exception as e:
        logger.warning(f"[{_LABEL}] 1M RS table failed to load ({e}); 1M RS gate waived")
        return None
    if table is None:
        logger.warning(f"[{_LABEL}] no cloud RS table; 1M RS gate waived")
        return None
    if RS_1M_COLUMN not in table.columns:
        logger.warning(f"[{_LABEL}] cloud RS table has no {RS_1M_COLUMN} column; 1M RS gate waived")
        return None
    return table


def find_below_sma_drops(closes: dict[str, pd.Series], periods) -> list[str]:
    """Names whose latest close is not strictly above every SMA in ``periods``.
    A period longer than the name's history is not judged (sorted result)."""
    drops: list[str] = []
    for t, close in closes.items():
        for period in periods:
            if len(close) >= period and not float(close.iloc[-1]) > float(close.tail(period).mean()):
                drops.append(t)
                break
    return sorted(drops)


def find_rs_1m_drops(names: list[str], table: pd.DataFrame | None, threshold: float) -> list[str]:
    """Names whose market-wide 1M RS percentile is below ``threshold``. A
    missing table or column, an absent ticker, or a blank value is not judged
    (the RS gates' missing → kept policy); ``threshold`` ≤ 0 is off."""
    if threshold <= 0 or table is None or RS_1M_COLUMN not in table.columns:
        return []
    drops: list[str] = []
    for t in names:
        if t in table.index and pd.notna(table.at[t, RS_1M_COLUMN]) and float(table.at[t, RS_1M_COLUMN]) < threshold:
            drops.append(t)
    return sorted(drops)


def find_floor_drops(
    closes: dict[str, pd.Series],
    volumes: dict[str, pd.Series],
    min_price: float,
    min_dollar_volume: float,
    window: int = DOLLAR_VOLUME_WINDOW,
) -> list[str]:
    """Names whose latest close is below ``min_price`` or whose average of
    close × volume over the last ``window`` sessions is below
    ``min_dollar_volume`` (0 disables either floor). No volume series → the
    dollar-volume floor is skipped for that name (sorted result)."""
    drops: list[str] = []
    for t, close in closes.items():
        if close.empty:
            continue
        if min_price > 0 and float(close.iloc[-1]) < min_price:
            drops.append(t)
            continue
        vol = volumes.get(t)
        if min_dollar_volume > 0 and vol is not None:
            dv = (close * vol.reindex(close.index)).tail(window).fillna(0.0)
            if dv.empty or float(dv.mean()) < min_dollar_volume:
                drops.append(t)
    return sorted(drops)


def find_adr_drops(
    closes: dict[str, pd.Series],
    highs: dict[str, pd.Series],
    lows: dict[str, pd.Series],
    min_adr_percent: float,
    window: int = ADR_WINDOW,
) -> list[str]:
    """Names whose mean (High − Low) / Close × 100 over the last ``window``
    usable sessions is below ``min_adr_percent`` (0 disables). No high / low
    series or fewer than ``window`` usable bars → not judged, kept (sorted
    result)."""
    if min_adr_percent <= 0:
        return []
    drops: list[str] = []
    for t, close in closes.items():
        high, low = highs.get(t), lows.get(t)
        if high is None or low is None:
            continue
        ranges = ((high.reindex(close.index) - low.reindex(close.index)) / close).dropna().tail(window)
        if len(ranges) == window and float(ranges.mean()) * 100 < min_adr_percent:
            drops.append(t)
    return sorted(drops)


def find_break_drops(
    closes: dict[str, pd.Series],
    lows: dict[str, pd.Series],
    sma_period: int = DEFAULT_SMA_PERIOD,
) -> list[str]:
    """Names whose trailing streak of closes below SMA``sma_period`` is ≥ 2
    sessions AND whose latest close is below the low of that streak's first
    session. Fewer than ``sma_period`` bars, no low series, or a NaN
    first-day low → kept (sorted result)."""
    drops: list[str] = []
    for t, close in closes.items():
        low = lows.get(t)
        if low is None or len(close) < sma_period:
            continue
        sma = close.rolling(sma_period).mean()
        below = (close < sma).to_numpy()
        streak = 0
        for b in below[::-1]:
            if not b:
                break
            streak += 1
        if streak < 2:
            continue
        first_day = close.index[-streak]
        first_low = low.get(first_day)
        if first_low is None or pd.isna(first_low):
            continue
        if float(close.iloc[-1]) < float(first_low):
            drops.append(t)
    return sorted(drops)


def _write_pruned_list(path: Path, drops: list[str]) -> None:
    earlier = load_master(path) if path.exists() else []
    _write_csv(path, sorted(set(earlier) | set(drops)))


def prune_master(cfg: dict, output_dir: Path, today: date, bars: Bars | None | object = _UNSET) -> list[str]:
    """Remove from the master every name that fails the floors
    (``find_floor_drops``) or the SMA50 first-break rule (``find_break_drops``);
    returns the names removed. ``bars`` may be passed by ``run_leaders`` (one
    download for both steps; None = that download failed → skip); when not
    given, this function downloads the master itself. ``[]`` when ``prune``
    is off, the master is empty, the batch failed, or nothing qualifies — the
    master is rewritten only when something was removed."""
    if not cfg.get("prune", True):
        logger.info(f"[{_LABEL}] prune disabled in config; skipping")
        return []
    master_path = output_dir / "TV" / "US" / MASTER_NAME
    master = load_master(master_path)
    if not master:
        return []
    if bars is _UNSET:
        bars = _fetch_daily_bars(master)
    if bars is None:
        logger.warning(f"[{_LABEL}] bar download failed; prune skipped this run")
        return []
    assert isinstance(bars, Bars)
    missing = [t for t in master if t not in bars.closes]
    if missing:
        logger.warning(f"[{_LABEL}] no usable bars for {len(missing)} name(s), kept: {missing}")
    in_master = set(master)
    closes = {t: c for t, c in bars.closes.items() if t in in_master}
    floor_drops = _below_floors(cfg, closes, bars)
    break_drops = find_break_drops(closes, bars.lows, int(cfg.get("sma_period", DEFAULT_SMA_PERIOD)))
    drops = sorted(set(floor_drops) | set(break_drops))
    if not drops:
        logger.info(f"[{_LABEL}] prune: {len(master)} checked, nothing under the floors or the "
                    f"SMA50 first-break low")
        return []
    kept = [t for t in master if t not in set(drops)]
    _write_csv(master_path, kept)
    pruned_path = master_path.parent / f"{today:%Y_%m_%d}_{PRUNED_STEM}.txt"
    try:
        _write_pruned_list(pruned_path, drops)
    except OSError as e:
        logger.warning(f"[{_LABEL}] could not write {pruned_path}: {e}")
    logger.info(f"[{_LABEL}] pruned {len(drops)} -> master {len(kept)}; floors "
                f"({_floors_label(cfg)}): {floor_drops}; "
                f"SMA50 first-break: {break_drops}")
    return drops


def run_leaders(cfg: dict, output_dir: Path, today: date, ranking_path: Path | None) -> None:
    """The daily step: one bar download for master ∪ today's candidates, then
    the gated add (``update_leaders``) and the prune (``prune_master``). No
    download when there is nothing to gate or prune, or when the floors, the
    trend gate and the prune are all off. The RS table is only loaded when
    there is a new name for its gate to judge."""
    master, new = _todays_new(cfg, output_dir, ranking_path)
    if not master and not new:
        return
    master = _drop_eod_seen_from_master(output_dir, master)
    if not master and not new:
        return
    want_bars = (any(f > 0 for f in _floors(cfg))
                 or (bool(new) and bool(_entry_sma_periods(cfg)))
                 or (cfg.get("prune", True) and bool(master)))
    bars: Bars | None = None
    if want_bars:
        bars = _fetch_daily_bars(master + new)
        if bars is None:
            logger.warning(f"[{_LABEL}] bar download failed; price gates waived, prune skipped")
    rs_table = _fetch_rs_1m_table(output_dir, today) if new and _entry_rs_threshold(cfg) > 0 else None
    update_leaders(cfg, output_dir, today, ranking_path, bars=bars, plan=(master, new), rs_table=rs_table)
    prune_master(cfg, output_dir, today, bars=bars)
